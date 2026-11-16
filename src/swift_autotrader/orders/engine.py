"""Restart-safe, PAPER-ONLY single-leg order execution and reconciliation.

No Telegram, network, Robinhood or live-broker dependencies. Only Phase 2's
persisted, unambiguous and fresh entry and close decisions are consumed.
All logical order intents are committed before a paper broker submission.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from swift_autotrader.broker.base import Broker, BrokerOrderState, BrokerOrderStatus
from swift_autotrader.broker.paper import PaperBroker, as_utc, dt, iso
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.orders.exit_policy import ExitPolicy, ExitQuote
from swift_autotrader.orders.policy import deadline_from_acceptance
from swift_autotrader.orders.risk import PreflightProvider, RiskCeilings, rejection_reason
from swift_autotrader.orders.slippage import SlippageExceededError, resolve_live_entry_limit
from swift_autotrader.orders.state_machine import (
    ACTIVE,
    TERMINAL,
    broker_transition_allowed,
)
from swift_autotrader.orders.watchdog import cancellation_due
from swift_autotrader.signals.identity import OptionContract, OptionRight

if TYPE_CHECKING:
    from swift_autotrader.paper_dispatch import VerifiedQuote


@contextmanager
def _cycle_lock(db_path: Path):
    """Host-local OS lock: prevent overlapping engines, release on process crash.

    Separate OS locks for Linux and Windows; fail closed on unsupported targets.
    Not designed for a network file share (Windows deployment must use local SSD).
    """
    filename = db_path.with_suffix(db_path.suffix + ".cycle.lock")
    with filename.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("paper engine cycle already running") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        elif os.name == "posix":
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("paper engine cycle already running") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        else:
            raise RuntimeError("unsupported paper-engine locking platform")


class ReconciliationError(RuntimeError):
    """Contradictory broker confirmation; operator review, never guess fills."""


@dataclass(frozen=True, slots=True)
class CycleResult:
    entry_submissions: int = 0
    close_submissions: int = 0
    cancellation_requests: int = 0
    blocked_entries: int = 0
    alerts: int = 0


class PaperOrderEngine:
    """Serial, idempotent, explicitly invoked paper execution cycle.

    Engine construction deliberately rejects anything except the local PaperBroker.
    Caller supplies trustworthy preflight, fresh exit quotes, tested UTC clock and
    explicit ceilings. Default gateway rejects every entry; no hidden live path.
    """

    def __init__(
        self, db_path: Path, broker: Broker, *,
        preflight: PreflightProvider, limits: RiskCeilings | None = None,
        exit_policy: ExitPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        live_mode: bool = False,
        entry_authorizer: Callable[[sqlite3.Connection, sqlite3.Row, OrderIntent, datetime], bool] | None = None,
    ) -> None:
        self.live_mode = live_mode
        self.entry_authorizer = entry_authorizer
        if not live_mode and type(broker) is not PaperBroker:
            raise TypeError("Phase 3 engine accepts ONLY the concrete PaperBroker when live_mode=False")
        self.db_path = Path(db_path)
        if getattr(broker, "db_path", None) is not None and broker.db_path.resolve() != self.db_path.resolve():
            raise ValueError("paper broker and order engine must share the same database")
        self.broker = broker
        self.preflight = preflight
        self.limits = limits or RiskCeilings()
        self.exit_policy = exit_policy or ExitPolicy()
        self.clock = clock or (lambda: datetime.now(UTC))
        with self._db() as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='paper_engine_control'"
            ).fetchone():
                raise RuntimeError("Phase 3 migration missing")

    @contextmanager
    def _db(self):
        if not self.db_path.is_file():
            raise RuntimeError("run migrate before using paper order engine")
        db = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _contract(row: sqlite3.Row) -> OptionContract:
        from datetime import date
        return OptionContract(
            str(row["underlying"]), OptionRight(str(row["option_right"])),
            Decimal(row["strike"]), date.fromisoformat(row["expiration_date"]),
        )

    @staticmethod
    def _key(signal_id: str, side: OrderSide) -> str:
        return hashlib.sha256(f"paper-v1:{signal_id}:{side.value}:0".encode()).hexdigest()

    def _alert(
        self, signal_id: str | None, order_id: str | None, code: str,
        now: datetime,
    ) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO paper_engine_alerts(" 
                "signal_id,logical_order_id,code,recorded_at_utc) VALUES(?,?,?,?)",
                (signal_id, order_id, code, iso(now)),
            )

    def _resolve_closed_exit_alerts(self, signal_id: str) -> None:
        """Clear stale missing-quote warnings only after actual full closure."""
        with self._db() as db:
            db.execute(
                "UPDATE paper_engine_alerts SET resolved=1 WHERE signal_id=? "
                "AND code IN ('EXIT_QUOTE_MISSING','EXIT_QUOTE_STALE',"
                "'EXIT_QUOTE_CONTRACT_MISMATCH','EXIT_QUOTE_INVALID',"
                "'EXIT_INVALID_TICK','EXIT_PRICE_UNKNOWN','UNFILLED_EXIT')",
                (signal_id,),
            )

    def _state(self, order_id: str, state: str, now: datetime, *,
               detail: str | None = None) -> None:
        """Append state change only after its corresponding durable status update."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM order_intents WHERE logical_order_id=?", (order_id,)
            ).fetchone()
            if row is None:
                raise ReconciliationError("missing durable order intent")
            if row["state"] != state:
                db.execute(
                    "UPDATE order_intents SET state=? WHERE logical_order_id=?",
                    (state, order_id),
                )
                db.execute(
                    "INSERT INTO order_state_events(logical_order_id,state,"
                    "recorded_at_utc,detail) VALUES(?,?,?,?)",
                    (order_id, state, iso(now), detail),
                )

    def _signal_state(self, signal_id: str, desired: str) -> None:
        with self._db() as db:
            row = db.execute(
                "SELECT state FROM option_signals WHERE signal_id=?", (signal_id,)
            ).fetchone()
            if row is None or row[0] == "CLOSED":
                return
            if desired in {"OPEN", "PARTIAL", "CANCELED", "REJECTED"}:
                closed = db.execute(
                    "SELECT 1 FROM signal_close_requests WHERE signal_id=?", (signal_id,)
                ).fetchone()
                if closed:
                    return
            db.execute(
                "UPDATE option_signals SET state=? WHERE signal_id=?",
                (desired, signal_id),
            )

    def _intent(self, signal: sqlite3.Row, side: OrderSide,
                quantity: int, price: Decimal, now: datetime) -> tuple[OrderIntent, bool]:
        signal_id = str(signal["signal_id"])
        order_id = f"{signal_id}:{'entry' if side is OrderSide.BUY_TO_OPEN else 'close'}"
        intent = OrderIntent(
            order_id, self._key(signal_id, side), self._contract(signal),
            side, quantity, price,
        )
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if side is OrderSide.BUY_TO_OPEN and db.execute(
                "SELECT 1 FROM signal_close_requests WHERE signal_id=?", (signal_id,),
            ).fetchone():
                raise ReconciliationError("close requested before opening buy")
            existing = db.execute(
                "SELECT requested_quantity,limit_price,side,idempotency_key "
                "FROM order_intents WHERE logical_order_id=?", (order_id,),
            ).fetchone()
            if existing:
                if (existing["requested_quantity"] != quantity or
                    Decimal(existing["limit_price"]) != price or
                    existing["side"] != side.value or
                    existing["idempotency_key"] != intent.idempotency_key):
                    raise ReconciliationError("existing order intent differs from request")
                return intent, False
            if (side is OrderSide.BUY_TO_OPEN and self.entry_authorizer is not None
                    and not self.entry_authorizer(db, signal, intent, as_utc(self.clock()))):
                raise PermissionError("Entry is outside supervised authorization or allowance consumed")
            db.execute(
                "INSERT INTO order_intents(logical_order_id,signal_id,idempotency_key,"
                "side,requested_quantity,filled_quantity,limit_price,state) "
                "VALUES(?,?,?,?,?,0,?,'INTENT_RECORDED')",
                (order_id, signal_id, intent.idempotency_key, side.value,
                 quantity, str(price)),
            )
            db.execute(
                "INSERT INTO order_state_events(logical_order_id,state,"
                "recorded_at_utc,detail) VALUES(?,'INTENT_RECORDED',?,?)",
                (order_id, iso(now), "LIVE" if self.live_mode else "PAPER_ONLY"),
            )
        return intent, True

    def _submit_existing(self, intent: OrderIntent, now: datetime) -> None:
        """Safe in paper mode: same durable key maps to exactly one broker order."""
        try:
            snapshot = self.broker.submit_order(intent)
            self._reconcile_one(intent.logical_order_id, now, snapshot=snapshot)
        except Exception:
            # An ambiguous response MUST NOT generate a fresh logical order/key.
            self._alert(intent.logical_order_id.split(":")[0],
                        intent.logical_order_id, "SUBMISSION_UNCERTAIN", now)
            raise

    def _reconcile_one(
        self, order_id: str, now: datetime, *, snapshot: BrokerOrderStatus | None = None,
    ) -> None:
        with self._db() as db:
            row = db.execute(
                "SELECT o.*,s.underlying,s.option_right,s.strike,s.expiration_date "
                "FROM order_intents o JOIN option_signals s ON s.signal_id=o.signal_id "
                "WHERE o.logical_order_id=?", (order_id,),
            ).fetchone()
        if row is None:
            raise ReconciliationError("unknown durable order")
        status = snapshot if snapshot is not None else self.broker.get_order(order_id)
        signal_id = str(row["signal_id"])
        if status.logical_order_id != order_id:
            raise ReconciliationError("broker logical order mismatch")
        if status.state is BrokerOrderState.UNKNOWN:
            self._alert(signal_id, order_id, "UNKNOWN_BROKER_ORDER", now)
            if self.live_mode:
                raise ReconciliationError("Unresolved live order; reconcile before continuing")
            return
        if (status.contract != self._contract(row) or
            status.side is not OrderSide(row["side"]) or
            status.requested_quantity != row["requested_quantity"] or
            status.limit_price != Decimal(row["limit_price"]) or
            status.idempotency_key != row["idempotency_key"]):
            raise ReconciliationError("broker contract, price or idempotency mismatch")
        if not broker_transition_allowed(str(row["state"]), status.state.value):
            raise ReconciliationError("invalid broker state regression")
        if status.filled_quantity != sum(fill.quantity for fill in status.fills):
            raise ReconciliationError("broker cumulative quantity disagrees with fills")
        if status.filled_quantity > int(row["requested_quantity"]):
            raise ReconciliationError("broker quantity exceeds order")
        if status.state is BrokerOrderState.FILLED and (
            status.filled_quantity != int(row["requested_quantity"])
        ):
            raise ReconciliationError("false fully filled status")
        if status.state in {BrokerOrderState.SUBMITTED, BrokerOrderState.ACCEPTED,
                            BrokerOrderState.PARTIALLY_FILLED, BrokerOrderState.FILLED,
                            BrokerOrderState.CANCEL_PENDING, BrokerOrderState.CANCELED
                            } and status.accepted_at_utc is None:
            raise ReconciliationError("broker acceptance timestamp missing")
        accepted = as_utc(status.accepted_at_utc) if status.accepted_at_utc else None
        former_accepted = dt(row["accepted_at_utc"])
        if former_accepted is not None and accepted != former_accepted:
            raise ReconciliationError("broker acceptance time changed")
        valid_ids: set[str] = set()
        for fill in status.fills:
            if (not fill.fill_id or fill.fill_id in valid_ids or fill.quantity <= 0 or
                not fill.price.is_finite() or fill.price <= 0):
                raise ReconciliationError("invalid broker fill")
            valid_ids.add(fill.fill_id)
            if accepted is None or as_utc(fill.filled_at_utc) < accepted:
                raise ReconciliationError("fill occurred before broker acceptance")
            if row["side"] == "BUY_TO_OPEN" and fill.price > Decimal(row["limit_price"]):
                raise ReconciliationError("buy fill above resting limit")
            if row["side"] == "SELL_TO_CLOSE" and fill.price < Decimal(row["limit_price"]):
                raise ReconciliationError("sell fill below resting limit")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            fresh = db.execute(
                "SELECT state,filled_quantity,accepted_at_utc FROM order_intents "
                "WHERE logical_order_id=?", (order_id,),
            ).fetchone()
            if fresh is None:
                raise ReconciliationError("intent disappeared")
            if not broker_transition_allowed(str(fresh["state"]), status.state.value):
                raise ReconciliationError("concurrent broker state regression")
            if status.filled_quantity < fresh["filled_quantity"]:
                raise ReconciliationError("broker cumulative fills regressed")
            # Idempotent fill history with conflict detection.
            for fill in status.fills:
                existing = db.execute(
                    "SELECT logical_order_id,filled_quantity,filled_price,filled_at_utc "
                    "FROM order_fills WHERE fill_id=?", (fill.fill_id,),
                ).fetchone()
                if existing:
                    if (existing["logical_order_id"] != order_id or
                        existing["filled_quantity"] != fill.quantity or
                        Decimal(existing["filled_price"]) != fill.price or
                        existing["filled_at_utc"] != iso(fill.filled_at_utc)):
                        raise ReconciliationError("conflicting duplicate broker fill ID")
                else:
                    db.execute(
                        "INSERT INTO order_fills(fill_id,logical_order_id,filled_quantity,"
                        "filled_price,filled_at_utc) VALUES(?,?,?,?,?)",
                        (fill.fill_id, order_id, fill.quantity, str(fill.price),
                         iso(fill.filled_at_utc)),
                    )
            all_fills = db.execute(
                "SELECT filled_quantity,filled_price FROM order_fills "
                "WHERE logical_order_id=?", (order_id,),
            ).fetchall()
            if sum(int(f[0]) for f in all_fills) != status.filled_quantity:
                raise ReconciliationError("broker history no longer matches durable ledger")
            expiry = iso(deadline_from_acceptance(accepted)) if (
                accepted and row["side"] == "BUY_TO_OPEN"
            ) else None
            db.execute(
                "UPDATE order_intents SET state=?,filled_quantity=?,"
                "accepted_at_utc=?,expires_at_utc=? WHERE logical_order_id=?",
                (status.state.value, status.filled_quantity,
                 iso(accepted) if accepted else None, expiry, order_id),
            )
            if status.broker_order_id:
                cursor = db.execute(
                    "UPDATE order_intents SET broker_order_id=? WHERE logical_order_id=? "
                    "AND (broker_order_id IS NULL OR broker_order_id=?)",
                    (status.broker_order_id, order_id, status.broker_order_id),
                )
                if cursor.rowcount != 1:
                    raise ReconciliationError("Conflicting durable broker order identity")
            if status.state.value != fresh["state"]:
                db.execute(
                    "INSERT INTO order_state_events(logical_order_id,state,"
                    "recorded_at_utc,detail) VALUES(?,?,?,?)",
                    (order_id, status.state.value, iso(now), "BROKER_CONFIRMED"),
                )
            if row["side"] == "BUY_TO_OPEN" and all_fills:
                total = sum(int(f[0]) for f in all_fills)
                average = sum(Decimal(f[1]) * int(f[0]) for f in all_fills) / total
                db.execute(
                    "UPDATE option_signals SET confirmed_avg_fill_price=? "
                    "WHERE signal_id=?", (str(average), signal_id),
                )
        if status.state is BrokerOrderState.REJECTED:
            self._alert(signal_id, order_id, "BROKER_REJECTED", now)
        if row["side"] == "BUY_TO_OPEN":
            if status.state is BrokerOrderState.FILLED:
                self._signal_state(signal_id, "OPEN")
            elif status.state is BrokerOrderState.PARTIALLY_FILLED:
                self._signal_state(signal_id, "PARTIAL")
            elif status.state is BrokerOrderState.REJECTED:
                self._signal_state(signal_id, "REJECTED")
            elif status.state is BrokerOrderState.CANCELED and status.filled_quantity == 0:
                self._signal_state(signal_id, "CANCELED")
        elif (
            status.state is BrokerOrderState.CANCELED
            and status.filled_quantity < int(row["requested_quantity"])
        ):
            self._alert(signal_id, order_id, "UNFILLED_EXIT", now)

    def _attempt_cancel(self, order_id: str, now: datetime) -> bool:
        snapshot = self.broker.get_order(order_id)
        self._reconcile_one(order_id, now, snapshot=snapshot)
        if snapshot.state not in {BrokerOrderState.SUBMITTED, BrokerOrderState.ACCEPTED, BrokerOrderState.PARTIALLY_FILLED}:
            return False
        try:
            self._reconcile_one(order_id, now, snapshot=self.broker.cancel_order(order_id))
        except Exception:
            self._alert(order_id.split(":")[0], order_id, "CANCEL_UNCERTAIN", now)
            raise
        return True

    def _control(self) -> tuple[bool, bool]:
        with self._db() as db:
            row = db.execute(
                "SELECT paused,emergency_stop FROM paper_engine_control WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise RuntimeError("missing persistent paper engine control")
        return bool(row[0]), bool(row[1])

    def set_paused(self, paused: bool, *, reason: str = "OPERATOR") -> None:
        with self._db() as db:
            db.execute(
                "UPDATE paper_engine_control SET paused=?,updated_at_utc=?,reason=? "
                "WHERE singleton=1", (int(paused), iso(self.clock()), reason[:120]),
            )

    def emergency_stop(self, *, reason: str = "OPERATOR_EMERGENCY_STOP") -> None:
        with self._db() as db:
            db.execute(
                "UPDATE paper_engine_control SET emergency_stop=1,paused=1,"
                "updated_at_utc=?,reason=? WHERE singleton=1",
                (iso(self.clock()), reason[:120]),
            )
        self.run_cycle()  # cancel outstanding entry remainder without opening buys.

    def reset_emergency_stop(self, *, approved: bool = False) -> None:
        if not approved:
            raise PermissionError("explicit manual approval required to clear kill switch")
        with self._db() as db:
            db.execute(
                "UPDATE paper_engine_control SET emergency_stop=0,paused=1,"
                "updated_at_utc=?,reason='MANUAL_RESET_STILL_PAUSED' WHERE singleton=1",
                (iso(self.clock()),),
            )

    def cancel_pending_buys(self, now: datetime | None = None) -> int:
        """Cancel all open or partially filled BUY_TO_OPEN order intents."""
        now_dt = as_utc(self.clock() if now is None else now)
        cancels = 0
        with _cycle_lock(self.db_path):
            with self._db() as db:
                entries = db.execute(
                    "SELECT logical_order_id, state FROM order_intents "
                    "WHERE side='BUY_TO_OPEN' ORDER BY logical_order_id"
                ).fetchall()
            for row in entries:
                if row["state"] in {"ACCEPTED", "PARTIALLY_FILLED"}:
                    if self._attempt_cancel(str(row["logical_order_id"]), now_dt):
                        cancels += 1
        return cancels

    def flatten_positions(self, now: datetime | None = None) -> list[OrderIntent]:
        """Emergency liquidate all active long positions and cancel open buys.

        Halts engine entry submissions, cancels outstanding entry orders via
        emergency_stop, and submits SELL_TO_CLOSE liquidation orders for all
        currently held open positions.
        """
        now_dt = as_utc(self.clock() if now is None else now)
        self.emergency_stop(reason="EMERGENCY_FLATTEN")

        submitted_intents: list[OrderIntent] = []
        with _cycle_lock(self.db_path):
            with self._db() as db:
                signals = db.execute(
                    "SELECT s.* FROM option_signals s "
                    "WHERE s.state NOT IN ('CLOSED', 'REJECTED')"
                ).fetchall()

            for sig in signals:
                sid = str(sig["signal_id"])
                buys, sold = self._summary(sid)
                if buys <= sold:
                    continue

                remaining = buys - sold
                price = Decimal(sig["original_limit"])

                with self._db() as db:
                    existing_close = db.execute(
                        "SELECT * FROM order_intents WHERE signal_id=? AND side='SELL_TO_CLOSE' "
                        "ORDER BY rowid DESC LIMIT 1",
                        (sid,),
                    ).fetchone()

                if existing_close is not None and existing_close["state"] in (
                    "ACCEPTED", "PARTIALLY_FILLED", "PENDING", "INTENT_RECORDED"
                ):
                    continue

                order_id = f"{sid}:close" if existing_close is None else f"{sid}:flatten"
                intent = OrderIntent(
                    order_id,
                    f"{self._key(sid, OrderSide.SELL_TO_CLOSE)}:{order_id}",
                    self._contract(sig),
                    OrderSide.SELL_TO_CLOSE,
                    remaining,
                    price,
                )

                with self._db() as db:
                    db.execute(
                        "INSERT INTO order_intents("
                        "logical_order_id,signal_id,idempotency_key,side,"
                        "requested_quantity,filled_quantity,limit_price,state) "
                        "VALUES(?,?,?,?,?,0,?,'INTENT_RECORDED')",
                        (
                            intent.logical_order_id,
                            sid,
                            intent.idempotency_key,
                            intent.side.value,
                            intent.quantity,
                            str(intent.limit_price),
                        ),
                    )
                    db.execute(
                        "INSERT INTO order_state_events(logical_order_id,state,"
                        "recorded_at_utc,detail) VALUES(?,'INTENT_RECORDED',?,?)",
                        (
                            intent.logical_order_id,
                            iso(now_dt),
                            "LIVE" if self.live_mode else "PAPER_ONLY",
                        ),
                    )

                self._signal_state(sid, "CLOSE_SUBMITTED")
                self._submit_existing(intent, now_dt)
                submitted_intents.append(intent)

        return submitted_intents

    def _summary(self, signal_id: str) -> tuple[int, int]:
        with self._db() as db:
            results = db.execute(
                "SELECT side,COALESCE(SUM(filled_quantity),0) "
                "FROM order_intents WHERE signal_id=? GROUP BY side", (signal_id,),
            ).fetchall()
        filled = {str(row[0]): int(row[1]) for row in results}
        return filled.get("BUY_TO_OPEN", 0), filled.get("SELL_TO_CLOSE", 0)

    def _outstanding_risk(self) -> tuple[Decimal, int]:
        with self._db() as db:
            signals = db.execute(
                "SELECT signal_id, original_limit, confirmed_avg_fill_price "
                "FROM option_signals WHERE state NOT IN ('CLOSED','REJECTED')"
            ).fetchall()
            held_total = Decimal("0")
            active_sids: set[str] = set()
            for sig in signals:
                sid = str(sig["signal_id"])
                buys, sold = self._summary(sid)
                if buys > sold:
                    active_sids.add(sid)
                    price = (
                        Decimal(sig["confirmed_avg_fill_price"])
                        if sig["confirmed_avg_fill_price"]
                        else Decimal(sig["original_limit"])
                    )
                    held_total += Decimal(buys - sold) * price * Decimal("100")

            pending_rows = db.execute(
                "SELECT signal_id, requested_quantity, filled_quantity, limit_price "
                "FROM order_intents WHERE side='BUY_TO_OPEN' "
                "AND state IN ('INTENT_RECORDED', 'SUBMITTED', 'ACCEPTED', "
                "'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNKNOWN', 'ALERT_REQUIRED')"
            ).fetchall()
            pending_total = Decimal("0")
            for row in pending_rows:
                sid = str(row["signal_id"])
                active_sids.add(sid)
                unfilled = int(row["requested_quantity"]) - int(row["filled_quantity"])
                if unfilled > 0:
                    pending_total += Decimal(unfilled) * Decimal(row["limit_price"]) * Decimal("100")

            total = held_total + pending_total
            return total, len(active_sids)

    def _block(self, signal_id: str, reason: str, now: datetime) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO paper_engine_decisions("
                "signal_id,outcome,reason,decided_at_utc) "
                "VALUES(?,'BLOCKED',?,?)", (signal_id, reason, iso(now)),
            )
        self._alert(signal_id, None, reason, now)

    def cumulative_realized_daily_loss(self, now: datetime | None = None) -> Decimal:
        """Calculate the total realized loss in USD from closed positions today.

        Uses FIFO matching of buy and sell fills. Returns a non-negative Decimal:
        positive if net realized daily PnL is negative (a loss), or 0 if positive/flat.
        """
        now_dt = as_utc(now or self.clock())
        today = now_dt.date()
        daily_pnl = Decimal("0")
        with self._db() as db:
            signals = db.execute(
                "SELECT DISTINCT signal_id FROM order_intents WHERE side='SELL_TO_CLOSE'"
            ).fetchall()
            for row in signals:
                sid = str(row[0])
                buys = db.execute(
                    "SELECT f.filled_quantity AS qty, f.filled_price AS price, f.filled_at_utc AS at "
                    "FROM order_fills f JOIN order_intents o ON o.logical_order_id=f.logical_order_id "
                    "WHERE o.signal_id=? AND o.side='BUY_TO_OPEN' ORDER BY f.filled_at_utc, f.fill_id",
                    (sid,),
                ).fetchall()
                sells = db.execute(
                    "SELECT f.filled_quantity AS qty, f.filled_price AS price, f.filled_at_utc AS at "
                    "FROM order_fills f JOIN order_intents o ON o.logical_order_id=f.logical_order_id "
                    "WHERE o.signal_id=? AND o.side='SELL_TO_CLOSE' ORDER BY f.filled_at_utc, f.fill_id",
                    (sid,),
                ).fetchall()
                fifo = [[int(f["qty"]), Decimal(f["price"])] for f in buys]
                for sell in sells:
                    qty = int(sell["qty"])
                    sell_price = Decimal(sell["price"])
                    sell_at = dt(sell["at"])
                    sell_is_today = (sell_at is not None and sell_at.date() == today)
                    while qty > 0:
                        if not fifo or fifo[0][0] == 0:
                            if fifo:
                                fifo.pop(0)
                                continue
                            break
                        portion = min(qty, fifo[0][0])
                        if sell_is_today:
                            gross = (sell_price - fifo[0][1]) * Decimal("100") * portion
                            daily_pnl += gross
                        fifo[0][0] -= portion
                        qty -= portion
        if daily_pnl < Decimal("0"):
            return -daily_pnl
        return Decimal("0")

    def _open_entry(
        self, signal: sqlite3.Row, now: datetime, *,
        entry_quotes: Mapping[OptionContract, VerifiedQuote] | None = None,
    ) -> bool:
        sid = str(signal["signal_id"])
        if self._control() != (False, False):
            return False
        with self._db() as db:
            previous = db.execute(
                "SELECT 1 FROM paper_engine_decisions WHERE signal_id=?", (sid,),
            ).fetchone()
            existing = db.execute(
                "SELECT 1 FROM order_intents WHERE signal_id=?", (sid,),
            ).fetchone()
            closed = db.execute(
                "SELECT 1 FROM signal_close_requests WHERE signal_id=?", (sid,),
            ).fetchone()
        if previous or existing or closed:
            return False
        received = dt(signal["created_at_utc"])
        if received is None:
            self._block(sid, "MISSING_SOURCE_TIMESTAMP", now)
            return False
        age = (now - received).total_seconds()
        if not 0 <= age <= self.limits.entry_max_age_seconds:
            self._block(sid, "STALE_ENTRY_NO_REPLAY", now)
            return False
        contract = self._contract(signal)
        if contract.expiration < now.date():
            self._block(sid, "EXPIRED_OPTION", now)
            return False
        with self._db() as db:
            possible_duplicates = db.execute(
                "SELECT s.strike FROM option_signals s JOIN order_intents o ON o.signal_id=s.signal_id "
                "WHERE s.underlying=? AND s.option_right=? AND s.expiration_date=? "
                "AND s.signal_id != ? AND s.state NOT IN ('CLOSED','REJECTED') "
                "AND o.side='BUY_TO_OPEN' "
                "AND (o.state NOT IN ('REJECTED','CANCELED') OR o.filled_quantity > 0)",
                (contract.underlying, contract.right.value,
                 contract.expiration.isoformat(), sid),
            ).fetchall()
        if any(Decimal(row["strike"]) == contract.strike for row in possible_duplicates):
            self._block(sid, "DUPLICATE_CONTRACT_POSITION", now)
            return False
        price = Decimal(signal["original_limit"])
        preflight = self.preflight.check(contract)

        if entry_quotes and contract in entry_quotes:
            entry_quote = entry_quotes[contract]
            tick = (
                preflight.premium_tick
                if (preflight.premium_tick and preflight.premium_tick > 0)
                else Decimal("0.01")
            )
            try:
                price = resolve_live_entry_limit(
                    alert_limit=price,
                    current_ask=entry_quote.ask,
                    tick=tick,
                    max_slippage_pct=self.limits.slippage_tolerance_pct,
                )
            except SlippageExceededError:
                self._block(sid, "SLIPPAGE_EXCEEDED", now)
                return False
            except (ValueError, ArithmeticError):
                self._block(sid, "INVALID_ENTRY_QUOTE", now)
                return False

        quantity = self.limits.contracts_per_signal
        outstanding, count = self._outstanding_risk()
        cumulative_loss = self.cumulative_realized_daily_loss(now)
        reason = rejection_reason(
            contract, price, quantity, self.limits,
            preflight, outstanding_usd=outstanding,
            active_signals=count,
            cumulative_realized_loss_usd=cumulative_loss,
        )
        if reason:
            self._block(sid, reason, now)
            return False
        intent, created = self._intent(signal, OrderSide.BUY_TO_OPEN, quantity, price, now)
        if not created:
            return False
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO paper_engine_decisions(signal_id,outcome,decided_at_utc) "
                "VALUES(?,'INTENT_RECORDED',?)", (sid, iso(now)),
            )
        self._submit_existing(intent, now)
        return True

    def _close_signal(
        self, signal: sqlite3.Row, now: datetime,
        quotes: Mapping[OptionContract, ExitQuote],
    ) -> tuple[bool, bool]:
        sid = str(signal["signal_id"])
        with self._db() as db:
            entry = db.execute(
                "SELECT * FROM order_intents WHERE signal_id=? AND side='BUY_TO_OPEN'",
                (sid,),
            ).fetchone()
            exit_order = db.execute(
                "SELECT * FROM order_intents WHERE signal_id=? AND side='SELL_TO_CLOSE'",
                (sid,),
            ).fetchone()
        if entry is not None and entry["state"] in ACTIVE:
            did_cancel = self._attempt_cancel(str(entry["logical_order_id"]), now)
            with self._db() as db:
                entry = db.execute(
                    "SELECT * FROM order_intents WHERE logical_order_id=?",
                    (entry["logical_order_id"],),
                ).fetchone()
            if entry["state"] in ACTIVE:
                # Wait for terminal confirmation; a fill can race the cancel.
                self._signal_state(sid, "CLOSE_REQUESTED")
                return False, did_cancel
        buys, sold = self._summary(sid)
        if sold > buys:
            raise ReconciliationError("sell fills exceed actual buy fills")
        if buys == sold:
            self._signal_state(sid, "CLOSED")
            self._resolve_closed_exit_alerts(sid)
            if entry is None:
                with self._db() as db:
                    db.execute(
                        "INSERT OR IGNORE INTO paper_engine_decisions(signal_id,outcome,"
                        "reason,decided_at_utc) VALUES(?,'CLOSED_NO_FILL',?,?)",
                        (sid, "CLOSE_BEFORE_ENTRY", iso(now)),
                    )
            return False, False
        if exit_order is not None:
            if exit_order["state"] == "FILLED" and sold == buys:
                self._signal_state(sid, "CLOSED")
            elif exit_order["state"] in TERMINAL and sold < buys:
                self._signal_state(sid, "ALERT_REQUIRED")
                self._alert(sid, str(exit_order["logical_order_id"]),
                            "UNFILLED_EXIT", now)
            else:
                self._signal_state(sid, "PARTIAL_CLOSE" if sold else "CLOSE_SUBMITTED")
            return False, False
        contract = self._contract(signal)
        price, problem = self.exit_policy.choose_limit(contract, quotes.get(contract), now)
        if problem is not None or price is None:
            self._alert(sid, None, problem or "EXIT_PRICE_UNKNOWN", now)
            self._signal_state(sid, "ALERT_REQUIRED")
            return False, False
        intent, created = self._intent(
            signal, OrderSide.SELL_TO_CLOSE, buys - sold, price, now,
        )
        if not created:
            return False, False
        self._signal_state(sid, "CLOSE_SUBMITTED")
        self._submit_existing(intent, now)
        return True, False

    def run_cycle(
        self, *, quotes: Mapping[OptionContract, ExitQuote] | None = None,
        entry_quotes: Mapping[OptionContract, VerifiedQuote] | None = None,
        allow_entries: bool = True, process_closes: bool = True,
        allowed_signal_ids: frozenset[str] | None = None,
    ) -> CycleResult:
        with _cycle_lock(self.db_path):
            return self._run_cycle_locked(
                quotes=quotes, entry_quotes=entry_quotes, allow_entries=allow_entries,
                process_closes=process_closes, allowed_signal_ids=allowed_signal_ids,
            )

    def _run_cycle_locked(
        self, *, quotes: Mapping[OptionContract, ExitQuote] | None = None,
        entry_quotes: Mapping[OptionContract, VerifiedQuote] | None = None,
        allow_entries: bool = True, process_closes: bool = True,
        allowed_signal_ids: frozenset[str] | None = None,
    ) -> CycleResult:
        now = as_utc(self.clock())
        quotes = dict(quotes or {})
        entry_submissions = close_submissions = cancels = blocked = 0
        with self._db() as db:
            orders = db.execute(
                "SELECT logical_order_id FROM order_intents "
                "ORDER BY logical_order_id"
            ).fetchall()
        for row in orders:
            try:
                self._reconcile_one(str(row[0]), now)
            except Exception:
                self._alert(str(row[0]).split(":")[0], str(row[0]),
                            "RECONCILIATION_REQUIRED", now)
                raise
        paused, stopped = self._control()
        with self._db() as db:
            entries = db.execute(
                "SELECT logical_order_id,accepted_at_utc,state FROM order_intents "
                "WHERE side='BUY_TO_OPEN' ORDER BY logical_order_id"
            ).fetchall()
        for row in entries:
            if (
                row["state"] in {"ACCEPTED", "PARTIALLY_FILLED"}
                and (stopped or cancellation_due(dt(row["accepted_at_utc"]), now))
                and self._attempt_cancel(str(row["logical_order_id"]), now)
            ):
                cancels += 1
        with self._db() as db:
            closing = db.execute(
                "SELECT s.* FROM option_signals s "
                "JOIN signal_close_requests c ON c.signal_id=s.signal_id "
                "ORDER BY c.first_revision_id"
            ).fetchall()
        if process_closes:
            for signal in closing:
                submitted, cancelled = self._close_signal(signal, now, quotes)
                close_submissions += int(submitted)
                cancels += int(cancelled)
        if allow_entries and not paused and not stopped:
            with self._db() as db:
                candidates = db.execute(
                    "SELECT s.* FROM option_signals s "
                    "JOIN signal_metadata m ON m.signal_id=s.signal_id "
                    "JOIN signal_parser_decisions p ON p.revision_id=m.first_revision_id "
                    "JOIN telegram_message_revisions r ON r.revision_id=p.revision_id "
                    "WHERE p.intent='ENTRY' AND p.status='RECORDED' "
                    "AND p.reason='ENTRY_VALIDATED_NOT_EXECUTED' "
                    "AND r.eligible_for_future_processing=1 "
                    "AND NOT EXISTS(SELECT 1 FROM signal_close_requests c "
                    "WHERE c.signal_id=s.signal_id) "
                    "AND NOT EXISTS(SELECT 1 FROM paper_engine_decisions d "
                    "WHERE d.signal_id=s.signal_id) "
                    "ORDER BY m.first_revision_id"
                ).fetchall()
            for signal in candidates:
                # An unrelated verified alert may never mark this signal
                # permanently BLOCKED while its own quote remains missing.
                if (
                    allowed_signal_ids is not None
                    and str(signal["signal_id"]) not in allowed_signal_ids
                ):
                    continue
                if self._open_entry(signal, now, entry_quotes=entry_quotes):
                    entry_submissions += 1
                else:
                    with self._db() as db:
                        blocked += int(bool(db.execute(
                            "SELECT 1 FROM paper_engine_decisions WHERE signal_id=? "
                            "AND outcome='BLOCKED'", (signal["signal_id"],),
                        ).fetchone()))
        with self._db() as db:
            alert_count = db.execute(
                "SELECT COUNT(*) FROM paper_engine_alerts WHERE resolved=0"
            ).fetchone()[0]
        return CycleResult(entry_submissions, close_submissions, cancels,
                           blocked, int(alert_count))

    def health(self) -> dict[str, object]:
        with self._db() as db:
            controls = db.execute(
                "SELECT paused,emergency_stop FROM paper_engine_control WHERE singleton=1"
            ).fetchone()
            counts = db.execute(
                "SELECT state,COUNT(*) FROM order_intents GROUP BY state"
            ).fetchall()
            alerts = db.execute(
                "SELECT code,COUNT(*) FROM paper_engine_alerts "
                "WHERE resolved=0 GROUP BY code"
            ).fetchall()
        return {
            "mode": "LIVE" if self.live_mode else "PAPER_ONLY",
            "broker_writes_enabled": self.live_mode,
            "paused": bool(controls[0]), "emergency_stop": bool(controls[1]),
            "orders_by_state": {str(x[0]): int(x[1]) for x in counts},
            "open_alerts": {str(x[0]): int(x[1]) for x in alerts},
        }
