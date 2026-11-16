from decimal import Decimal
import pytest
from swift_autotrader.broker.review_validation import exact_contract_quantity, reconciled_processed_quantity

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
