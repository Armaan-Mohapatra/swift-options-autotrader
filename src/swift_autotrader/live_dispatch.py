"""Unattended LIVE Trading Dispatcher for SwiftTrades -> Robinhood Agentic MCP.

Coordinates the end-to-end pipeline:
1. Ingests Telegram signals from the local SQLite store.
2. Resolves option contracts, expirations, and live quotes from Robinhood.
3. Keeps the exact original alert premium as the entry limit (no price chasing).
4. Checks all risk and safety gates (buying power, concurrent positions, daily loss cap).
5. Submits buy-to-open and sell-to-close orders through Robinhood Agentic MCP.
6. Reconciles broker order states, fills, and cancellations.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from swift_autotrader.broker.mcp_paper_data import MCPReadOnlyOptionData
from swift_autotrader.broker.robinhood_mcp import (
    MCPAccount,
    RobinhoodLiveBroker,
    RobinhoodMCPBroker,
)
from swift_autotrader.config import Settings
from swift_autotrader.notifications.alerts import NotificationCenter
from swift_autotrader.orders.acceptance_scope import AcceptanceScope
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.exit_policy import ExitQuote
from swift_autotrader.orders.exposure_state import active_signal_ids
from swift_autotrader.orders.risk import RiskCeilings
from swift_autotrader.parser.rules import SignalIntent, parse_message
from swift_autotrader.signals.identity import OptionContract
from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog

log = logging.getLogger("swift_autotrader.live_dispatch")


@dataclass(frozen=True, slots=True)
class LiveDispatchResult:
    status: str
    parsed_events: int
    entry_orders_submitted: int
    close_orders_submitted: int
    cancellations: int
    realized_daily_loss_usd: Decimal
    open_positions_count: int
    unresolved_exit_count: int = 0


class LiveDispatcher:
    """Production live trading dispatcher with comprehensive safety gates."""

    def __init__(
        self,
        db_path: Path,
        channel_id: int,
        mcp_broker: RobinhoodMCPBroker,
        account: MCPAccount,
        provider: MCPReadOnlyOptionData,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        single_entry: bool = False,
        authorized_symbol: str | None = None,
        target_message_id: int | None = None,
        acceptance_scope: AcceptanceScope | None = None,
    ):
        if not mcp_broker.allow_live_writes:
            raise PermissionError("LiveDispatcher requires mcp_broker.allow_live_writes=True")
        # Explicit owner caps only; never substitute an implicit ceiling.
        for cap in (settings.max_premium_per_signal_usd, settings.max_total_exposure_usd):
            if not cap.is_finite() or cap <= 0:
                raise PermissionError("Live dispatch requires explicit positive premium and exposure caps")
        self.db_path = Path(db_path)
        self.channel_id = channel_id
        self.mcp = mcp_broker
        self.account = account
        self.provider = provider
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.single_entry = single_entry
        self.authorized_symbol = authorized_symbol.strip().upper() if authorized_symbol else None
        self.target_message_id = target_message_id
        self.acceptance_scope = acceptance_scope
        if acceptance_scope is not None:
            if not single_entry or acceptance_scope.account != account.account_number or acceptance_scope.channel_id != channel_id:
                raise PermissionError("Supervised account/channel scope mismatch")
            if self.authorized_symbol not in (None, acceptance_scope.contract.underlying) or target_message_id not in (None, acceptance_scope.message_id):
                raise PermissionError("Supervised command scope mismatch")
            self.authorized_symbol = acceptance_scope.contract.underlying
            self.target_message_id = acceptance_scope.message_id

        # Risk limits derived from settings
        self.limits = RiskCeilings(
            contracts_per_signal=settings.max_contracts_per_signal,
            premium_per_signal_usd=settings.max_premium_per_signal_usd,
            total_exposure_usd=settings.max_total_exposure_usd,
            open_signal_count=settings.max_concurrent_positions,
            entry_max_age_seconds=settings.telegram_max_live_age_seconds,
            max_daily_loss_usd=settings.max_daily_loss_usd,
            max_concurrent_positions=settings.max_concurrent_positions,
        )

        # Build live broker adapter
        self.live_broker = RobinhoodLiveBroker(self.mcp, self.account, loop=loop, db_path=self.db_path)

        # Preflight store
        from swift_autotrader.orders.risk import PaperPreflight
        self.preflight = PaperPreflight()

        # Engine configured in LIVE mode
        self.engine = PaperOrderEngine(
            self.db_path,
            self.live_broker,
            preflight=self.preflight,
            limits=self.limits,
            clock=self.clock,
            live_mode=True,
            entry_authorizer=self._authorize_entry if self.single_entry else None,
        )
        self.notifications = NotificationCenter(self.db_path)
        if self.acceptance_scope is not None:
            self.live_broker.mcp.submission_guard = self._authorize_submission

    def _authorize_entry(self, db, signal, intent: OrderIntent, now: datetime) -> bool:
        # Called within the engine's BEGIN IMMEDIATE transaction. Enforce one
        # durable attempted entry even when two dispatch cycles race.
        if db.execute("SELECT 1 FROM order_intents WHERE side='BUY_TO_OPEN' LIMIT 1").fetchone():
            return False
        scope = self.acceptance_scope
        if scope is None:
            # Existing isolated simulation callers only. The real service
            # refuses single-entry startup without an exact local receipt.
            return True
        row = db.execute(
            "SELECT m.channel_id,m.source_message_id,m.first_revision_id,t.latest_revision_id "
            "FROM signal_metadata m JOIN telegram_messages t "
            "ON t.channel_id=m.channel_id AND t.message_id=m.source_message_id "
            "WHERE m.signal_id=?", (str(signal["signal_id"]),),
        ).fetchone()
        if row is None or int(row[2]) != int(row[3]):
            return False
        return scope.permits(
            account=self.account.account_number, channel_id=int(row[0]),
            message_id=int(row[1]), revision_id=int(row[2]),
            contract=intent.contract, quantity=intent.quantity,
            limit=intent.limit_price, now=now,
        )

    def _authorize_submission(self, intent: OrderIntent) -> bool:
        """Re-check exact scope at the broker boundary. Exits are not blocked."""
        if intent.side is not OrderSide.BUY_TO_OPEN:
            return True
        scope = self.acceptance_scope
        if scope is None:
            return True
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(str(self.db_path))) as db:
            row = db.execute(
                "SELECT m.channel_id,m.source_message_id,m.first_revision_id,t.latest_revision_id,"
                "o.state,o.broker_order_id,(SELECT count(*) FROM order_intents WHERE side='BUY_TO_OPEN') "
                "FROM order_intents o JOIN signal_metadata m ON m.signal_id=o.signal_id "
                "JOIN telegram_messages t ON t.channel_id=m.channel_id AND t.message_id=m.source_message_id "
                "WHERE o.logical_order_id=?", (intent.logical_order_id,),
            ).fetchone()
        if row is None or int(row[2]) != int(row[3]):
            return False  # unknown intent or superseded Telegram revision
        if row[4] != "INTENT_RECORDED" or row[5] is not None or int(row[6]) != 1:
            return False  # duplicate / already-submitted / extra entry
        return scope.permits(
            account=self.account.account_number, channel_id=int(row[0]),
            message_id=int(row[1]), revision_id=int(row[2]),
            contract=intent.contract, quantity=intent.quantity,
            limit=intent.limit_price, now=self.clock(),
        )

    def _verify_supervised_journal(self) -> None:
        scope = self.acceptance_scope
        if scope is None:
            return
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(str(self.db_path))) as db:
            rows = db.execute(
                "SELECT o.side,o.requested_quantity,o.limit_price,s.underlying,s.option_right,s.strike,"
                "s.expiration_date,m.channel_id,m.source_message_id,m.first_revision_id "
                "FROM order_intents o LEFT JOIN option_signals s ON s.signal_id=o.signal_id "
                "LEFT JOIN signal_metadata m ON m.signal_id=o.signal_id"
            ).fetchall()
        from datetime import date

        from swift_autotrader.signals.identity import OptionRight
        buys = 0
        for row in rows:
            contract = OptionContract(row[3], OptionRight(row[4]), Decimal(row[5]), date.fromisoformat(row[6]))
            if (contract != scope.contract or int(row[7]) != scope.channel_id
                    or int(row[8]) != scope.message_id or int(row[9]) != scope.revision_id):
                raise PermissionError("Existing journal contains out-of-scope orders")
            if row[0] == "BUY_TO_OPEN":
                buys += 1
                if row[1] != scope.quantity or not Decimal("0") < Decimal(row[2]) <= scope.maximum_limit:
                    raise PermissionError("Existing buy differs from supervised receipt")
            elif row[0] != "SELL_TO_CLOSE":
                raise PermissionError("Unknown journal order side")
        if buys > 1 or (rows and buys != 1):
            raise PermissionError("Supervised journal contains ambiguous ownership")

    async def run_once(self) -> LiveDispatchResult:
        """Execute one complete live cycle."""
        self._verify_supervised_journal()
        now = self.clock()
        log.info("Starting live dispatch cycle at %s", now.isoformat())

        # Ensure live broker has event loop for threadsafe async bridging
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self.live_broker.loop = loop

        try:
            # 1. Watchdog: Cancel due unfilled orders off-thread
            watchdog = await asyncio.to_thread(
                self.engine.run_cycle,
                allow_entries=False,
                process_closes=False,
            )

            # Check durable acceptance state in database
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(str(self.db_path))) as con:
                row = con.execute("SELECT count(*) FROM order_intents WHERE side='BUY_TO_OPEN'").fetchone()
                existing_buys = int(row[0]) if row else 0

            # 2. Get pending Telegram updates and active positions
            matcher = SignalMatcher(self.db_path, self.channel_id)
            pending_entries, pending_followups, active = self._get_pending_and_active(now)

            target_revision_ids: list[int] | None = None
            if self.single_entry:
                allow_entries = False
                allowed_signal_ids: frozenset[str] | None = frozenset()
                if existing_buys > 0:
                    entries = []
                elif pending_entries:
                    target_rev_id, _mid, target_alert = pending_entries[0]
                    entries = [target_alert]
                    target_revision_ids = [target_rev_id]
                else:
                    entries = []
            else:
                allow_entries = True
                allowed_signal_ids = None
                entries = [p[2] for p in pending_entries]

            # 3. Fetch live market data snapshot from Robinhood MCP
            snapshot = None
            if entries or active:
                try:
                    snapshot = await asyncio.wait_for(
                        self.provider.snapshot(entries, active, now),
                        timeout=15.0,
                    )
                    snapshot.validate(now, max_age_seconds=30)
                except Exception as exc:
                    log.warning("Market snapshot unavailable: %s", exc)
                    self.notifications.record("APPLICATION", "MARKET_DATA_UNAVAILABLE", source_key="live-dispatch")
                    realized_loss = await asyncio.to_thread(
                        self.engine.cumulative_realized_daily_loss, now
                    )
                    return LiveDispatchResult(
                        status="MARKET_DATA_UNAVAILABLE",
                        parsed_events=0,
                        entry_orders_submitted=0,
                        close_orders_submitted=0,
                        cancellations=watchdog.cancellation_requests,
                        realized_daily_loss_usd=realized_loss,
                        open_positions_count=len(active),
                        unresolved_exit_count=await asyncio.to_thread(self.engine.unresolved_exit_count),
                    )

            # 4. Process parser decisions for verified contracts
            decisions = []
            exit_quotes: dict[OptionContract, ExitQuote] = {}
            if snapshot:
                self.preflight.instruments.clear()
                self.preflight.instruments.update(snapshot.instruments)
                catalog = StaticInstrumentCatalog(snapshot.instruments.keys())
                matcher.catalog = catalog
                if self.single_entry:
                    revisions_to_process = []
                    if target_revision_ids:
                        revisions_to_process.extend(target_revision_ids)
                    if pending_followups:
                        revisions_to_process.extend(pending_followups)
                    if revisions_to_process:
                        decisions = matcher.process_pending(limit=len(revisions_to_process), revision_ids=revisions_to_process)
                else:
                    decisions = matcher.process_pending(limit=200)

                for contract, quote in snapshot.quotes.items():
                    inst_check = snapshot.instruments.get(contract)
                    if inst_check and inst_check.market_session_open:
                        exit_quotes[contract] = ExitQuote(
                            contract, quote.bid, inst_check.premium_tick, quote.observed_at_utc
                        )

            # 5. Run the live execution cycle off-thread
            if self.single_entry:
                valid_sids = []
                if decisions and existing_buys == 0:
                    for d in decisions:
                        if d.intent == "ENTRY" and d.signal_id and d.status == "RECORDED":
                            with closing(sqlite3.connect(str(self.db_path))) as con:
                                sig = con.execute(
                                    "SELECT underlying FROM option_signals WHERE signal_id=?",
                                    (d.signal_id,),
                                ).fetchone()
                            if sig:
                                if self.authorized_symbol is not None and sig[0].upper() != self.authorized_symbol:
                                    log.warning(
                                        "Signal %s underlying %s does not match authorized %s",
                                        d.signal_id, sig[0], self.authorized_symbol,
                                    )
                                    continue
                                valid_sids.append(d.signal_id)
                if valid_sids:
                    allowed_signal_ids = frozenset([valid_sids[0]])
                    allow_entries = True
                else:
                    allowed_signal_ids = frozenset()
                    allow_entries = False

            cycle = await asyncio.to_thread(
                self.engine.run_cycle,
                quotes=exit_quotes,
                entry_quotes=snapshot.quotes if snapshot else None,
                allow_entries=allow_entries,
                process_closes=True,
                allowed_signal_ids=allowed_signal_ids,
            )

            # Dynamically calculate realized daily loss today
            realized_loss = await asyncio.to_thread(
                self.engine.cumulative_realized_daily_loss, now
            )

            return LiveDispatchResult(
                status="LIVE_CYCLE_COMPLETED",
                parsed_events=len(decisions),
                entry_orders_submitted=cycle.entry_submissions,
                close_orders_submitted=cycle.close_submissions,
                cancellations=watchdog.cancellation_requests + cycle.cancellation_requests,
                realized_daily_loss_usd=realized_loss,
                open_positions_count=len(active),
                unresolved_exit_count=await asyncio.to_thread(self.engine.unresolved_exit_count),
            )
        finally:
            self.live_broker.loop = None

    def _get_pending_and_active(
        self, now: datetime
    ) -> tuple[list[tuple[int, int, Any]], list[int], list[OptionContract]]:
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(str(self.db_path))) as db, db:
            pending = db.execute(
                "SELECT r.revision_id, r.message_id, e.body, r.eligible_for_future_processing, r.telegram_sent_at_utc "
                "FROM telegram_message_revisions r "
                "JOIN raw_events e ON e.event_id=r.raw_event_id "
                "LEFT JOIN signal_parser_decisions p ON p.revision_id=r.revision_id "
                "WHERE r.channel_id=? AND p.revision_id IS NULL "
                "ORDER BY r.revision_id ASC LIMIT 500",
                (str(self.channel_id),),
            ).fetchall()

            active_ids = active_signal_ids(db)
            active_rows = [
                row[1:] for row in db.execute(
                    "SELECT signal_id, underlying, option_right, strike, expiration_date "
                    "FROM option_signals"
                ).fetchall() if str(row[0]) in active_ids
            ]

        from datetime import date

        from swift_autotrader.signals.identity import OptionRight
        active = [
            OptionContract(row[0], OptionRight(row[1]), Decimal(row[2]), date.fromisoformat(row[3]))
            for row in active_rows
        ]
        entries = []
        followup_rev_ids = []
        for rid, mid, body, eligible, _sent_at in pending:
            alert = parse_message(body)
            if alert.intent is SignalIntent.ENTRY:
                if not eligible:
                    continue
                if self.single_entry:
                    if self.target_message_id is not None and mid != self.target_message_id:
                        continue
                    if self.acceptance_scope is not None and rid != self.acceptance_scope.revision_id:
                        continue
                    if (
                        self.authorized_symbol is not None
                        and (alert.underlying or "").upper() != self.authorized_symbol
                    ):
                        continue
                entries.append((rid, mid, alert))
            elif eligible or alert.intent is SignalIntent.CLOSE_100_PERCENT:
                # SignalMatcher verifies delayed exits against durable ownership,
                # source lineage and the entry timestamp. Never promote old buys.
                followup_rev_ids.append(rid)

        return entries, followup_rev_ids, active
