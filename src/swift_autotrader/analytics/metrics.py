"""Read-only metrics from confirmed simulated fills; no invented market prices.

No Robinhood client, paid Telegram text, or network dependence. Decimal arithmetic
retains option-premium precision. Standard US equity option multiplier is explicit.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from swift_autotrader.broker.paper import as_utc, dt, iso

STANDARD_OPTION_MULTIPLIER = Decimal(100)
ZERO = Decimal(0)


def amount(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class CostModel:
    """Explicit costs; None means net performance is unknown (not zero)."""

    fee_per_contract_side_usd: Decimal | None = None
    slippage_per_contract_premium: Decimal = ZERO
    fill_assumption: str = "touch"
    mark_max_age_seconds: int = 60

    def __post_init__(self) -> None:
        if self.fee_per_contract_side_usd is not None and (
            not self.fee_per_contract_side_usd.is_finite()
            or self.fee_per_contract_side_usd < 0
        ):
            raise ValueError("invalid simulated fee")
        if (not self.slippage_per_contract_premium.is_finite()
                or self.slippage_per_contract_premium < 0):
            raise ValueError("invalid modeled slippage")
        if self.fill_assumption not in {"touch", "through", "no_fills"}:
            raise ValueError("unrecognized execution assumption")
        if not 0 <= self.mark_max_age_seconds <= 86400:
            raise ValueError("invalid quote freshness policy")


class PaperAnalytics:
    """Conservative read-only snapshot; missing quotes produce null valuations."""

    def __init__(self, db_path: Path, *, costs: CostModel | None = None) -> None:
        self.db_path = Path(db_path)
        self.costs = costs or CostModel()

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise FileNotFoundError("paper database does not exist")
        con = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _filled(db: sqlite3.Connection, signal_id: str, side: str) -> list[sqlite3.Row]:
        return db.execute(
            "SELECT f.filled_quantity AS qty,f.filled_price AS price,f.filled_at_utc AS at, "
            "o.limit_price AS limit_price "
            "FROM order_fills f JOIN order_intents o ON o.logical_order_id=f.logical_order_id "
            "WHERE o.signal_id=? AND o.side=? ORDER BY f.filled_at_utc,f.fill_id",
            (signal_id, side),
        ).fetchall()

    @staticmethod
    def _last_quote(db: sqlite3.Connection, sig: sqlite3.Row,
                    now: datetime) -> sqlite3.Row | None:
        return db.execute(
            "SELECT bid,ask,observed_at_utc,provenance FROM paper_market_observations "
            "WHERE underlying=? AND option_right=? AND strike=? AND expiration_date=? "
            "AND observed_at_utc<=? ORDER BY observed_at_utc DESC,quote_id DESC LIMIT 1",
            (sig["underlying"], sig["option_right"], sig["strike"],
             sig["expiration_date"], iso(now)),
        ).fetchone()

    def snapshot(self, *, as_of: datetime | None = None,
                 include_private_text: bool = False) -> dict[str, object]:
        now = as_utc(as_of or datetime.now(UTC))
        fee = self.costs.fee_per_contract_side_usd
        realized_gross = ZERO
        realized_net = ZERO if fee is not None else None
        unrealized_gross = ZERO
        unrealized_net = ZERO if fee is not None else None
        total_fees = ZERO if fee is not None else None
        win_count = completed_count = 0
        timeline: list[tuple[str, str, Decimal]] = []
        positions: list[dict[str, object]] = []
        pending: list[dict[str, object]] = []
        with closing(self._connect()) as db:
            signals = db.execute(
                "SELECT * FROM option_signals ORDER BY created_at_utc,signal_id"
            ).fetchall()
            all_orders = db.execute(
                "SELECT * FROM order_intents ORDER BY logical_order_id"
            ).fetchall()
            for signal in signals:
                sid = str(signal["signal_id"])
                buys = self._filled(db, sid, "BUY_TO_OPEN")
                sells = self._filled(db, sid, "SELL_TO_CLOSE")
                # Never report fictitious profitability from a corrupted fill ledger.
                if any(Decimal(f["price"]) > Decimal(f["limit_price"]) for f in buys):
                    raise ValueError("buy fill violates original limit")
                if any(Decimal(f["price"]) < Decimal(f["limit_price"]) for f in sells):
                    raise ValueError("sell fill violates exit limit")
                buy_units = sum(int(f["qty"]) for f in buys)
                sell_units = sum(int(f["qty"]) for f in sells)
                if sell_units > buy_units:
                    raise ValueError("durable fills indicate naked/over-sold position")
                fifo = [[int(f["qty"]), Decimal(f["price"])] for f in buys]
                per_signal_gross = ZERO
                per_signal_realized_net = ZERO if fee is not None else None
                for sell in sells:
                    qty = int(sell["qty"])
                    sell_price = Decimal(sell["price"])
                    while qty:
                        if not fifo or fifo[0][0] == 0:
                            if fifo:
                                fifo.pop(0)
                                continue
                            raise ValueError("sell fill exceeds known buy fills")
                        portion = min(qty, fifo[0][0])
                        gross = (sell_price - fifo[0][1]) * STANDARD_OPTION_MULTIPLIER * portion
                        per_signal_gross += gross
                        if per_signal_realized_net is not None and fee is not None:
                            per_signal_realized_net += gross - fee * portion * 2
                        # Drawdown and cumulative equity use closed fill chronology,
                        # never price marks or inferred fills.
                        timeline.append((str(sell["at"]), sid, gross -
                                         (fee * portion * 2 if fee is not None else ZERO)))
                        fifo[0][0] -= portion
                        qty -= portion
                remaining = [(q, p) for q, p in fifo if q > 0]
                held = sum(q for q, _ in remaining)
                if held != buy_units - sell_units:
                    raise ValueError("position reconciliation mismatch")
                avg_cost = (sum((Decimal(f["price"]) * int(f["qty"]) for f in buys), ZERO)
                            / buy_units if buy_units else None)
                avg_exit = (sum((Decimal(f["price"]) * int(f["qty"]) for f in sells), ZERO)
                            / sell_units if sell_units else None)
                open_basis = sum((Decimal(q) * p * STANDARD_OPTION_MULTIPLIER
                                  for q, p in remaining), ZERO)
                quote = self._last_quote(db, signal, now)
                quote_age = ((now - dt(quote["observed_at_utc"])).total_seconds()
                             if quote is not None else None)
                mark = (Decimal(quote["bid"]) if quote is not None
                        and quote_age is not None and 0 <= quote_age <=
                        self.costs.mark_max_age_seconds else None)
                open_gross = ((mark * held * STANDARD_OPTION_MULTIPLIER - open_basis)
                              if mark is not None else None)
                if held:
                    if open_gross is not None:
                        unrealized_gross += open_gross
                        if unrealized_net is not None and fee is not None:
                            unrealized_net += open_gross - fee * held * 2
                    else:
                        unrealized_gross = None
                        unrealized_net = None
                realized_gross += per_signal_gross
                if realized_net is not None and per_signal_realized_net is not None:
                    realized_net += per_signal_realized_net
                if fee is not None and total_fees is not None:
                    total_fees += fee * (buy_units + sell_units)
                if buy_units and not held:
                    completed_count += 1
                    success = (per_signal_realized_net if fee is not None else
                               per_signal_gross) > ZERO
                    win_count += int(success)
                if buy_units or any(o["signal_id"] == sid for o in all_orders):
                    positions.append({
                        "signal_id": sid,
                        "contract": f"{signal['underlying']} {signal['expiration_date']} "
                                    f"{signal['strike']}{signal['option_right'][0]}",
                        "source_limit": signal["original_limit"],
                        "swift_reported_average": signal["swift_reported_avg"],
                        "actual_average_fill": amount(avg_cost),
                        "actual_average_exit": amount(avg_exit),
                        "filled_buy_contracts": buy_units,
                        "filled_sell_contracts": sell_units,
                        "open_contracts": held,
                        "realized_gross_usd": amount(per_signal_gross),
                        "realized_net_usd": amount(per_signal_realized_net),
                        "paid_fees_usd": amount(
                            fee * (buy_units + sell_units) if fee is not None else None
                        ),
                        "unrealized_gross_usd": amount(open_gross),
                        "last_bid": amount(mark),
                        "quote_age_seconds": quote_age,
                        "quote_provenance": quote["provenance"] if quote is not None else None,
                        "mark_status": "AVAILABLE_ESTIMATE" if mark is not None else
                                       "UNAVAILABLE_NO_FRESH_BID",
                        "state": signal["state"],
                    })
            for order in all_orders:
                remaining_qty = int(order["requested_quantity"]) - int(order["filled_quantity"])
                if remaining_qty > 0 and order["state"] in {
                    "ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING", "INTENT_RECORDED"
                }:
                    expires = dt(order["expires_at_utc"])
                    countdown = max(0, int((expires - now).total_seconds())) if expires else None
                    pending.append({
                        "logical_order_id": order["logical_order_id"],
                        "signal_id": order["signal_id"],
                        "side": order["side"], "state": order["state"],
                        "limit_price": order["limit_price"],
                        "remaining_contracts": remaining_qty,
                        "accepted_at_utc": order["accepted_at_utc"],
                        "expires_at_utc": order["expires_at_utc"],
                        "countdown_seconds": countdown,
                    })
            counts = {str(r[0]): int(r[1]) for r in db.execute(
                "SELECT state,COUNT(*) FROM order_intents GROUP BY state"
            )}
            spread_rows = db.execute(
                "SELECT bid,ask FROM paper_market_observations WHERE observed_at_utc<=?",
                (iso(now),),
            ).fetchall()
            average_spread = (sum((Decimal(r[1]) - Decimal(r[0]) for r in spread_rows), ZERO)
                              / len(spread_rows) if spread_rows else None)
            parser_counts = {str(r[0]): int(r[1]) for r in db.execute(
                "SELECT status,COUNT(*) FROM signal_parser_decisions GROUP BY status"
            )}
            alerts = [{"code": str(r["code"]), "recorded_at_utc": r["recorded_at_utc"]}
                      for r in db.execute(
                          "SELECT code,recorded_at_utc FROM paper_engine_alerts "
                          "WHERE resolved=0 ORDER BY alert_id DESC LIMIT 25"
                      )]
            controls = db.execute(
                "SELECT paused,emergency_stop,reason FROM paper_engine_control WHERE singleton=1"
            ).fetchone()
            telegram = db.execute(
                "SELECT state,last_received_at_utc,last_error_code,overflow_count "
                "FROM telegram_runtime_state ORDER BY updated_at_utc DESC LIMIT 1"
            ).fetchone()
            recent = []
            for r in db.execute(
                "SELECT p.message_id,p.intent,p.status,p.reason,r.received_at_utc,"
                "e.body FROM signal_parser_decisions p "
                "JOIN telegram_message_revisions r ON r.revision_id=p.revision_id "
                "JOIN raw_events e ON e.event_id=r.raw_event_id "
                "ORDER BY p.revision_id DESC LIMIT 25"
            ):
                item = {"message_id": r["message_id"], "intent": r["intent"],
                        "status": r["status"], "reason": r["reason"],
                        "received_at_utc": r["received_at_utc"]}
                if include_private_text:
                    item["text"] = r["body"]
                recent.append(item)
            notices = [{"category": r["category"], "severity": r["severity"],
                        "code": r["source_code"], "created_at_utc": r["created_at_utc"]}
                       for r in db.execute(
                           "SELECT category,severity,source_code,created_at_utc "
                           "FROM local_notifications WHERE acknowledged=0 "
                           "ORDER BY notification_id DESC LIMIT 25"
                       )]
        equity = ZERO
        peak = ZERO
        max_dd = ZERO
        for _, _, delta in sorted(timeline):
            equity += delta
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        is_live = False
        try:
            from swift_autotrader.config import Settings
            cfg = Settings.from_env()
            is_live = cfg.is_live
        except Exception:
            pass
        if not is_live and self.db_path.name == "live.sqlite3":
            is_live = True
        mode = "LIVE" if is_live else "PAPER_ONLY"
        real_broker_writes = is_live
        return {
            "mode": mode, "real_broker_writes_enabled": real_broker_writes,
            "as_of_utc": iso(now), "data_quality": "LIVE_PORTFOLIO" if is_live else "SYNTHETIC_OR_UNVERIFIED_PAPER",
            "costs_assumed": {"fee_per_contract_side_usd": amount(fee),
                              "slippage_per_contract_premium": str(
                                  self.costs.slippage_per_contract_premium),
                              "fill_assumption": self.costs.fill_assumption},
            "realized_gross_usd": amount(realized_gross),
            "realized_net_usd": amount(realized_net),
            "unrealized_gross_usd": amount(unrealized_gross),
            "unrealized_net_usd": amount(unrealized_net),
            "fees_paid_usd": amount(total_fees),
            "mean_observed_bid_ask_spread_premium": amount(average_spread),
            "spread_treatment": "QUOTED_ASK_BUY_AND_BID_SELL_EMBEDDED_IN_FILL_MODEL",
            "rejected_orders": counts.get("REJECTED", 0),
            "cancelled_orders": counts.get("CANCELED", 0),
            "ignored_alerts": parser_counts.get("IGNORED", 0),
            "completed_positions": completed_count,
            "winning_completed_positions": win_count,
            "win_rate": amount(Decimal(win_count) / completed_count)
                        if completed_count else None,
            "realized_max_drawdown_usd": amount(max_dd),
            "drawdown_basis": "CLOSED_FILLS_ONLY_NOT_MARK_TO_MARKET",
            "positions": positions, "pending_orders": pending,
            "orders_by_state": counts, "parser_by_status": parser_counts,
            "alerts": alerts, "notifications": notices,
            "paused": bool(controls["paused"]),
            "emergency_stop": bool(controls["emergency_stop"]),
            "stop_reason_code": controls["reason"],
            "telegram": ({"status": telegram["state"],
                          "last_received_at_utc": telegram["last_received_at_utc"],
                          "last_error_code": telegram["last_error_code"],
                          "overflow_count": telegram["overflow_count"]}
                         if telegram is not None else {"status": "NOT_STARTED"}),
            "recent_messages": recent,
        }
