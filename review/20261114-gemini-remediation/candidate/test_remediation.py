"""Negative-path tests for the review-only repair. Fake data only."""
import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import pytest
import swift_reviewed as sr
import unattended_policy as up
from swift_autotrader.broker import robinhood_mcp as rm
from test_unattended_policy import T0, PIN, CHANNEL, make_mandate, buy_request, DETAILS, authorizer
from test_portfolio_production import build_portfolio_fixture
from decimal import Decimal

class CatalogOnly:
    async def list_tools(self):
        names=rm.READ_TOOLS | rm.WRITE_TOOLS | {'get_trade_approval_setting','get_trade_approvals'}
        return [{'name':n,'inputSchema':{'type':'object','properties':{k:{} for k in rm._REQUIRED.get(n,{'account_number'})}}} for n in names]
    async def call_tool(self,*args):
        raise AssertionError('catalog fixture never calls tools')

def test_optional_tools_visible_but_not_callable_in_read_only_facade():
    b=sr.ReadBroker(CatalogOnly(),approved_account_number=PIN)
    assert asyncio.run(b.discover()).status=='READ_ONLY_SCHEMA_OK'
    assert rm.OPTIONAL_READ_TOOLS <= b._discovered.keys()
    for n in rm.OPTIONAL_READ_TOOLS | rm.WRITE_TOOLS:
        with pytest.raises(rm.BrokerWritesDisabledError): asyncio.run(b._call(n,{'account_number':PIN}))

@pytest.mark.parametrize('review',[{}, {'customer_approval_required':False}, {'customer_approval_required':False,'order_checks':None}, {'customer_approval_required':False,'approval_required':True,'order_checks':{}}, {'customer_approval_required':False,'order_checks':[{'severity':'warning'}]}])
def test_review_requires_explicit_clean_checks(review):
    assert up._review_expressly_unblocked(review) is False
    with pytest.raises(sr.Blocked): sr.checked_review(review)

def test_observed_clean_review():
    r={'customer_approval_required':False,'order_checks':{},'fees':{'total_fee':'0.03'}}
    assert up._review_expressly_unblocked(r) is True
    assert sr.extract_review_fee(r)==Decimal('0.03')

def test_expected_release_requires_binding(tmp_path):
    make_mandate(tmp_path)
    with pytest.raises(up.MandateError,match='release'):
        up.Mandate.load(tmp_path/'mandate.json',pin=PIN,source_channel=CHANNEL,at=T0+timedelta(minutes=1),expected_release_id='release-test')

def test_expected_config_requires_binding(tmp_path):
    make_mandate(tmp_path)
    with pytest.raises(up.MandateError,match='risk configuration'):
        up.Mandate.load(tmp_path/'mandate.json',pin=PIN,source_channel=CHANNEL,at=T0+timedelta(minutes=1),expected_risk_config_sha256='a'*64)

def test_object_error_is_not_eligibility_data():
    with pytest.raises(ValueError): up._structured(SimpleNamespace(isError=True,structuredContent={'data':{'allowed':True}}))

@pytest.mark.parametrize('price',['0','-0.01'])
def test_nonpositive_premium_denied(tmp_path,price):
    a=authorizer(tmp_path)
    assert asyncio.run(a(buy_request(price=price),DETAILS,PIN)) is False
    assert not a.ledger_path.exists()

def test_independent_position_remains_protected(tmp_path):
    agent,_,_,_=build_portfolio_fixture(tmp_path)
    rows=[{'option_id':'synthetic-independent-holding','quantity':'1','type':'long','trade_value_multiplier':100}]
    with pytest.raises(sr.Blocked,match='unreconciled existing account position'):
        agent.entry_risk({},rows,Decimal('1.00'),[],Decimal('0'),set())

def test_dev_cli_cannot_launch(monkeypatch,tmp_path):
    monkeypatch.setattr('sys.argv',['swift_reviewed','--root',str(tmp_path),'--reviewed-live'])
    with pytest.raises(sr.Blocked,match='review-only candidate'): sr.main()
