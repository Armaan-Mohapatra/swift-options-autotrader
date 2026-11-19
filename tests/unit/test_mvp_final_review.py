"""Offline-only checks of the proposed MVP changes."""
import json
from decimal import Decimal

import pytest

from swift_autotrader.app import main
from swift_autotrader.broker.robinhood_mcp import MCPContractError
from swift_autotrader.config import Settings
from swift_autotrader.orders.risk import RiskCeilings, rejection_reason
from swift_autotrader.supervisor import run_supervisor
from tests.unit.test_live_risk import CONTRACT, make_ceilings, make_preflight
from tests.unit.test_robinhood_mcp import ACCOUNT, INSTRUMENT, WRITE_TOOLS, intent, setup, wait
from tests.unit.test_supervisor import make_supervisor


def test_defaults():
    s = Settings.from_env({})
    assert s.max_daily_loss_usd == RiskCeilings.from_environment({}).daily_net_loss_usd == Decimal('750')
    assert s.kill_switch_enabled is False and not s.allow_broker_writes and not s.live_enabled


@pytest.mark.parametrize('loss,expected', [('749.99',None),('750','DAILY_LOSS_CEILING_REACHED'),('750.01','DAILY_LOSS_CEILING_REACHED')])
def test_loss_boundary(loss,expected):
    # Net P&L is signed: a $750 loss is -750.
    assert rejection_reason(CONTRACT, Decimal('1'), 1, make_ceilings(max_daily_loss='750'), make_preflight(),outstanding_usd=Decimal(0),active_signals=0,realized_net_pnl_today_usd=-Decimal(loss)) == expected


@pytest.mark.parametrize('reply,allowed', [({'order_checks':{},'customer_approval_required':False},True),({'order_checks':{},'customer_approval_required':True},False),({'order_checks':[],'approval_required':False},True),({'order_checks':{},'approval_required':False,'customer_approval_required':False},True),({'order_checks':[{'severity':'warning','code':'REVIEW'}],'customer_approval_required':False},False)])
def test_observed_and_legacy_review(reply,allowed):
    fake,adapter=setup();fake.responses['review_option_order']=reply
    result=wait(adapter.review_order(ACCOUNT, INSTRUMENT,intent()))
    assert result.unblocked is allowed and not result.submitted
    assert not any(n in WRITE_TOOLS for n,_ in fake.calls)


def test_live_shape_alert_dict_blocks_with_its_code():
    reply = {'order_checks': {'alertType': 'OPTION_WIDE_BID_ASK_SPREAD', 'details': {'isSingleLeg': True}},
             'customer_approval_required': False}
    fake, adapter = setup(); fake.responses['review_option_order'] = reply
    result = wait(adapter.review_order(ACCOUNT, INSTRUMENT, intent()))
    assert not result.unblocked and result.alerts == ('OPTION_WIDE_BID_ASK_SPREAD',)
    assert not any(n in WRITE_TOOLS for n, _ in fake.calls)


@pytest.mark.parametrize('reply',[{'order_checks':{}},{'order_checks':{},'customer_approval_required':'false'},{'order_checks':{},'customer_approval_required':False,'approval_required':True},{'order_checks':{'unknown':'warning'},'customer_approval_required':False},{'customer_approval_required':False}])
def test_ambiguous_review_denied(reply):
    fake,adapter=setup();fake.responses['review_option_order']=reply
    with pytest.raises(MCPContractError):wait(adapter.review_order(ACCOUNT, INSTRUMENT,intent()))
    assert not any(n in WRITE_TOOLS for n,_ in fake.calls)


def test_startup_check_no_config_or_workers(monkeypatch,capsys):
    def forbidden(*a,**k):raise AssertionError('no configuration or credentials')
    monkeypatch.setattr(Settings,'from_env',forbidden)
    assert main(['mvp-startup-check']) == 0
    report=json.loads(capsys.readouterr().out)
    assert report['offline_check_passed'] and not report['live_startup_ready'] and not report['workers_started']


@pytest.mark.parametrize('marker',['stop.request','SWIFT_REVIEWED_STOP'])
def test_persistent_stop_blocks_supervisor(tmp_path,marker):
    state=tmp_path/'state';state.mkdir();(state/marker).write_text('STOP')
    sup=make_supervisor(tmp_path)
    with pytest.raises(RuntimeError,match='Persistent local stop'):
        run_supervisor(tmp_path,once=True,supervisor_factory=lambda _:sup)
    assert not sup.workers and (state/marker).is_file()


def test_partial_supervisor_start_is_cleaned(tmp_path):
    sup=make_supervisor(tmp_path)
    def broken(config=None):
        sup.spawn('mock',['mock']);raise RuntimeError('fake startup failure')
    sup.setup_default_workers=broken
    with pytest.raises(RuntimeError,match='fake startup failure'):
        run_supervisor(tmp_path,once=True,supervisor_factory=lambda _:sup)
    assert sup.workers['mock'].terminated
    assert json.loads((tmp_path/'state'/'supervisor-health.json').read_text())['status']=='STOPPED'
