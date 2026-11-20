"""Official MCP *read-only* option evidence for a simulated PAPER dispatcher.

This module never submits, cancels or modifies a Robinhood position. It will
remain paused until the actual MCP response provides exact instrument rules,
contemporaneous bid/ask depth, account permission, broker review and a current
exchange-calendar session. No undocumented fallback or fake depth is allowed.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from swift_autotrader.broker.robinhood_mcp import (
    MCPContractError,
    RobinhoodMCPBroker,
    _rows,
)
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.orders.risk import Preflight
from swift_autotrader.paper_dispatch import (
    MarketEvidenceUnavailable,
    MarketSnapshot,
    VerifiedQuote,
)
from swift_autotrader.parser.rules import ParsedAlert
from swift_autotrader.signals.identity import OptionContract
from swift_autotrader.signals.lineage import StaticInstrumentCatalog, resolve_entry


class SessionCalendar(Protocol):
    def is_regular_session_open(self, at_utc: datetime) -> bool: ...


class ExchangeCalendarsSession:
    """Conservative weekday/exchange schedule; no trading past 4 PM ET.

    Requires separately locked `exchange-calendars` dependency. An absent or
    unsupported calendar *fails closed*, never guesses based on weekdays alone.
    """

    def is_regular_session_open(self, at_utc: datetime) -> bool:
        if at_utc.utcoffset() is None:
            raise MarketEvidenceUnavailable("Naive market clock")
        eastern = at_utc.astimezone(ZoneInfo("America/New_York"))
        if not (9 <= eastern.hour < 16):
            return False
        if eastern.hour == 9 and eastern.minute < 30:
            return False
        try:
            import exchange_calendars
            import pandas
            calendar = exchange_calendars.get_calendar("XNYS")
            return bool(calendar.is_open_on_minute(pandas.Timestamp(at_utc)))
        except (ImportError, ValueError, RuntimeError, KeyError) as exc:
            raise MarketEvidenceUnavailable("Verified exchange session unavailable") from exc


class MCPReadOnlyOptionData:
    """Adapter from documented read-only Robinhood tool calls to evidence.

    Tool result fields (ticks, multiplier, quote depth) are not guaranteed by
    Robinhood's public overview. Field mismatch is a documented local blocker.
    No real brokerage writes are reachable through this class.
    """

    def __init__(self, broker: RobinhoodMCPBroker, *, calendar: SessionCalendar,
                 contracts_per_signal: int = 1):
        if not isinstance(broker, RobinhoodMCPBroker):
            raise TypeError("Official Robinhood read-only MCP adapter required")
        self.broker = broker
        self.calendar = calendar
        if not 1 <= contracts_per_signal <= 100:
            raise ValueError("Unapproved paper review quantity")
        self.contracts_per_signal = contracts_per_signal

    async def _listed_contracts(self, alert: ParsedAlert, now: datetime) -> list[OptionContract]:
        if not alert.has_contract or alert.expiry is None:
            raise MarketEvidenceUnavailable("Incomplete option entry")
        assert alert.underlying and alert.right and alert.strike
        response = await self.broker._call("get_option_chains", {
            "underlying_symbol": alert.underlying,
        })
        rows = _rows(response, "chains", "results")
        results: list[OptionContract] = []
        for row in rows:
            if (row.get("underlying_symbol") or row.get("symbol")) != alert.underlying:
                continue
            dates = row.get("expiration_dates")
            if not isinstance(dates, list) or len(dates) > 1000:
                raise MCPContractError("Incomplete or unbounded option expirations")
            for text_date in dates:
                try:
                    from datetime import date
                    expiry = date.fromisoformat(text_date)
                except (ValueError, TypeError) as exc:
                    raise MCPContractError("Malformed option expiration") from exc
                if (expiry.month, expiry.day) != (alert.expiry.month, alert.expiry.day):
                    continue
                if alert.expiry.year is not None and expiry.year != alert.expiry.year:
                    continue
                if expiry < now.astimezone(ZoneInfo("America/New_York")).date():
                    continue
                results.append(OptionContract(alert.underlying, alert.right,
                                              alert.strike, expiry))
        return list(dict.fromkeys(results))

    async def snapshot(
        self, entries: Sequence[ParsedAlert], active: Sequence[OptionContract],
        now: datetime,
    ) -> MarketSnapshot:
        if now.utcoffset() is None:
            raise MarketEvidenceUnavailable("Naive time")
        # Bound each polling cycle before any network calls. Never enumerate
        # hundreds of paid-alert candidates or hammer option-chain APIs.
        if len(entries) > 12 or len(active) > 12:
            raise MarketEvidenceUnavailable("Too many contracts for bounded PAPER review")
        status = await self.broker.discover()
        if status.status != "READ_ONLY_SCHEMA_OK":
            raise MarketEvidenceUnavailable("Official MCP read-only tools incompatible")
        account = await self.broker.external_account()
        available = await self.broker.buying_power(account)
        market_open = self.calendar.is_regular_session_open(now)
        candidates = list(dict.fromkeys(active))
        for alert in entries:
            possible = await self._listed_contracts(alert, now)
            if len(possible) != 1:
                raise MarketEvidenceUnavailable("Exact expiration not independently resolved")
            candidates.extend(c for c in possible if c not in candidates)
        if len(candidates) > 12:
            raise MarketEvidenceUnavailable("PAPER review contract count exceeds safe cap")
        instruments: dict[OptionContract, Preflight] = {}
        quotes: dict[OptionContract, VerifiedQuote] = {}
        for contract in candidates:
            instrument = await self.broker.resolve_instrument(contract)
            if (not instrument.tradable or
                (instrument.minimum_tick is None and instrument.tick_schedule is None) or
                instrument.multiplier != 100):
                raise MarketEvidenceUnavailable("Tick, tradability or 100 multiplier unverified")
            quote = await self.broker.quote(instrument, now=now, max_age_seconds=15)
            if quote.bid_size is None or quote.ask_size is None:
                raise MarketEvidenceUnavailable("Option bid/ask displayed depth unavailable")
            # Exits use the live bid tick; new entries use the ORIGINAL alert
            # premium. A variable tick schedule must never be guessed.
            limit_for_tick = quote.bid
            matched_entry = False
            # Match the exact contract including expiration YEAR. A same-strike
            # alert on a different expiration must never be reviewed as this leg.
            for alert in entries:
                resolved, problem = resolve_entry(
                    alert, StaticInstrumentCatalog((contract,)),
                    as_of=now.astimezone(ZoneInfo("America/New_York")).date(),
                )
                if resolved != contract or problem is not None:
                    continue
                if alert.premium is None:
                    raise MarketEvidenceUnavailable("Swift limit missing")
                if matched_entry and limit_for_tick != alert.premium:
                    raise MarketEvidenceUnavailable("Conflicting entry prices for same contract")
                limit_for_tick = alert.premium
                matched_entry = True
                test_intent = OrderIntent(
                    "paper-review", "paper-review", contract,
                    OrderSide.BUY_TO_OPEN, self.contracts_per_signal, alert.premium,
                )
                review = await self.broker.review_order(account, instrument, test_intent)
                if not review.unblocked:
                    raise MarketEvidenceUnavailable("Broker read-only review blocked")
            tick = instrument.tick_for_price(limit_for_tick)
            if tick is None or not tick.is_finite() or tick <= 0:
                raise MarketEvidenceUnavailable("Option tick at order price unverified")
            instruments[contract] = Preflight(
                instrument_verified=True, options_permission=account.eligible,
                market_session_open=market_open, available_buying_power_usd=available,
                premium_tick=tick, contract=contract,
            )
            # Distinct market observations can share a rounded timestamp.
            # Include immutable quote payload so the broker detects real changes
            # instead of aliasing different prices to the same journal event.
            key = (f"{instrument.option_id}|{quote.observed_at_utc.isoformat()}|"
                   f"{quote.bid}|{quote.ask}|{quote.bid_size}|{quote.ask_size}")
            from hashlib import sha256
            evidence = sha256(key.encode()).hexdigest()
            quotes[contract] = VerifiedQuote(
                quote_id=evidence[:48], contract=contract, bid=quote.bid, ask=quote.ask,
                bid_size=quote.bid_size, ask_size=quote.ask_size,
                observed_at_utc=quote.observed_at_utc,
                source="MCP_READONLY", evidence_reference=evidence,
            )
        return MarketSnapshot(instruments, quotes, "MCP_READONLY", now)
