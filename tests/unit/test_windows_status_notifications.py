"""No network: verify metadata-only opt-in notifications and local dedupe."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swift_autotrader.database.migrate import apply_migrations
from swift_autotrader.notifications.alerts import NotificationCenter
from swift_autotrader.windows_status_notifications import deliver_status_notifications


@pytest.fixture
def installation(tmp_path: Path) -> Path:
    root = tmp_path / "Managed"
    root.mkdir()
    for name in ("data", "logs", "backups", "secrets", "state", "releases"):
        (root / name).mkdir()
    (root / "deployment.json").write_text(json.dumps({
        "product": "swift-options-autotrader", "mode": "PAPER_ONLY",
        "live_enabled": False, "broker_writes_enabled": False,
    }))
    apply_migrations(root / "data" / "swift.sqlite3")
    NotificationCenter(root / "data" / "swift.sqlite3").record(
        "APPLICATION", "WORKER_FAILURE", source_key="synthetic-service",
    )
    return root


class MockResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit):
        return b'{"ok":true}'


def test_disabled_by_default(installation: Path) -> None:
    assert deliver_status_notifications(installation) == 0
    assert not (installation / "state" / "status_delivery.json").exists()


def test_safe_status_delivery_is_metadata_only_and_deduplicated(installation: Path) -> None:
    requests = []
    env = {"ENABLE_TELEGRAM_STATUS_NOTIFICATIONS": "true", "PAPER_ONLY": "true",
           "ALLOW_BROKER_WRITES": "false",
           "TELEGRAM_STATUS_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz_ABC",
           "TELEGRAM_STATUS_CHAT_ID": "-1009999999999",
           "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890"}

    def fake_post(req, **kwargs):
        requests.append((req, kwargs))
        return MockResponse()

    assert deliver_status_notifications(installation, env=env, post=fake_post) == 1
    assert deliver_status_notifications(installation, env=env, post=fake_post) == 0
    assert len(requests) == 1
    payload = json.loads(requests[0][0].data)
    assert payload["text"] == "Swift PAPER status: ERROR / WORKER_FAILURE"
    assert "synthetic-service" not in payload["text"]


def test_prevents_accidental_posting_to_paid_source(installation: Path) -> None:
    env = {"ENABLE_TELEGRAM_STATUS_NOTIFICATIONS": "true", "PAPER_ONLY": "true",
           "ALLOW_BROKER_WRITES": "false",
           "TELEGRAM_STATUS_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz_ABC",
           "TELEGRAM_STATUS_CHAT_ID": "-1001234567890",
           "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890"}
    with pytest.raises(ValueError):
        deliver_status_notifications(installation, env=env, post=lambda *a, **k: None)
