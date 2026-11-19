from decimal import Decimal as D
import json
import pytest
from swift_autotrader.app import main
from swift_autotrader.orders.slippage import resolve_live_entry_limit, SlippageExceededError
from tests.unit.test_live_risk import CONTRACT, make_ceilings, make_preflight
from swift_autotrader.orders.risk import rejection_reason
from tests.unit.test_supervisor import make_supervisor

@pytest.mark.parametrize('index,value',[(0,'NaN'),(1,'Infinity'),(2,'0'),(3,'NaN'),(3,'Infinity')])
def test_invalid_slippage_values(index,value):
    args=list(map(D,['1.20','1.15','0.05','10']));args[index]=D(value)
    with pytest.raises(ValueError):resolve_live_entry_limit(*args)

def test_tick_grid_below_alert():
    assert resolve_live_entry_limit(D('1.22'),D('1.20'),D('0.05'))==D('1.25')

def test_no_valid_positive_tick():
    with pytest.raises(SlippageExceededError):resolve_live_entry_limit(D('.01'),D('.01'),D('.05'))

@pytest.mark.parametrize('limit,qty',[(D('NaN'),1),(D('0'),1),(D('1'),0),(D('1'),True),(D('-1'),1)])
def test_invalid_candidate_order(limit,qty):
    assert rejection_reason(CONTRACT,limit,qty,make_ceilings(),make_preflight(),outstanding_usd=D('0'),active_signals=0)=='INVALID_ORDER_INPUT'

@pytest.mark.parametrize('change',[{'outstanding_usd':D('NaN')},{'outstanding_usd':D('-1')},{'active_signals':-1},{'active_signals':True},{'cumulative_realized_loss_usd':D('Infinity')}])
def test_invalid_risk_evidence(change):
    args={'outstanding_usd':D('0'),'active_signals':0,'cumulative_realized_loss_usd':D('0'),**change}
    assert rejection_reason(CONTRACT,D('1'),1,make_ceilings(),make_preflight(),**args)=='INVALID_RISK_EVIDENCE'

def test_review_is_offline_and_does_not_read_configuration(monkeypatch,capsys):
    from swift_autotrader.config import Settings
    def fail(*a,**k):raise AssertionError('must not load local credentials')
    monkeypatch.setattr(Settings,'from_env',fail)
    assert main(['mvp-review'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['status']=='REVIEW_ONLY_NOT_LIVE_CERTIFIED' and report['real_order_submission'] is False
    assert report['cli']['live_dispatch_registered'] is False

def test_supervisor_cannot_claim_healthy_or_overwrite_receiver(tmp_path):
    state=tmp_path/'state';state.mkdir();rx=state/'health.json';rx.write_text('receiver evidence')
    sup=make_supervisor(tmp_path);proc=sup.spawn('worker',['mock']);proc.crash(1)
    sup.write_health_heartbeat()
    assert rx.read_text()=='receiver evidence'
    assert json.loads((state/'supervisor-health.json').read_text())['status']=='DEGRADED'
