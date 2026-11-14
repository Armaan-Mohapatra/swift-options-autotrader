"""Durable deterministic PAPER adapter. No network, quotes or invented fills.

Test/operator-injected paper fills are separate immutable rows. Cancel can remain
pending to simulate a fill racing with the cancellation acknowledgment.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from swift_autotrader.broker.base import BrokerFill, BrokerOrderState, BrokerOrderStatus
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.signals.identity import OptionContract, OptionRight


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware UTC instant required")
    return value.astimezone(UTC)


def iso(value: datetime) -> str:
    return as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


@dataclass(frozen=True, slots=True)
class PaperMarketQuote:
    """Externally supplied test observation; never fetched from a live exchange."""
    quote_id: str
    contract: OptionContract
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    observed_at_utc: datetime


class PaperBroker:
    """Paper-only journal; require pre-migrated database and controlled clock.

    `manual_fill` is strictly simulation input, never inferred from Swift alerts.
    Recreating the adapter with the same file reuses broker confirmations/fills.
    """

    def __init__(
        self, db_path: Path, *, clock: Callable[[], datetime] | None = None,
        cancel_pending: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.cancel_pending = cancel_pending
        if not self.db_path.is_file():
            raise RuntimeError("run migrate before creating paper broker")
        with closing(self._db()) as db, db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='paper_broker_orders'"
            ).fetchone():
                raise RuntimeError("Phase 3 migration missing")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _same_order(row: sqlite3.Row, intent: OrderIntent) -> bool:
        return (
            row["logical_order_id"] == intent.logical_order_id
            and row["idempotency_key"] == intent.idempotency_key
            and row["signal_id"] == intent.logical_order_id.split(":")[0]
            and row["underlying"] == intent.contract.underlying
            and row["option_right"] == intent.contract.right.value
            and Decimal(row["strike"]) == intent.contract.strike
            and row["expiration_date"] == intent.contract.expiration.isoformat()
            and row["side"] == intent.side.value
            and row["quantity"] == intent.quantity
            and Decimal(row["limit_price"]) == intent.limit_price
        )

    def submit_order(self, intent: OrderIntent) -> BrokerOrderStatus:
        now = iso(self.clock())
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM paper_broker_orders "
                "WHERE logical_order_id=? OR idempotency_key=?",
                (intent.logical_order_id, intent.idempotency_key),
            ).fetchone()
            if existing is not None:
                if not self._same_order(existing, intent):
                    raise ValueError("idempotency key reused for a different order")
            else:
                db.execute(
                    "INSERT INTO paper_broker_orders(logical_order_id,idempotency_key,"
                    "signal_id,underlying,option_right,strike,expiration_date,side,"
                    "quantity,limit_price,state,accepted_at_utc,created_at_utc)"
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (intent.logical_order_id, intent.idempotency_key,
                     intent.logical_order_id.split(":")[0], intent.contract.underlying,
                     intent.contract.right.value, str(intent.contract.strike),
                     intent.contract.expiration.isoformat(), intent.side.value,
                     intent.quantity, str(intent.limit_price), "ACCEPTED", now, now),
                )
        return self.get_order(intent.logical_order_id)

    def get_order(self, logical_order_id: str) -> BrokerOrderStatus:
        with closing(self._db()) as db, db:
            row = db.execute(
                "SELECT * FROM paper_broker_orders WHERE logical_order_id=?", (logical_order_id,)
            ).fetchone()
            if row is None:
                return BrokerOrderStatus(logical_order_id, BrokerOrderState.UNKNOWN, 0, None)
            fills = tuple(
                BrokerFill(str(f["fill_id"]), int(f["quantity"]), Decimal(f["price"]),
                           dt(f["filled_at_utc"]))
                for f in db.execute(
                    "SELECT * FROM paper_broker_fills WHERE logical_order_id=? "
                    "ORDER BY filled_at_utc,fill_id", (logical_order_id,),
                )
            )
            return BrokerOrderStatus(
                logical_order_id, BrokerOrderState(row["state"]),
                sum(x.quantity for x in fills), dt(row["accepted_at_utc"]), fills,
                row["reject_reason"],
                OptionContract(row["underlying"], OptionRight(row["option_right"]),
                               Decimal(row["strike"]),
                               date.fromisoformat(row["expiration_date"])),
                OrderSide(row["side"]), int(row["quantity"]),
                Decimal(row["limit_price"]), str(row["idempotency_key"]),
            )

    def cancel_order(self, logical_order_id: str) -> BrokerOrderStatus:
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM paper_broker_orders WHERE logical_order_id=?",
                (logical_order_id,),
            ).fetchone()
            if row and row["state"] in {"ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING"}:
                db.execute(
                    "UPDATE paper_broker_orders SET state=? WHERE logical_order_id=?",
                    ("CANCEL_PENDING" if self.cancel_pending else "CANCELED", logical_order_id),
                )
        return self.get_order(logical_order_id)

    def confirm_cancel(self, logical_order_id: str) -> BrokerOrderStatus:
        """Simulate independent broker confirmation, possibly after a late fill."""
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE paper_broker_orders SET state='CANCELED' "
                "WHERE logical_order_id=? AND state='CANCEL_PENDING'",
                (logical_order_id,),
            )
        return self.get_order(logical_order_id)

    def manual_fill(
        self, logical_order_id: str, *, fill_id: str, quantity: int,
        price: Decimal, at: datetime | None = None,
    ) -> BrokerOrderStatus:
        """Inject a simulated fill with a stable fill ID. Never auto-mark-to-market."""
        if not fill_id or quantity <= 0 or not price.is_finite() or price <= 0:
            raise ValueError("invalid simulated fill")
        when = as_utc(at or self.clock())
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM paper_broker_orders WHERE logical_order_id=?",
                (logical_order_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown simulated order")
            duplicate = db.execute(
                "SELECT * FROM paper_broker_fills WHERE fill_id=?", (fill_id,)
            ).fetchone()
            if duplicate:
                if (duplicate["logical_order_id"], duplicate["quantity"],
                    Decimal(duplicate["price"]), duplicate["filled_at_utc"]) != (
                    logical_order_id, quantity, price, iso(when)
                ):
                    raise ValueError("conflicting duplicate fill identifier")
                return self.get_order(logical_order_id)
            if row["state"] not in {"ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING"}:
                raise ValueError("cannot fill a terminal order")
            if when < dt(row["accepted_at_utc"]):
                raise ValueError("fill predates broker acceptance")
            if (row["side"] == OrderSide.BUY_TO_OPEN.value and
                price > Decimal(row["limit_price"])) or (
                row["side"] == OrderSide.SELL_TO_CLOSE.value and
                price < Decimal(row["limit_price"])
            ):
                raise ValueError("simulated fill breaches limit price")
            prior = db.execute(
                "SELECT COALESCE(SUM(quantity),0) FROM paper_broker_fills "
                "WHERE logical_order_id=?", (logical_order_id,),
            ).fetchone()[0]
            if prior + quantity > int(row["quantity"]):
                raise ValueError("simulated overfill")
            db.execute(
                "INSERT INTO paper_broker_fills(fill_id,logical_order_id,quantity,price,"
                "filled_at_utc) VALUES(?,?,?,?,?)",
                (fill_id, logical_order_id, quantity, str(price), iso(when)),
            )
            db.execute(
                "UPDATE paper_broker_orders SET state=? WHERE logical_order_id=?",
                ("FILLED" if prior + quantity == int(row["quantity"]) else
                 "CANCEL_PENDING" if row["state"] == "CANCEL_PENDING" else
                 "PARTIALLY_FILLED", logical_order_id),
            )
        return self.get_order(logical_order_id)

    def observe_quote(
        self, quote: PaperMarketQuote, *, max_age_seconds: int = 15,
        slippage: Decimal = Decimal(0), min_improvement: Decimal = Decimal(0),
    ) -> int:
        """Deterministic simulated quote crossing, atomic and idempotent.

        Prices and displayed liquidity are explicit synthetic inputs. A BUY can
        fill only at quoted ASK <= original limit; a SELL only when BID >= limit.
        No quote means no fill. The entire event commits in one SQLite write.
        """
        if any(not x.is_finite() or x < 0 for x in (slippage, min_improvement)):
            raise ValueError("invalid price execution assumption")
        when = as_utc(quote.observed_at_utc)
        age = (as_utc(self.clock()) - when).total_seconds()
        if not quote.quote_id or not 0 <= age <= max_age_seconds:
            raise ValueError("stale, future or unidentified market quote")
        if (not quote.bid.is_finite() or not quote.ask.is_finite() or
            quote.bid <= 0 or quote.ask <= 0 or quote.bid > quote.ask or
            quote.bid_size < 0 or quote.ask_size < 0):
            raise ValueError("invalid paper bid/ask or displayed depth")
        fingerprint = hashlib.sha256((
            f"{quote.contract!r}|{quote.bid}|{quote.ask}|{quote.bid_size}|"
            f"{quote.ask_size}|{iso(when)}"
        ).encode()).hexdigest()
        count = 0
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT quote_fingerprint FROM paper_quote_observations WHERE quote_id=?",
                (quote.quote_id,),
            ).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise ValueError("quote ID reused for different observation")
                return 0
            db.execute(
                "INSERT INTO paper_quote_observations(quote_id,quote_fingerprint,"
                "observed_at_utc) VALUES(?,?,?)",
                (quote.quote_id, fingerprint, iso(when)),
            )
            orders = db.execute(
                "SELECT * FROM paper_broker_orders WHERE underlying=? "
                "AND option_right=? AND strike=? AND expiration_date=? "
                "AND state IN ('ACCEPTED','PARTIALLY_FILLED') "
                "AND accepted_at_utc<=? ORDER BY accepted_at_utc,logical_order_id",
                (quote.contract.underlying, quote.contract.right.value,
                 str(quote.contract.strike), quote.contract.expiration.isoformat(),
                 iso(when)),
            ).fetchall()
            remaining_ask = quote.ask_size
            remaining_bid = quote.bid_size
            for order in orders:
                buy = order["side"] == "BUY_TO_OPEN"
                price = quote.ask + slippage if buy else quote.bid - slippage
                if price <= 0 or (buy and price + min_improvement > Decimal(
                    order["limit_price"]
                )) or (not buy and price - min_improvement < Decimal(
                    order["limit_price"]
                )):
                    continue
                filled = int(db.execute(
                    "SELECT COALESCE(SUM(quantity),0) FROM paper_broker_fills "
                    "WHERE logical_order_id=?", (order["logical_order_id"],),
                ).fetchone()[0])
                available = remaining_ask if buy else remaining_bid
                quantity = min(int(order["quantity"]) - filled, available)
                if quantity <= 0:
                    continue
                if buy:
                    remaining_ask -= quantity
                else:
                    remaining_bid -= quantity
                fill_id = hashlib.sha256((
                    f"quote:{quote.quote_id}:{order['logical_order_id']}"
                ).encode()).hexdigest()
                db.execute(
                    "INSERT INTO paper_broker_fills(fill_id,logical_order_id,quantity,"
                    "price,filled_at_utc) VALUES(?,?,?,?,?)",
                    (fill_id, order["logical_order_id"], quantity, str(price), iso(when)),
                )
                db.execute(
                    "UPDATE paper_broker_orders SET state=? WHERE logical_order_id=?",
                    ("FILLED" if filled + quantity == order["quantity"] else
                     "PARTIALLY_FILLED", order["logical_order_id"]),
                )
                count += quantity
        return count

    def force_reject(self, logical_order_id: str, *, reason: str = "PAPER_REJECTION") -> None:
        """Explicit test-only rejection, before fills; never fabricate success."""
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM paper_broker_orders WHERE logical_order_id=?",
                (logical_order_id,),
            ).fetchone()
            if row is None or row["state"] != "ACCEPTED":
                raise ValueError("only unfilled accepted order can be rejected")
            db.execute(
                "UPDATE paper_broker_orders SET state='REJECTED',reject_reason=? "
                "WHERE logical_order_id=?", (reason, logical_order_id),
            )

    def close_session(self) -> int:
        """Simulate exchange GFD cancellation before hour expiry; no resubmission."""
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE paper_broker_orders SET state='CANCELED' "
                "WHERE state IN ('ACCEPTED','PARTIALLY_FILLED','CANCEL_PENDING')"
            )
            return int(db.execute("SELECT changes()").fetchone()[0])
