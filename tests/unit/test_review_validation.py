from decimal import Decimal

import pytest

from swift_autotrader.broker.review_validation import (
    exact_contract_quantity,
    reconciled_processed_quantity,
)


@pytest.mark.parametrize('value', ['0.0000', 0, '1.0000', Decimal('10'), '0002'])
def test_whole_quantity(value):
    assert exact_contract_quantity(value) == int(Decimal(str(value)))

@pytest.mark.parametrize('value', [None, True, False, 1.0, '0.5', '1.1', '-1', 'NaN', 'Infinity', '-Infinity', '', 'abc'])
def test_invalid_quantity(value):
    with pytest.raises(ValueError):
        exact_contract_quantity(value)

def test_fill_minimum():
    with pytest.raises(ValueError):
        exact_contract_quantity('0.0000', minimum=1)

@pytest.mark.parametrize('order, expected', [({'processed_quantity':'0.0000'},0), ({'filled_quantity':1},1), ({'processed_quantity':'1.0','filled_quantity':1},1)])
def test_processed_quantity(order, expected):
    assert reconciled_processed_quantity(order) == expected

@pytest.mark.parametrize('order', [{}, {'filled_quantity':None}, {'processed_quantity':'0.1'}, {'filled_quantity':'1','processed_quantity':'0'}])
def test_bad_processed_quantity(order):
    with pytest.raises(ValueError):
        reconciled_processed_quantity(order)


def test_review_order_underlying_type_mapping():
    import asyncio
    from datetime import date

    from swift_autotrader.broker.robinhood_mcp import (
        MCPAccount,
        MCPContractError,
        MCPInstrument,
        RobinhoodMCPBroker,
    )
    from swift_autotrader.orders.commands import OrderIntent, OrderSide
    from swift_autotrader.signals.identity import OptionContract, OptionRight
    from tests.fixtures.live_mocks import StatefulFakeMCP

    account = MCPAccount("test-acc", True, "option_level_2")
    fake_mcp = StatefulFakeMCP(account_number="test-acc")
    broker = RobinhoodMCPBroker(fake_mcp)

    # 1. Equity contract (e.g. SPY)
    contract_equity = OptionContract("SPY", OptionRight.CALL, Decimal("680"), date(2026, 12, 18))
    inst_equity = MCPInstrument("opt-1", contract_equity, True, "chain-1", underlying_type="equity")
    intent_equity = OrderIntent("log-1", "idemp-1", contract_equity, OrderSide.BUY_TO_OPEN, 1, Decimal("1.25"))

    asyncio.run(broker.review_order(account, inst_equity, intent_equity))
    assert fake_mcp.calls[-1][0] == "review_option_order"
    assert fake_mcp.calls[-1][1]["underlying_type"] == "equity"
    assert fake_mcp.calls[-1][1]["chain_symbol"] == "SPY"

    # 2. Index contract (e.g. SPX)
    contract_index = OptionContract("SPX", OptionRight.CALL, Decimal("6000"), date(2026, 12, 18))
    inst_index = MCPInstrument("opt-2", contract_index, True, "chain-2", underlying_type="index")
    intent_index = OrderIntent("log-2", "idemp-2", contract_index, OrderSide.BUY_TO_OPEN, 1, Decimal("5.00"))

    asyncio.run(broker.review_order(account, inst_index, intent_index))
    assert fake_mcp.calls[-1][0] == "review_option_order"
    assert fake_mcp.calls[-1][1]["underlying_type"] == "index"
    assert fake_mcp.calls[-1][1]["chain_symbol"] == "SPX"

    # 3. Invalid underlying_type
    inst_invalid = MCPInstrument("opt-3", contract_equity, True, "chain-1", underlying_type="crypto")
    with pytest.raises(MCPContractError, match="underlying asset class"):
        asyncio.run(broker.review_order(account, inst_invalid, intent_equity))

