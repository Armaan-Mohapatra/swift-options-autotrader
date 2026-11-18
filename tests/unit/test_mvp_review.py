import json
from decimal import Decimal as D

import pytest

from swift_autotrader.app import main
from swift_autotrader.orders.risk import rejection_reason
from tests.unit.test_live_risk import CONTRACT, make_ceilings, make_preflight
from tests.unit.test_supervisor import make_supervisor


@pytest.mark.parametrize('limit,qty',[(D('NaN'),1),(D('0'),1),(D('1'),0),(D('1'),True),(D('-1'),1)])
def test_invalid_candidate_order(limit,qty):
    assert rejection_reason(CONTRACT,limit,qty,make_ceilings(),make_preflight(),outstanding_usd=D('0'),active_signals=0)=='INVALID_ORDER_INPUT'

@pytest.mark.parametrize('change,expected',[({'outstanding_usd':D('NaN')},'INVALID_RISK_EVIDENCE'),({'outstanding_usd':D('-1')},'INVALID_RISK_EVIDENCE'),({'active_signals':-1},'INVALID_RISK_EVIDENCE'),({'active_signals':True},'INVALID_RISK_EVIDENCE'),({'realized_net_pnl_today_usd':D('-Infinity')},'DAILY_PNL_UNVERIFIED'),({'realized_net_pnl_today_usd':D('NaN')},'DAILY_PNL_UNVERIFIED')])
def test_invalid_risk_evidence(change,expected):
    args={'outstanding_usd':D('0'),'active_signals':0,'realized_net_pnl_today_usd':D('0'),**change}
    assert rejection_reason(CONTRACT,D('1'),1,make_ceilings(),make_preflight(),**args)==expected

def test_review_is_offline_and_does_not_read_configuration(monkeypatch,capsys):
    from swift_autotrader.config import Settings
    def fail(*a,**k):raise AssertionError('must not load local credentials')
    monkeypatch.setattr(Settings,'from_env',fail)
    assert main(['mvp-review'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['status']=='REVIEW_ONLY_NOT_LIVE_CERTIFIED' and report['real_order_submission'] is False
    assert report['cli']['live_dispatch_registered'] is True

def test_supervisor_cannot_claim_healthy_or_overwrite_receiver(tmp_path):
    state=tmp_path/'state';state.mkdir();rx=state/'health.json';rx.write_text('receiver evidence')
    sup=make_supervisor(tmp_path);proc=sup.spawn('worker',['mock']);proc.crash(1)
    sup.write_health_heartbeat()
    assert rx.read_text()=='receiver evidence'
    assert json.loads((state/'supervisor-health.json').read_text())['status']=='DEGRADED'
