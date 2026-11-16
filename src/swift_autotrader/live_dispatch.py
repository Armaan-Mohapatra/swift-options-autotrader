"""Unattended LIVE Trading Dispatcher for SwiftTrades -> Robinhood Agentic MCP.

Coordinates the end-to-end pipeline:
1. Ingests Telegram signals from the local SQLite store.
2. Resolves option contracts, expirations, and live quotes from Robinhood.
3. Applies slippage-tolerant limit pricing within configured bounds.
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
from time import monotonic

from swift_autotrader.broker.base import BrokerOrderState
from swift_autotrader.broker.mcp_paper_data import MCPReadOnlyOptionData
from swift_autotrader.broker.robinhood_mcp import (
    MCPAccount,
    RobinhoodLiveBroker,
    RobinhoodMCPBroker,
)
from swift_autotrader.config import Settings
from swift_autotrader.notifications.alerts import NotificationCenter
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.exit_policy import ExitQuote
from swift_autotrader.orders.risk import Preflight, RiskCeilings
from swift_autotrader.orders.slippage import resolve_live_entry_limit, SlippageExceededError
from swift_autotrader.paper_dispatch import (
    MarketEvidenceUnavailable,
    MarketSnapshot,
    VerifiedQuote,
)
from swift_autotrader.parser.rules import SignalIntent, parse_message
from swift_autotrader.signals.identity import OptionContract
from swift_autotrader.signals.lineage import SignalMatcher, StaticInstrumentCatalog, resolve_entry

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
    ):
        if not mcp_broker.allow_live_writes:
            raise PermissionError("LiveDispatcher requires mcp_broker.allow_live_writes=True")
        self.db_path = Path(db_path)
        self.channel_id = channel_id
        self.mcp = mcp_broker
        self.account = account
        self.provider = provider
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))

        # Risk limits derived from settings
        self.limits = RiskCeilings(
            contracts_per_signal=settings.max_contracts_per_signal,
            premium_per_signal_usd=settings.max_premium_per_signal_usd or Decimal("9999"),
            total_exposure_usd=Decimal("10000"),
            open_signal_count=settings.max_concurrent_positions,
            entry_max_age_seconds=settings.telegram_max_live_age_seconds,
            max_daily_loss_usd=settings.max_daily_loss_usd,
            max_concurrent_positions=settings.max_concurrent_positions,
            slippage_tolerance_pct=settings.slippage_tolerance_pct,
        )

        # Build live broker adapter
        self.live_broker = RobinhoodLiveBroker(self.mcp, self.account, loop=loop)

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
        )
        self.notifications = NotificationCenter(self.db_path)

    async def run_once(self) -> LiveDispatchResult:
        """Execute one complete live cycle."""
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

            # 2. Get pending Telegram updates and active positions
            matcher = SignalMatcher(self.db_path, self.channel_id)
            entries, active = self._get_pending_and_active(now)

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
                    )

            # 4. Process parser decisions for verified contracts
            decisions = []
            exit_quotes: dict[OptionContract, ExitQuote] = {}
            if snapshot:
                self.preflight.instruments.clear()
                self.preflight.instruments.update(snapshot.instruments)
                catalog = StaticInstrumentCatalog(snapshot.instruments.keys())
                matcher.catalog = catalog
                decisions = matcher.process_pending(limit=200)

                for contract, quote in snapshot.quotes.items():
                    inst_check = snapshot.instruments.get(contract)
                    if inst_check and inst_check.market_session_open:
                        exit_quotes[contract] = ExitQuote(
                            contract, quote.bid, inst_check.premium_tick, quote.observed_at_utc
                        )

            # 5. Run the live execution cycle off-thread
            cycle = await asyncio.to_thread(
                self.engine.run_cycle,
                quotes=exit_quotes,
                entry_quotes=snapshot.quotes if snapshot else None,
                allow_entries=True,
                process_closes=True,
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
            )
        finally:
            self.live_broker.loop = None

    def _get_pending_and_active(self, now: datetime) -> tuple[list[Any], list[OptionContract]]:
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(str(self.db_path))) as db, db:
            pending = db.execute(
                "SELECT r.revision_id, e.body, r.eligible_for_future_processing, r.telegram_sent_at_utc "
                "FROM telegram_message_revisions r "
                "JOIN raw_events e ON e.event_id=r.raw_event_id "
                "LEFT JOIN signal_parser_decisions p ON p.revision_id=r.revision_id "
                "WHERE r.channel_id=? AND p.revision_id IS NULL "
                "ORDER BY r.revision_id DESC LIMIT 500",
                (str(self.channel_id),),
            ).fetchall()

            active_rows = db.execute(
                "SELECT DISTINCT s.underlying, s.option_right, s.strike, s.expiration_date "
                "FROM option_signals s JOIN order_intents o ON o.signal_id=s.signal_id "
                "WHERE o.side='BUY_TO_OPEN' AND o.state IN ('ACCEPTED','PARTIALLY_FILLED','FILLED') "
                "AND s.state NOT IN ('CLOSED','REJECTED','CANCELED')"
            ).fetchall()

        from datetime import date
        from swift_autotrader.signals.identity import OptionRight
        active = [
            OptionContract(row[0], OptionRight(row[1]), Decimal(row[2]), date.fromisoformat(row[3]))
            for row in active_rows
        ]
        entries = []
        for _rid, body, eligible, sent_at in reversed(pending):
            if eligible:
                alert = parse_message(body)
                if alert.intent is SignalIntent.ENTRY:
                    entries.append(alert)

        return entries, active
