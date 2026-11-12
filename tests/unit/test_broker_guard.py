from datetime import date
from decimal import Decimal

import pytest

from swift_autotrader.broker.paper import PaperBroker
from swift_autotrader.broker.robinhood_mcp import (
    BrokerWritesDisabledError,
    RobinhoodMCPBroker,
)
from swift_autotrader.orders.commands import OrderIntent, OrderSide
from swift_autotrader.signals.identity import OptionContract, OptionRight


def _synthetic_intent():
    return OrderIntent(
        "synthetic-order", "synthetic-idempotency", 
        OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 11, 13)),
        OrderSide.BUY_TO_OPEN, 1, Decimal("1.20")
    )


def test_real_broker_write_methods_fail_closed():
    adapter = RobinhoodMCPBroker()
    with pytest.raises(BrokerWritesDisabledError):
        adapter.submit_order(_synthetic_intent())
    with pytest.raises(BrokerWritesDisabledError):
        adapter.cancel_order("synthetic-order")


def test_paper_execution_not_misrepresented_as_implemented():
    with pytest.raises(NotImplementedError):
        PaperBroker().submit_order(_synthetic_intent())
