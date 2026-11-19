"""Local PAPER worker safeguards: no account, browser, or Windows host required."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from swift_autotrader.app import main
from swift_autotrader.broker.mcp_local_auth import LocalAuthNotConfigured
from swift_autotrader.config import Settings
from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.paper_service import run_paper_service, token_file
from swift_autotrader.windows_runtime import PaperSupervisor


@pytest.fixture
def root(tmp_path):
    install = tmp_path / 'SwiftAutoTraderPaper'
    for part in ('data', 'secrets', 'state', 'backups', 'logs', 'releases'):
        (install / part).mkdir(parents=True, exist_ok=True)
    (install / 'deployment.json').write_text(json.dumps({
        'product': 'swift-options-autotrader', 'mode': 'PAPER_ONLY',
        'live_enabled': False, 'broker_writes_enabled': False,
    }), encoding='utf-8')
    apply_migrations(install / 'data' / 'swift.sqlite3')
    return install


def test_dispatch_service_refuses_non_windows_without_auth(root):
    settings = Settings.from_env({'DB_PATH': str(root / 'data' / 'swift.sqlite3')})
    with pytest.raises(LocalAuthNotConfigured):
        asyncio.run(run_paper_service(settings, root))


def test_dispatch_cli_is_disabled_by_default(monkeypatch, root, capsys):
    monkeypatch.setenv('DB_PATH', str(root / 'data' / 'swift.sqlite3'))
    assert main(['paper-dispatch-service', '--root', str(root)]) == 2
    assert json.loads(capsys.readouterr().err)['status'] == 'ERROR'


def test_token_file_requires_managed_root(root, tmp_path):
    assert token_file(root) == root / 'secrets' / 'oauth.dpapi'
    with pytest.raises(RuntimeError):
        token_file(tmp_path / 'unmanaged')


def test_supervisor_fails_closed_if_dispatch_enabled_without_telegram(root):
    env = {'DB_PATH': str(root / 'data' / 'swift.sqlite3'),
           'PAPER_DISPATCH_ENABLED': 'true',
           'PAPER_MARKET_DATA_SOURCE': 'robinhood_mcp_readonly'}
    with pytest.raises(RuntimeError, match='Telegram'):
        PaperSupervisor(root, env=env)


class FakeProcess:
    def __init__(self):
        self.stdout = io.BytesIO(b'')
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def send_signal(self, _sig):
        self.terminate()

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=5):
        return 0

    def kill(self):
        self.terminate()


def test_supervisor_opt_in_starts_paper_only_worker_and_hides_telegram_secrets(root):
    session = root / 'secrets' / 'telegram.session'
    session.write_bytes(b'SYNTHETIC-NOT-AN-AUTHENTIC-SESSION')
    calls = []

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess()

    env = {
        'DB_PATH': str(root / 'data' / 'swift.sqlite3'),
        'TELEGRAM_ENABLED': 'true',
        'SWIFT_TELEGRAM_CHANNEL_ID': '-1001234567000',
        'TELEGRAM_API_ID': '123',
        'TELEGRAM_API_HASH': 'a' * 32,
        'TELEGRAM_SESSION_PATH': str(session),
        'PAPER_DISPATCH_ENABLED': 'true',
        'PAPER_MARKET_DATA_SOURCE': 'robinhood_mcp_readonly',
    }
    supervisor = PaperSupervisor(root, env=env, spawn=spawn, sleep=lambda _: None)
    assert [x.name for x in supervisor.children] == [
        'dashboard', 'telegram', 'paper-dispatch',
    ]
    supervisor._notify_scan = lambda: None
    supervisor._daily_backup = lambda: None
    supervisor.run(max_cycles=1)
    assert len(calls) == 3
    cmd, options = calls[-1]
    assert 'paper-dispatch-service' in cmd and '--root' in cmd
    assert options['env']['TELEGRAM_ENABLED'] == 'false'
    assert options['env']['PAPER_DISPATCH_ENABLED'] == 'true'
    for name in ('TELEGRAM_API_HASH', 'TELEGRAM_API_ID', 'TELEGRAM_SESSION_PATH'):
        assert name not in options['env']
    assert options['env']['SWIFT_TELEGRAM_CHANNEL_ID'] == '-1001234567000'


def test_windows_installer_opt_in_is_off_and_needs_sha_pin():
    root_path = Path(__file__).resolve().parents[2]
    setup = (root_path / 'deployment/windows/setup.ps1').read_text()
    settings = (root_path / 'deployment/windows/common.ps1').read_text()
    assert 'paper_dispatch_enabled = $false' in setup
    assert '$env:PAPER_DISPATCH_ENABLED = \'false\'' in settings
    assert 'PackageSha256' in setup and 'extract_approved_package.py' in setup
    assert 'ExpectedCommit' in setup and 'PackageZip' in setup
