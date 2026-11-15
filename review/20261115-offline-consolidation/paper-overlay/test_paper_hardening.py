"""Tests of the offline-only paper revision. All fixtures are disposable."""
from datetime import timedelta
import asyncio, json, sqlite3, os
import pytest
import pilot
from test_pilot import fixture, ingest, rows, ENTRY, CH


def safe_health(**changes):
    return {'status':'RUNNING','mode':'PAPER_ONLY','as_of_utc':pilot.stamp(),'pid':1234,'workers':{'telegram':True},'live_enabled':False,'broker_writes_enabled':False,'automated_paper_orders':False,'paper_dispatch_worker_enabled':False,**changes}

@pytest.mark.parametrize('change',[{'live_enabled':True},{'broker_writes_enabled':0},{'automated_paper_orders':True},{'pid':True},{'pid':0},{'workers':[]},{'workers':{'telegram':False}},{'as_of_utc':'2026-11-15T00:00:00'}, {'as_of_utc':'bad'}])
def test_invalid_health(tmp_path,change):
    state=tmp_path/'state';state.mkdir(); (state/'health.json').write_text(json.dumps(safe_health(**change)))
    with pytest.raises(RuntimeError,match='RECEIVER_HEARTBEAT_UNAVAILABLE'): pilot.healthy(tmp_path)

@pytest.mark.parametrize('age',[-5,46])
def test_health_clock(tmp_path,age):
    state=tmp_path/'state';state.mkdir(); (state/'health.json').write_text(json.dumps(safe_health(as_of_utc=pilot.stamp(pilot.now()-timedelta(seconds=age)))))
    with pytest.raises(RuntimeError): pilot.healthy(tmp_path)

def test_valid_health_stop_and_duplicate(tmp_path):
    state=tmp_path/'state';state.mkdir(); p=state/'health.json';p.write_text(json.dumps(safe_health())); assert pilot.healthy(tmp_path)
    (state/'control').mkdir(); (state/'control'/'EMERGENCY_STOP.json').write_text('invalid')
    with pytest.raises(RuntimeError,match='STOP_MARKER_PRESENT'): pilot.healthy(tmp_path)
    (state/'control'/'EMERGENCY_STOP.json').unlink()
    p.write_text(json.dumps(safe_health())[:-1]+',"live_enabled":false}')
    with pytest.raises(RuntimeError,match='RECEIVER_HEARTBEAT_UNAVAILABLE'): pilot.healthy(tmp_path)

@pytest.mark.parametrize('sql',["UPDATE raw_events SET body='CHANGED'", "UPDATE raw_events SET edit_version=999", "UPDATE raw_events SET sent_at_utc='2020-01-01T00:00:00+00:00'", "UPDATE telegram_message_revisions SET origin='recovered'", "UPDATE telegram_message_revisions SET content_sha256='fake'"])
def test_reject_mutated_source_atomically(tmp_path,sql):
    s,d,st=fixture(tmp_path);pilot.setup_db(s,d,CH); ingest(st,1,ENTRY,pilot.now())
    with sqlite3.connect(s) as c:c.execute(sql)
    with pytest.raises(RuntimeError,match='SOURCE_EVENT_(BINDING|INTEGRITY)_INVALID'):pilot.sync(s,d,CH)
    assert rows(d,'SELECT COUNT(*) FROM raw_events')==[(0,)]
    assert rows(d,'SELECT COUNT(*) FROM pilot_source_fingerprints')==[(0,)]
    assert rows(d,'SELECT cursor FROM pilot_cursor')==[(0,)]

def test_health_status_age_and_disconnect():
    s={'broker_connected':True,'fresh_quote_verified':True,'fresh_quote_at_utc':pilot.stamp(),'broker_approval_setting_known':True,'human_must_approve_trades':False,'forward_fill_verified':True}
    assert pilot.cycle_status(s)['fresh_quote_verified'] is True
    d=pilot.cycle_status(s,error=True); assert d['broker_connected'] is False and d['fresh_quote_verified'] is False and d['human_must_approve_trades'] is None
    assert 'fresh_quote_at_utc' not in d and d['forward_fill_verified'] is True and s['broker_connected'] is True
    s['fresh_quote_at_utc']=pilot.stamp(pilot.now()-timedelta(seconds=60)); assert pilot.cycle_status(s)['fresh_quote_verified'] is False

def test_atomic_status_preserves_old_on_replacement_failure(tmp_path,monkeypatch):
    p=tmp_path/'status.json';pilot.write_json(p,{'status':'OLD'})
    def fail(*a):raise OSError('simulated replace failure')
    monkeypatch.setattr(pilot.os,'replace',fail)
    with pytest.raises(OSError):pilot.write_json(p,{'status':'NEW'})
    assert pilot.strict_object(p)=={'status':'OLD'}
    assert list(tmp_path.iterdir())==[p]

def test_read_only_transport_failure_does_not_call_write():
    calls=[]
    class Raw:
        async def list_tools(self):return [{'name':'get_portfolio','inputSchema':{'type':'object','properties':{'account_number':{'type':'string'}},'required':['account_number']}}]
        async def call_tool(self,n,args):calls.append(n);raise TimeoutError('synthetic transport')
    async def go():
        c=pilot.ReadOnlyClient(Raw(),'pinned');await c.list_tools()
        with pytest.raises(TimeoutError):await c.call_tool('get_portfolio',{'account_number':'pinned'})
        with pytest.raises(PermissionError):await c.call_tool('place_option_order',{'account_number':'pinned'})
    asyncio.run(go());assert calls==['get_portfolio']

def test_candidate_cannot_start(monkeypatch):
    monkeypatch.setattr(pilot.sys,'argv',['pilot.py','--run'])
    with pytest.raises(RuntimeError,match='REVIEW_ONLY_HARDENED_PAPER'):pilot.main()


def test_interrupted_batch_has_no_partial_writes(tmp_path):
    s,d,st=fixture(tmp_path);pilot.setup_db(s,d,CH)
    ingest(st,1,ENTRY,pilot.now());ingest(st,2,ENTRY,pilot.now()+timedelta(milliseconds=1))
    with sqlite3.connect(s) as c:c.execute("UPDATE raw_events SET body='CORRUPT' WHERE telegram_message_id='2'")
    with pytest.raises(RuntimeError,match='SOURCE_EVENT_INTEGRITY_INVALID'):pilot.sync(s,d,CH)
    assert rows(d,'SELECT COUNT(*) FROM raw_events')==[(0,)]
    assert rows(d,'SELECT cursor FROM pilot_cursor')==[(0,)]
    with sqlite3.connect(s) as c:c.execute('UPDATE raw_events SET body=? WHERE telegram_message_id=?',(ENTRY,'2'))
    assert pilot.sync(s,d,CH)==2 and pilot.sync(s,d,CH)==0


def test_readonly_metrics_no_invented_net(tmp_path):
    from swift_autotrader.analytics.metrics import PaperAnalytics
    s,d,st=fixture(tmp_path);pilot.setup_db(s,d,CH)
    before=d.read_bytes(); report=PaperAnalytics(d).snapshot()
    assert report['mode']=='PAPER_ONLY' and report['real_broker_writes_enabled'] is False
    assert report['realized_net_usd'] is None and report['win_rate'] is None
    assert report['completed_positions']==0 and d.read_bytes()==before
