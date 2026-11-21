"""Live rehearsal and the entry kill-switch command. Fake MCP client only.

The rehearsal must run the whole entry preflight and report what would be sent while
never calling place_option_order or cancel_option_order.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from swift_autotrader import entry_control
from swift_autotrader.app import main
from swift_autotrader.broker.live_broker import (
    LiveBroker,
    LiveSafetyError,
    engine_kill_switch,
    entry_signal_gate,
)
from swift_autotrader.broker.robinhood_mcp import BrokerWritesDisabledError
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.live_runtime import (
    LiveStartupError,
    ReadOnlyClient,
    recorded_entry_intents,
    rehearse_entries,
)
from tests.unit.test_live_broker import Clock, FakeMCPClient, entry

WRITE_TOOLS = {'place_option_order', 'cancel_option_order'}


def add_signal(db: Path, sid: str, *, intent: str = 'ENTRY', expiry: str = '2027-02-19',
               limit: str = '2.00', rev: int = 1,
               sent_at: str = '2026-11-17T16:59:40Z') -> None:
    with sqlite3.connect(db) as con:
        con.execute('INSERT INTO raw_events(channel_id,telegram_message_id,edit_version,'
                    'received_at_utc,sent_at_utc,body) VALUES(?,?,0,?,?,?)',
                    ('c', str(rev), sent_at, sent_at, 'SYNTHETIC OPTIONS ALERT'))
        raw_id = con.execute('SELECT last_insert_rowid()').fetchone()[0]
        con.execute('INSERT INTO telegram_message_revisions('
                    'raw_event_id,channel_id,message_id,revision,event_kind,origin,'
                    'telegram_sent_at_utc,received_at_utc,content_sha256,'
                    'eligible_for_future_processing) '
                    "VALUES(?,?,?,0,'new','live',?,?,?,1)",
                    (raw_id, 'c', rev, sent_at, sent_at, f'hash-{rev}'))
        source_rev = con.execute('SELECT last_insert_rowid()').fetchone()[0]
        con.execute("INSERT OR IGNORE INTO option_signals(signal_id,underlying,option_right,"
                    "strike,expiration_date,original_limit,created_at_utc) "
                    "VALUES(?,?,?,?,?,?,'2026-11-17T16:00:00Z')",
                    (sid, 'AAPL', 'CALL', '150', expiry, limit))
        con.execute('INSERT INTO signal_metadata(signal_id,channel_id,source_message_id,'
                    'category,first_revision_id) VALUES(?,?,?,?,?)',
                    (sid, 'c', rev, 'STANDARD', source_rev))
        con.execute("INSERT INTO signal_parser_decisions(revision_id,channel_id,message_id,"
                    "intent,status,reason,signal_id,processed_at_utc) "
                    "VALUES(?,?,?,?,'RECORDED',?,?,'2026-11-17T16:00:01Z')",
                    (source_rev, 'c', rev, intent,
                     'ENTRY_VALIDATED_NOT_EXECUTED' if intent == 'ENTRY' else
                     'METADATA_ONLY', sid))

@pytest.fixture
def rig(tmp_path):
    db = tmp_path / 'swift.sqlite3'
    apply_migrations(db)
    journal = tmp_path / 'journal.sqlite'
    LiveBroker.create_journal(journal)
    clock = Clock()
    fake = FakeMCPClient(clock)
    broker = LiveBroker(journal, ReadOnlyClient(fake), account_number=fake.account,
                        rehearsal=True, kill_switch=engine_kill_switch(db),
                        signal_gate=entry_signal_gate(db), clock=clock)
    yield db, journal, broker, fake
    broker.close()


def journal_rows(journal: Path) -> tuple[int, int]:
    with sqlite3.connect(journal) as con:
        return (con.execute('SELECT COUNT(*) FROM live_orders').fetchone()[0],
                con.execute('SELECT COUNT(*) FROM live_risk_latches').fetchone()[0])


def test_rehearsal_logs_what_would_be_sent_and_never_writes(rig, tmp_path):
    db, journal, broker, fake = rig
    add_signal(db, 'sig-entry')
    add_signal(db, 'sig-add', intent='METADATA_ONLY', rev=2)  # an ADD is not an entry
    log = tmp_path / 'logs' / 'rehearsal.jsonl'
    results = rehearse_entries(broker, db, log_path=log)
    assert [r['logical_order_id'] for r in results] == ['sig-entry:entry']
    (result,) = results
    assert result['status'] == 'WOULD_SEND'
    sent = result['would_send']
    assert sent['tool'] == 'place_option_order'
    args = sent['arguments']
    assert args['price'] == '2.00' and args['quantity'] == '1' and args['type'] == 'limit'
    assert args['legs'][0]['side'] == 'buy' and args['legs'][0]['position_effect'] == 'open'
    assert args['account_number'] == f'...{fake.account[-4:]}'  # never the full number
    assert fake.account not in json.dumps(results)
    assert fake.place_count == 0 and fake.cancel_count == 0
    assert not WRITE_TOOLS & set(fake.call_log)
    assert 'review_option_order' in fake.call_log and 'get_option_quotes' in fake.call_log
    assert journal_rows(journal) == (0, 0)  # nothing reserved, nothing latched
    assert json.loads(log.read_text().splitlines()[0])['status'] == 'WOULD_SEND'


@pytest.mark.parametrize('setup,reason', [
    (lambda fake, db: setattr(fake, 'ask', '2.21'), '10%'),
    (lambda fake, db: setattr(fake, 'buying_power', '100'), 'buying power'),
    (lambda fake, db: setattr(fake, 'agentic_allowed', False), 'agentic_allowed'),
    (lambda fake, db: entry_control.pause_entries(db), 'kill switch'),
    (lambda fake, db: setattr(fake, 'approval_required', True), 'review'),
])
def test_rehearsal_reports_each_denial_without_side_effects(rig, setup, reason):
    db, journal, broker, fake = rig
    add_signal(db, 'sig-entry')
    setup(fake, db)
    (result,) = rehearse_entries(broker, db)
    assert result['status'] == 'DENIED' and reason in result['reason']
    assert 'would_send' not in result
    assert fake.place_count == 0 and not WRITE_TOOLS & set(fake.call_log)
    assert journal_rows(journal) == (0, 0)


def test_rehearsal_denies_an_expired_contract_and_a_malformed_row(rig):
    db, _, broker, _ = rig
    add_signal(db, 'sig-old', expiry='2020-01-17', rev=1)
    add_signal(db, 'sig-bad', limit='not-a-price', rev=2)
    results = {r['logical_order_id']: r for r in rehearse_entries(broker, db)}
    assert results['sig-old:entry']['status'] == 'DENIED'
    assert results['sig-bad:entry']['status'] == 'DENIED'


def test_rehearsal_never_calls_broker_for_stale_source_alert(rig):
    db, journal, broker, fake = rig
    add_signal(db, 'sig-stale', sent_at='2026-11-17T14:00:00Z')
    (result,) = rehearse_entries(broker, db)
    assert result['status'] == 'DENIED'
    assert result['reason'] == 'STALE_ENTRY_NO_REPLAY'
    assert not fake.call_log
    assert journal_rows(journal) == (0, 0)


def test_rehearsal_denies_missing_source_lineage(rig):
    db, _, broker, fake = rig
    add_signal(db, 'sig-entry')
    with sqlite3.connect(db) as con:
        con.execute('DELETE FROM signal_metadata WHERE signal_id=?', ('sig-entry',))
    (result,) = rehearse_entries(broker, db)
    assert result['status'] == 'DENIED'
    assert result['reason'] == 'SOURCE_LINEAGE_UNVERIFIED'
    assert not fake.call_log


def test_read_only_client_refuses_write_and_unknown_tools(rig):
    _, _, _, fake = rig
    client = ReadOnlyClient(fake)
    for name in ('place_option_order', 'cancel_option_order', 'some_future_tool'):
        with pytest.raises(BrokerWritesDisabledError):
            asyncio.run(client.call_tool(name, {'account_number': fake.account}))
    assert fake.place_count == 0 and fake.cancel_count == 0 and fake.call_log == []
    assert asyncio.run(client.call_tool('get_portfolio', {'account_number': fake.account}))


def test_a_rehearsal_broker_cannot_submit_cancel_or_call_a_write(rig):
    _, _, broker, fake = rig
    assert broker.writes_enabled is False
    with pytest.raises(LiveSafetyError, match='disabled'):
        broker.submit_order(entry())
    with pytest.raises(LiveSafetyError, match='refusing'):
        broker.run(broker._call_write('place_option_order', {}))
    assert broker.cancel_order('signal-1:entry').state.value == 'UNKNOWN'
    assert fake.place_count == 0 and fake.cancel_count == 0
    # even constructed with writes_enabled=True, rehearsal forces them off
    forced = LiveBroker(broker.path, fake, account_number=fake.account, writes_enabled=True,
                        rehearsal=True, clock=broker.clock)
    assert forced.writes_enabled is False
    forced.close()


def test_rehearse_entries_refuses_a_broker_that_can_write(rig):
    db, journal, _, fake = rig
    writer = LiveBroker(journal, fake, account_number=fake.account, writes_enabled=True)
    with pytest.raises(LiveStartupError):
        rehearse_entries(writer, db)
    writer.close()


def test_recorded_entry_intents_use_the_engine_idempotency_key(rig):
    db, _, _, _ = rig
    add_signal(db, 'sig-entry')
    (intent,) = recorded_entry_intents(db)
    assert intent.quantity == 1 and str(intent.limit_price) == '2.00'
    assert len(intent.idempotency_key) == 64  # same sha256 key the engine uses


def test_entry_control_pause_status_resume(tmp_path):
    db = tmp_path / 'swift.sqlite3'
    apply_migrations(db)
    assert entry_control.entries_status(db)['entries'] == 'ALLOWED'
    paused = entry_control.pause_entries(db)
    assert paused['entries'] == 'BLOCKED' and paused['reason'] == 'OPERATOR_PAUSE'
    assert engine_kill_switch(db)() is True  # LiveBroker sees it on the next entry
    assert entry_control.resume_entries(db)['entries'] == 'ALLOWED'
    with sqlite3.connect(db) as con:
        con.execute('UPDATE paper_engine_control SET emergency_stop=1, paused=1')
    with pytest.raises(RuntimeError, match='emergency stop'):
        entry_control.resume_entries(db)


def test_cli_one_liners_pause_and_check_the_kill_switch(tmp_path, monkeypatch, capsys):
    db = tmp_path / 'swift.sqlite3'
    apply_migrations(db)
    monkeypatch.setenv('DB_PATH', str(db))
    for name in ('PAPER_ONLY', 'LIVE_ENABLED', 'ALLOW_BROKER_WRITES', 'BROKER_BACKEND'):
        monkeypatch.delenv(name, raising=False)
    assert main(['entries-status']) == 0
    assert json.loads(capsys.readouterr().out)['entries'] == 'ALLOWED'
    assert main(['entries-pause']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['entries'] == 'BLOCKED' and out['mode'] == 'CONTROL_ONLY'
    assert main(['entries-status']) == 0
    assert json.loads(capsys.readouterr().out)['paused'] is True
    assert main(['entries-resume']) == 0
    assert json.loads(capsys.readouterr().out)['entries'] == 'ALLOWED'


def test_emergency_pause_stays_available_with_malformed_live_environment(
        tmp_path, monkeypatch, capsys):
    db = tmp_path / 'swift.sqlite3'
    apply_migrations(db)
    monkeypatch.setenv('DB_PATH', str(db))
    monkeypatch.setenv('PAPER_ONLY', 'definitely-not-a-bool')
    monkeypatch.setenv('LIVE_ENABLED', 'broken')
    monkeypatch.setenv('BROKER_BACKEND', 'unknown')
    assert main(['entries-pause']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['entries'] == 'BLOCKED'
    assert result['mode'] == 'CONTROL_ONLY'
    assert engine_kill_switch(db)() is True


def test_cli_live_journal_init_never_overwrites(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('LIVE_JOURNAL_PATH', str(tmp_path / 'journal.sqlite'))
    for name in ('PAPER_ONLY', 'LIVE_ENABLED', 'ALLOW_BROKER_WRITES', 'BROKER_BACKEND'):
        monkeypatch.delenv(name, raising=False)
    assert main(['live-journal-init']) == 0
    assert (tmp_path / 'journal.sqlite').is_file()
    capsys.readouterr()
    assert main(['live-journal-init']) == 2  # second run refuses to touch the existing file
