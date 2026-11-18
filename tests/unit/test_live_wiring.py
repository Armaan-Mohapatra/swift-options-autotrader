"""Live-mode settings, startup self-check and order-path wiring. Fake MCP only.

No test sets a real live flag or contacts Robinhood: live settings are passed as an
in-memory mapping with a synthetic account number.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from swift_autotrader.broker.live_broker import LiveBroker
from swift_autotrader.broker.paper import PaperBroker
from swift_autotrader.broker.robinhood_mcp import MCPContractError, _data, _next_cursor
from swift_autotrader.config import ConfigurationError, Settings
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.live_runtime import (
    BorrowedMCPLoop,
    LiveStartupError,
    build_broker,
    startup_self_check,
)
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.risk import PaperPreflight, RiskCeilings
from swift_autotrader.paper_dispatch import PaperDispatcher
from swift_autotrader.windows_runtime import (
    install_mode,
    manifest_mode,
    validate_install_root,
)
from tests.unit.test_live_broker import Clock, FakeMCPClient, entry

ACCOUNT = 'MCP-ONLY-123'  # synthetic; the fake client uses this number


def live_env(tmp_path: Path) -> dict[str, str]:
    """A complete canonical live configuration (synthetic account, offline session file)."""
    session = tmp_path / 'secrets' / 'telegram.session'
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_bytes(b'synthetic')
    return {'PAPER_ONLY': 'false', 'LIVE_ENABLED': 'true', 'ALLOW_BROKER_WRITES': 'true',
            'BROKER_BACKEND': 'robinhood_mcp', 'ROBINHOOD_MCP_ACCOUNT': ACCOUNT,
            'LIVE_JOURNAL_PATH': str(tmp_path / 'journal.sqlite'),
            'MAX_PREMIUM_PER_SIGNAL_USD': '999',
            'TELEGRAM_ENABLED': 'true', 'SWIFT_TELEGRAM_CHANNEL_ID': '-1001234567890',
            'TELEGRAM_API_ID': '12345', 'TELEGRAM_API_HASH': 'a' * 32,
            'TELEGRAM_SESSION_PATH': str(session)}


def test_defaults_are_paper():
    settings = Settings.from_env({})
    assert settings.mode == 'PAPER_ONLY' and not settings.live_mode
    assert settings.live_account_ending is None


def test_complete_live_settings_enable_live_and_mask_the_account(tmp_path):
    settings = Settings.from_env(live_env(tmp_path))
    assert settings.live_mode and settings.mode == 'LIVE'
    assert settings.live_account_ending == f'...{ACCOUNT[-4:]}'
    summary = json.dumps(settings.public_summary())
    assert ACCOUNT not in summary and ACCOUNT not in repr(settings)


@pytest.mark.parametrize('missing', ['PAPER_ONLY', 'LIVE_ENABLED', 'ALLOW_BROKER_WRITES',
                                     'BROKER_BACKEND', 'ROBINHOOD_MCP_ACCOUNT',
                                     'LIVE_JOURNAL_PATH', 'MAX_PREMIUM_PER_SIGNAL_USD',
                                     'TELEGRAM_ENABLED'])
def test_any_missing_live_setting_is_refused_never_live(tmp_path, missing):
    """An incomplete live request fails closed at startup; it never becomes live."""
    env = live_env(tmp_path)
    del env[missing]
    with pytest.raises(ConfigurationError):
        Settings.from_env(env)


def test_partial_settings_never_validate_and_live_needs_an_absolute_journal(tmp_path):
    with pytest.raises(ConfigurationError):
        Settings(paper_only=False, live_enabled=True).validate()
    env = live_env(tmp_path) | {'LIVE_JOURNAL_PATH': 'relative.sqlite'}
    with pytest.raises(ConfigurationError):
        Settings.from_env(env)
    env = live_env(tmp_path) | {'BROKER_BACKEND': 'robinhood_live'}  # PR #34 name is gone
    with pytest.raises(ConfigurationError):
        Settings.from_env(env)


def make_live(tmp_path: Path, *, account: str = ACCOUNT):
    env = live_env(tmp_path) | {'ROBINHOOD_MCP_ACCOUNT': account}
    settings = Settings.from_env(env)
    control = tmp_path / 'swift.sqlite3'
    apply_migrations(control)
    clock = Clock()
    fake = FakeMCPClient(clock)
    LiveBroker.create_journal(settings.live_journal_path)
    broker = build_broker(settings, control, clock=clock, client=fake)
    return settings, control, broker, fake


def test_one_borrowed_mcp_session_handles_live_calls_on_original_loop(tmp_path):
    async def exercise():
        settings = Settings.from_env(live_env(tmp_path))
        control = tmp_path / 'swift.sqlite3'
        apply_migrations(control)
        clock = Clock()
        fake = FakeMCPClient(clock)
        LiveBroker.create_journal(settings.live_journal_path)
        runner = BorrowedMCPLoop(asyncio.get_running_loop())
        broker = build_broker(settings, control, clock=clock, client=fake, loop=runner)
        broker.signal_gate = lambda _signal: 'ENTRY'  # fake-only test fixture
        try:
            result = await asyncio.to_thread(broker.submit_order, entry())
            assert result.state.value == 'ACCEPTED'
            assert fake.place_count == 1
            # The fake sees only the loop that owns the provider session.
            assert fake.loops == {id(asyncio.get_running_loop())}
            with pytest.raises(LiveStartupError, match='worker thread'):
                broker.submit_order(entry('next'))
            assert fake.place_count == 1
        finally:
            broker.close()

    asyncio.run(exercise())


def test_self_check_prints_paper_mode(tmp_path):
    """Regression: paper-dispatch-service calls this in paper mode; it must not crash."""
    lines: list[str] = []
    result = startup_self_check(Settings.from_env({}), tmp_path / 'x.sqlite3', out=lines.append)
    assert result == {'mode': 'PAPER_ONLY', 'account': '', 'kill_switch': ''}
    assert lines == ['MODE=PAPER_ONLY']
    with pytest.raises(ConfigurationError):  # a partial live request never reaches the check
        Settings.from_env({'LIVE_ENABLED': 'true'})


def test_self_check_passes_live_and_prints_only_the_account_ending(tmp_path):
    settings, control, broker, _ = make_live(tmp_path)
    lines: list[str] = []
    try:
        result = startup_self_check(settings, control, broker, out=lines.append)
    finally:
        broker.close()
    assert result == {'mode': 'LIVE', 'account': f'...{ACCOUNT[-4:]}', 'kill_switch': 'clear'}
    assert lines[0] == f'MODE=LIVE account=...{ACCOUNT[-4:]}'
    assert ACCOUNT not in ' '.join(lines)


def test_self_check_refuses_when_the_journal_is_missing(tmp_path):
    settings, control, broker, _ = make_live(tmp_path)
    settings.live_journal_path.unlink()
    with pytest.raises(LiveStartupError, match='journal'):
        startup_self_check(settings, control, broker, out=lambda _l: None)
    broker.close()


def test_self_check_refuses_when_the_kill_switch_cannot_be_read(tmp_path):
    settings, control, broker, _ = make_live(tmp_path)
    with sqlite3.connect(control) as con:
        con.execute('DROP TABLE paper_engine_control')
    with pytest.raises(LiveStartupError, match='kill switch'):
        startup_self_check(settings, control, broker, out=lambda _l: None)
    broker.close()


def test_self_check_refuses_an_account_that_is_not_the_pinned_one(tmp_path):
    settings, control, broker, fake = make_live(tmp_path)
    fake.agentic_allowed = False  # the pinned account is not agent-enabled
    with pytest.raises(LiveStartupError, match='pinned'):
        startup_self_check(settings, control, broker, out=lambda _l: None)
    broker.close()
    (tmp_path / 'b').mkdir()
    other, control2, broker2, _ = make_live(tmp_path / 'b', account='NOT-A-REAL-ACCT')
    with pytest.raises(LiveStartupError, match='pinned'):
        startup_self_check(other, control2, broker2, out=lambda _l: None)
    broker2.close()


def test_self_check_reports_an_engaged_kill_switch(tmp_path):
    settings, control, broker, _ = make_live(tmp_path)
    with sqlite3.connect(control) as con:
        con.execute('UPDATE paper_engine_control SET paused=1')
    result = startup_self_check(settings, control, broker, out=lambda _l: None)
    broker.close()
    assert result['kill_switch'] == 'ENGAGED'


def test_build_broker_is_paper_unless_live_is_complete(tmp_path):
    control = tmp_path / 'swift.sqlite3'
    apply_migrations(control)
    assert isinstance(build_broker(Settings.from_env({}), control, clock=Clock()), PaperBroker)
    (tmp_path / 'live').mkdir()
    settings, _, broker, _ = make_live(tmp_path / 'live')
    assert isinstance(broker, LiveBroker) and broker.writes_enabled
    broker.close()
    with pytest.raises(LiveStartupError):
        build_broker(settings, control, clock=Clock())  # live without a client


def test_engine_accepts_only_paper_or_an_enabled_live_broker(tmp_path):
    settings, control, broker, fake = make_live(tmp_path)
    engine = PaperOrderEngine(control, broker, preflight=PaperPreflight(), live_mode=True)
    assert type(engine.broker) is LiveBroker
    with pytest.raises(TypeError, match='ONLY the concrete PaperBroker'):
        PaperOrderEngine(control, broker, preflight=PaperPreflight())  # never silently live
    disabled = LiveBroker(settings.live_journal_path, fake, account_number=ACCOUNT)
    with pytest.raises(TypeError, match='only an enabled LiveBroker'):
        PaperOrderEngine(control, disabled, preflight=PaperPreflight(), live_mode=True)
    broker.close()
    disabled.close()


def test_paper_dispatcher_refuses_a_live_broker(tmp_path):
    """One live service: the paper dispatcher can never drive the live writer."""
    _, control, broker, _ = make_live(tmp_path)
    try:
        with pytest.raises(TypeError, match='ONLY the concrete PaperBroker'):
            PaperDispatcher(control, -1001234567890, provider=object(),
                            limits=RiskCeilings(), broker=broker)
    finally:
        broker.close()
    assert not PaperDispatcher(control, -1001234567890, provider=object(),
                               limits=RiskCeilings()).live


@pytest.fixture
def install(tmp_path: Path) -> Path:
    root = tmp_path / 'Swift'
    root.mkdir()
    for name in ('data', 'logs', 'backups', 'secrets', 'state', 'releases'):
        (root / name).mkdir()
    return root


def write_manifest(root: Path, **fields) -> None:
    (root / 'deployment.json').write_text(json.dumps(
        {'product': 'swift-options-autotrader'} | fields), encoding='utf-8')


def test_manifest_accepts_paper_or_complete_live_only(install):
    paper = {'mode': 'PAPER_ONLY', 'live_enabled': False, 'broker_writes_enabled': False}
    live = {'mode': 'LIVE', 'live_enabled': True, 'broker_writes_enabled': True}
    assert manifest_mode({'product': 'swift-options-autotrader'} | paper) == 'PAPER_ONLY'
    assert manifest_mode({'product': 'swift-options-autotrader'} | live) == 'LIVE'
    for mixed in ({'mode': 'LIVE', 'live_enabled': True, 'broker_writes_enabled': False},
                  {'mode': 'LIVE', 'live_enabled': False, 'broker_writes_enabled': True},
                  {'mode': 'PAPER_ONLY', 'live_enabled': True, 'broker_writes_enabled': True},
                  {'mode': 'OTHER', 'live_enabled': True, 'broker_writes_enabled': True}):
        assert manifest_mode({'product': 'swift-options-autotrader'} | mixed) is None
    write_manifest(install, **live)
    assert validate_install_root(install) == install.resolve()
    assert install_mode(install) == 'LIVE'
    write_manifest(install, mode='LIVE', live_enabled=True, broker_writes_enabled=False)
    with pytest.raises(RuntimeError):
        validate_install_root(install)


def test_status_notifications_accept_locked_paper_or_complete_live(install):
    from swift_autotrader.windows_status_notifications import deliver_status_notifications
    write_manifest(install, mode='PAPER_ONLY', live_enabled=False, broker_writes_enabled=False)
    base = {'ENABLE_TELEGRAM_STATUS_NOTIFICATIONS': 'true'}
    with pytest.raises(PermissionError):  # half-live is refused
        deliver_status_notifications(install, env=base | {
            'PAPER_ONLY': 'false', 'LIVE_ENABLED': 'true', 'ALLOW_BROKER_WRITES': 'true'})
    for env in ({'PAPER_ONLY': 'true', 'ALLOW_BROKER_WRITES': 'false'},
                {'PAPER_ONLY': 'false', 'LIVE_ENABLED': 'true', 'ALLOW_BROKER_WRITES': 'true',
                 'BROKER_BACKEND': 'robinhood_mcp'}):
        with pytest.raises(ValueError):  # gets past the mode check, stops at the missing token
            deliver_status_notifications(install, env=base | env)


def test_next_may_sit_beside_or_inside_data():
    url = 'https://api.example/x/?cursor=abc'
    for payload in ({'data': {'positions': []}, 'next': url},
                    {'data': {'positions': [], 'next': url}},
                    {'data': {'positions': [], 'next': url}, 'next': url},
                    {'structuredContent': {'data': {'positions': []}, 'next': url}}):
        assert _next_cursor(_data(payload)) == 'abc'
    assert _next_cursor(_data({'data': {'positions': []}})) is None
    with pytest.raises(MCPContractError):  # two different values are ambiguous
        _data({'data': {'positions': [], 'next': url}, 'next': url + '2'})
