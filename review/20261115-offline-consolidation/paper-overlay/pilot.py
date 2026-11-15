"""Isolated forward PAPER pilot. No broker order writes or live-approval changes.
Reads the existing receiver database in SQLite read-only mode; retains a separate
simulated portfolio. No Telegram user session access. No agent mandate creation.
"""
from __future__ import annotations
import argparse, asyncio, hashlib, json, os, re, sqlite3, sys, time, tempfile, stat
from contextlib import closing
from datetime import UTC, datetime, date
from decimal import Decimal
from pathlib import Path
from dataclasses import asdict
BASE=Path(__file__).resolve().parent
sys.path.insert(0,str(BASE)); sys.path.insert(0,str(BASE/'runtime'))
import verify
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.broker.robinhood_mcp import RobinhoodMCPBroker, READ_TOOLS, _data, _obj
from swift_autotrader.broker.mcp_paper_data import MCPReadOnlyOptionData, ExchangeCalendarsSession
from swift_autotrader.paper_dispatch import PaperDispatcher
from swift_autotrader.orders.risk import RiskCeilings
from swift_autotrader.signals.identity import OptionContract, OptionRight
from swift_autotrader.windows_runtime import single_instance

ROOT=verify.ROOT; SOURCE=ROOT/'data'/'swift.sqlite3'
LIMITS=RiskCeilings(1,Decimal('999'),Decimal('9999'),3,120,Decimal('655'))

def now(): return datetime.now(UTC)
def stamp(t=None): return (t or now()).isoformat()
def write_json(path, value):
    fd,tmp=tempfile.mkstemp(prefix='paper-status-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,indent=2,allow_nan=False); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.lexists(tmp): os.unlink(tmp)

def strict_object(path):
    def unique(pairs):
        out={}
        for k,v in pairs:
            if k in out: raise ValueError('DUPLICATE_STATUS_KEY')
            out[k]=v
        return out
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 or info.st_size>100000:
        raise ValueError('UNSAFE_HEALTH_FILE')
    with path.open('rb') as f: raw=f.read(100001)
    if len(raw)>100000: raise ValueError('UNSAFE_HEALTH_FILE')
    d=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=unique)
    if not isinstance(d,dict): raise ValueError('INVALID_HEALTH_FILE')
    return d

def health_age(value, at):
    if not isinstance(value,str): raise ValueError('INVALID_HEALTH_TIME')
    t=datetime.fromisoformat(value.replace('Z','+00:00'))
    if t.utcoffset() is None or at.utcoffset() is None: raise ValueError('INVALID_HEALTH_TIME')
    return (at-t).total_seconds()

def cycle_status(base, *, error=False):
    # A previous connection or quote cannot certify the current cycle after a failure.
    result=dict(base)
    if error:
        result.update(broker_connected=False,fresh_quote_verified=False,quote_probe='UNAVAILABLE',broker_approval_setting_known=False,human_must_approve_trades=None)
        result.pop('fresh_quote_at_utc',None)
    elif result.get('fresh_quote_verified') is True:
        try: fresh=0 <= health_age(result.get('fresh_quote_at_utc'),now()) <= 15
        except (TypeError,ValueError): fresh=False
        if not fresh: result.update(fresh_quote_verified=False,quote_probe='STALE')
    return result
def ro(path):
    c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    c.row_factory=sqlite3.Row; c.execute('PRAGMA query_only=ON'); return c

def healthy(root=ROOT):
    if any(os.path.lexists(root/'state'/x) for x in ('stop.request','SWIFT_REVIEWED_STOP','control/EMERGENCY_STOP.json')): raise RuntimeError('STOP_MARKER_PRESENT')
    try:
        h=strict_object(root/'state'/'health.json')
        valid=(0 <= health_age(h.get('as_of_utc'),now()) <= 45 and h.get('status')=='RUNNING' and h.get('mode')=='PAPER_ONLY' and all(h.get(k) is False for k in ('live_enabled','broker_writes_enabled','paper_dispatch_worker_enabled','automated_paper_orders')) and isinstance(h.get('workers'),dict) and h['workers'].get('telegram') is True and type(h.get('pid')) is int and h['pid']>0)
    except (OSError,ValueError,TypeError): valid=False
    if not valid: raise RuntimeError('RECEIVER_HEARTBEAT_UNAVAILABLE')
    return True

RAW=('event_id','channel_id','telegram_message_id','edit_version','received_at_utc','sent_at_utc','body')
REV=('revision_id','raw_event_id','channel_id','message_id','revision','event_kind','origin','telegram_sent_at_utc','telegram_edited_at_utc','received_at_utc','author_id','reply_to_message_id','content_sha256','eligible_for_future_processing','ignored_reason')
MSG=('channel_id','message_id','latest_revision_id','latest_effective_at_utc','first_seen_at_utc')
def validate_source_event(revision, raw):
    r,b=revision,raw
    if (not b or b['channel_id']!=r['channel_id'] or b['telegram_message_id']!=str(r['message_id']) or b['edit_version']!=r['revision'] or b['received_at_utc']!=r['received_at_utc'] or b['sent_at_utc']!=r['telegram_sent_at_utc']):
        raise RuntimeError('SOURCE_EVENT_BINDING_INVALID')
    try:
        body={'body':b['body'],'kind':r['event_kind'],'edit_at':r['telegram_edited_at_utc'],'reply':r['reply_to_message_id'],'author':int(r['author_id']) if r['author_id'] is not None else None,'sent_at':r['telegram_sent_at_utc']}
        expected=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
        if expected!=r['content_sha256']: raise ValueError('digest mismatch')
        sent=datetime.fromisoformat(r['telegram_sent_at_utc']); received=datetime.fromisoformat(r['received_at_utc'])
        if sent.utcoffset() is None or received.utcoffset() is None: raise ValueError('naive time')
        if r['eligible_for_future_processing'] not in (0,1): raise ValueError('invalid eligibility')
        if r['eligible_for_future_processing']==1 and (r['origin']!='live' or r['event_kind']!='new' or not 0 <= (received-sent).total_seconds() <= 120 or r['ignored_reason'] is not None): raise ValueError('invalid eligible event')
    except (TypeError,ValueError,OverflowError): raise RuntimeError('SOURCE_EVENT_INTEGRITY_INVALID') from None

def setup_db(source, target, channel):
    if source.resolve()==target.resolve(): raise ValueError('PILOT_DATABASE_MUST_BE_SEPARATE')
    apply_migrations(target)
    with closing(sqlite3.connect(target,timeout=5)) as dst, dst:
        dst.execute('CREATE TABLE IF NOT EXISTS pilot_source_fingerprints(revision_id INTEGER PRIMARY KEY, content_sha256 TEXT NOT NULL)')
        dst.execute('CREATE TABLE IF NOT EXISTS pilot_cursor(singleton INTEGER PRIMARY KEY CHECK(singleton=1), channel TEXT NOT NULL, source_path TEXT NOT NULL, baseline INTEGER NOT NULL, cursor INTEGER NOT NULL, started TEXT NOT NULL)')
        old=dst.execute('SELECT channel, source_path FROM pilot_cursor').fetchone()
        if old:
            if old!=(str(channel),str(source.resolve())): raise ValueError('PILOT_BINDING_MISMATCH')
            return
        with closing(ro(source)) as src:
            baseline=src.execute('SELECT COALESCE(MAX(revision_id),0) FROM telegram_message_revisions WHERE channel_id=?',(str(channel),)).fetchone()[0]
        dst.execute('INSERT INTO pilot_cursor VALUES(1,?,?,?,?,?)',(str(channel),str(source.resolve()),baseline,baseline,stamp()))

def sync(source,target,channel):
    """Exact local append-only replication; preserve original eligibility and latest revision.
    All copied rows and high-water mark commit together. No re-evaluating old messages.
    Bounded batch: pause rather than process an incomplete/superseded source batch.
    """
    if source.resolve()==target.resolve(): raise ValueError('PILOT_DATABASE_MUST_BE_SEPARATE')
    with closing(sqlite3.connect(target,timeout=5)) as dst, dst, closing(ro(source)) as src:
        dst.execute('PRAGMA foreign_keys=ON'); dst.execute('BEGIN IMMEDIATE')
        ch,path,baseline,cursor,started=dst.execute('SELECT channel,source_path,baseline,cursor,started FROM pilot_cursor WHERE singleton=1').fetchone()
        if ch!=str(channel) or path!=str(source.resolve()): raise ValueError('PILOT_BINDING_MISMATCH')
        src.execute('BEGIN')
        current=src.execute('SELECT COALESCE(MAX(revision_id),0) FROM telegram_message_revisions WHERE channel_id=?',(ch,)).fetchone()[0]
        if current<cursor: raise RuntimeError('SOURCE_REWOUND')
        # Check the exact last imported revision identity to catch rewrites/restores.
        if cursor>baseline:
            old=dst.execute('SELECT content_sha256 FROM pilot_source_fingerprints WHERE revision_id=?',(cursor,)).fetchone()
            cur=src.execute('SELECT content_sha256 FROM telegram_message_revisions WHERE revision_id=? AND channel_id=?',(cursor,ch)).fetchone()
            if not old or not cur or old[0]!=cur[0]: raise RuntimeError('SOURCE_DIVERGED')
        rows=src.execute('SELECT '+','.join(REV)+' FROM telegram_message_revisions WHERE channel_id=? AND revision_id>? ORDER BY revision_id LIMIT 1001',(ch,cursor)).fetchall()
        if len(rows)>1000: raise RuntimeError('SOURCE_BACKLOG_REVIEW_REQUIRED')
        touched=set()
        for r in rows:
            dst.execute('INSERT INTO pilot_source_fingerprints VALUES(?,?)',(r['revision_id'],r['content_sha256']))
            latest=src.execute('SELECT latest_revision_id FROM telegram_messages WHERE channel_id=? AND message_id=?',(ch,r['message_id'])).fetchone()
            if not latest: raise RuntimeError('SOURCE_MESSAGE_MISSING')
            # An obsolete edit of a pre-pilot message cannot refer to a paper-owned entry.
            if latest[0]<=baseline: continue
            raw=src.execute('SELECT '+','.join(RAW)+' FROM raw_events WHERE event_id=?',(r['raw_event_id'],)).fetchone()
            validate_source_event(r,raw)
            dst.execute('INSERT INTO raw_events('+','.join(RAW)+') VALUES('+','.join('?' for _ in RAW)+')',tuple(raw))
            vals=list(r)
            # Never turn a pre-pilot or ineligible source event into a fresh entry.
            sent=datetime.fromisoformat(r['telegram_sent_at_utc'].replace('Z','+00:00'))
            if sent<datetime.fromisoformat(started) and r['event_kind']=='new': vals[13]=0; vals[14]='PREDATES_PILOT'
            dst.execute('INSERT INTO telegram_message_revisions('+','.join(REV)+') VALUES('+','.join('?' for _ in REV)+')',vals)
            touched.add(r['message_id'])
        for mid in touched:
            m=src.execute('SELECT '+','.join(MSG)+' FROM telegram_messages WHERE channel_id=? AND message_id=?',(ch,mid)).fetchone()
            if not m: raise RuntimeError('SOURCE_MESSAGE_MISSING')
            # If a post-baseline historical edit points to a pre-baseline latest row,
            # keep it non-actionable and reference only a copied revision.
            if not dst.execute('SELECT 1 FROM telegram_message_revisions WHERE revision_id=?',(m['latest_revision_id'],)).fetchone():
                raise RuntimeError('SOURCE_LATEST_NOT_IN_PILOT')
            dst.execute('INSERT INTO telegram_messages('+','.join(MSG)+') VALUES(?,?,?,?,?) ON CONFLICT(channel_id,message_id) DO UPDATE SET latest_revision_id=excluded.latest_revision_id,latest_effective_at_utc=excluded.latest_effective_at_utc',tuple(m))
        if rows: dst.execute('UPDATE pilot_cursor SET cursor=? WHERE singleton=1',(rows[-1]['revision_id'],))
        return len(rows)

class ReadOnlyClient:
    """Second independent explicit read allowlist. No place/cancel/approval mutation."""
    EXTRA=frozenset({'get_trade_approval_setting','get_trade_approvals'})
    SCOPED=frozenset({'get_portfolio','get_option_orders','get_option_positions','review_option_order','get_trade_approval_setting','get_trade_approvals'})
    def __init__(self,raw,pin): self.raw=raw; self.pin=pin; self.defs={}; self.calls=0
    async def list_tools(self):
        raw=await asyncio.wait_for(self.raw.list_tools(),20)
        items=raw if isinstance(raw,list) else _obj(raw).get('tools')
        if not isinstance(items,list): raise RuntimeError('TOOL_CATALOG_UNAVAILABLE')
        defs={}
        for v in items:
            d=_obj(v); name=d.get('name')
            if not isinstance(name,str) or name in defs: raise RuntimeError('INVALID_TOOL_CATALOG')
            if not d.get('inputSchema') and d.get('input_schema'): d['inputSchema']=d['input_schema']
            defs[name]=d
        self.defs=defs
        return list(defs.values())
    async def call_tool(self,name,arguments):
        if name not in READ_TOOLS | self.EXTRA: raise PermissionError('BROKER_WRITE_DENIED')
        if name in self.SCOPED and arguments.get('account_number')!=self.pin: raise PermissionError('ACCOUNT_NOT_PINNED')
        if name not in self.defs: raise RuntimeError('TOOL_NOT_ADVERTISED')
        import jsonschema
        jsonschema.validate(arguments,self.defs[name]['inputSchema']); self.calls+=1
        return await asyncio.wait_for(self.raw.call_tool(name,arguments),20)

def totals(db):
    with closing(ro(db)) as con:
        return {t:con.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('telegram_message_revisions','option_signals','order_intents','order_fills','paper_market_observations')}

async def no_auth(*a,**k): raise RuntimeError('OAUTH_REAUTH_REQUIRED')
async def run(target,channel):
    from swift_autotrader.broker.mcp_local_auth import WindowsDPAPITokenStorage, load_pinned_agentic_account
    from swift_autotrader.broker.mcp_transport import OAuthEnrollment,authenticated_mcp_client
    db=target/'paper.sqlite3'; health=target/'PAPER-STATUS.json'; stop=target/'PAPER-STOP'; calendar=ExchangeCalendarsSession()
    if os.path.lexists(stop): raise RuntimeError('PILOT_STOPPED')
    healthy(); verify.health_gate(ROOT); setup_db(SOURCE,db,channel)
    pin=load_pinned_agentic_account(ROOT)
    tokenfile=ROOT/'secrets'/'oauth.dpapi'; verify.check_path(tokenfile,ROOT)
    enrollment=OAuthEnrollment(WindowsDPAPITokenStorage(tokenfile),no_auth,no_auth)
    base={'mode':'PAPER_ONLY','real_order_tools_called':False,'live_certified':False,'source_read_only':True,'limits':{'contracts':1,'premium_usd':999,'total_exposure_usd':9999,'max_open_signals':3,'daily_gross_loss_stop_usd':655},'fresh_quote_verified':False,'forward_fill_verified':False}
    probe_at=0; reconnect_delay=30
    while not os.path.lexists(stop):
        try:
            async with authenticated_mcp_client(enrollment) as raw:
                client=ReadOnlyClient(raw,pin); broker=RobinhoodMCPBroker(client,approved_account_number=pin)
                discover=await broker.discover()
                if discover.status!='READ_ONLY_SCHEMA_OK': raise RuntimeError('READ_ONLY_SCHEMA_NOT_VERIFIED')
                account=await broker.external_account(); await broker.buying_power(account)
                response=_data(await client.call_tool('get_trade_approval_setting',{'account_number':pin})); flag=response.get('setting',{}).get('human_must_approve_trades')
                base.update(broker_connected=True,broker_approval_setting_known=type(flag) is bool,human_must_approve_trades=flag if type(flag) is bool else None)
                provider=MCPReadOnlyOptionData(broker,calendar=calendar,contracts_per_signal=1)
                dispatcher=PaperDispatcher(db,channel,provider,limits=LIMITS)
                while not os.path.lexists(stop):
                    healthy(); verify.health_gate(ROOT); n=sync(SOURCE,db,channel)
                    market_open=calendar.is_regular_session_open(now()); base['market_open']=market_open
                    # Reuse prior operator-selected AFRM contract for a read-only quote probe,
                    # never a paper entry or real order. Only during regular sessions.
                    if market_open and time.monotonic()>=probe_at:
                        probe_at=time.monotonic()+300
                        try:
                            ct=OptionContract('AFRM',OptionRight.PUT,Decimal('60'),date(2027,1,15))
                            ins=await broker.resolve_instrument(ct); q=await broker.quote(ins,max_age_seconds=15)
                            if ins.underlying_type!='equity' or ins.minimum_tick is None or ins.multiplier!=100 or q.bid_size is None or q.ask_size is None or q.bid<=0 or q.ask<=0: raise RuntimeError('UNVERIFIED_INSTRUMENT_OR_QUOTE')
                            base.update(fresh_quote_verified=True,quote_probe='PASS',fresh_quote_at_utc=stamp(q.observed_at_utc))
                        except Exception as exc: base.update(quote_probe='BLOCKED',quote_error_type=type(exc).__name__)
                    if os.path.lexists(stop): break
                    if market_open:
                        result=await dispatcher.run_once(); cycle=asdict(result)
                        if cycle.get('broker_writes')!=0: raise RuntimeError('PAPER_INVARIANT_FAILED')
                    else: cycle={'status':'WAITING_MARKET_OPEN'}
                    count=totals(db); base['forward_fill_verified']=count['order_fills']>0
                    write_json(health,{**cycle_status(base),'status':'PAPER_PILOT_RUNNING','as_of_utc':stamp(),'cycle':cycle,'counts':count,'new_revisions':n,'read_only_calls_session':client.calls})
                    reconnect_delay=30
                    await asyncio.sleep(5 if market_open else 15)
        except Exception as exc:
            if os.path.lexists(stop): break
            base=cycle_status(base,error=True)
            write_json(health,{**base,'status':'PAUSED_RETRY','as_of_utc':stamp(),'error_type':type(exc).__name__,'reason':str(exc) if str(exc) in {'RECEIVER_HEARTBEAT_UNAVAILABLE','STOP_MARKER_PRESENT','OAUTH_REAUTH_REQUIRED','READ_ONLY_SCHEMA_NOT_VERIFIED','SOURCE_DIVERGED','SOURCE_REWOUND','SOURCE_BACKLOG_REVIEW_REQUIRED','SOURCE_EVENT_INTEGRITY_INVALID','SOURCE_EVENT_BINDING_INVALID'} else 'Check local connection, data or dependency; details withheld to protect secrets.'})
            await asyncio.sleep(reconnect_delay); reconnect_delay=min(300,reconnect_delay*2)
    write_json(health,{**cycle_status(base,error=True),'status':'PAPER_PILOT_STOPPED','as_of_utc':stamp(),'counts':totals(db)})

def main():
    a=argparse.ArgumentParser(); a.add_argument('--check',action='store_true'); a.add_argument('--run',action='store_true'); args=a.parse_args()
    raise RuntimeError('REVIEW_ONLY_HARDENED_PAPER_CANDIDATE_NOT_INSTALLED')
    if os.name!='nt': raise RuntimeError('WINDOWS_ONLY')
    if os.environ.get('PAPER_ONLY')!='true' or os.environ.get('LIVE_ENABLED')!='false' or os.environ.get('ALLOW_BROKER_WRITES')!='false': raise RuntimeError('PAPER_MODE_REQUIRED')
    channel=int(os.environ['SWIFT_TELEGRAM_CHANNEL_ID']); assert channel < -1000000000000
    v={'runtime_files':verify.verify_runtime(ROOT),**verify.verify_stage(verify.STAGE)}; verify.health_gate(ROOT); healthy()
    import importlib.util
    missing=[n for n in ('exchange_calendars','pandas','jsonschema','win32crypt','mcp','httpx2') if importlib.util.find_spec(n) is None]
    if missing:
        print(json.dumps({'status':'BLOCKED_MISSING_LOCAL_DEPENDENCY','missing':missing,'live_certified':False})); sys.exit(2)
    import exchange_calendars, pandas, jsonschema, win32crypt
    calendar=ExchangeCalendarsSession(); state=calendar.is_regular_session_open(now())
    # Check local OAuth files exist, do not read actual secrets during --check.
    for name in ('oauth.dpapi','agentic_account.dpapi'):
        p=ROOT/'secrets'/name; verify.check_path(p,ROOT)
        if not p.is_file(): raise RuntimeError('OAUTH_FILES_MISSING')
    if args.check:
        print(json.dumps({**v,'status':'PAPER_PRECHECK_PASS','market_open':state,'receiver_healthy':True,'live_certified':False})); return
    if not args.run: raise ValueError('MODE_REQUIRED')
    target=BASE/'state'; target.mkdir(exist_ok=True)
    with single_instance(target/'pilot.lock'): asyncio.run(run(target,channel))
if __name__=='__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'status':'BLOCKED','error_type':type(exc).__name__,'live_certified':False,'real_order_tools_called':False}),flush=True); sys.exit(2)
