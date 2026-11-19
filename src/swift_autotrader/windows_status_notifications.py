"""Opt-in, metadata-only Telegram *status* notifications to operator's private chat.

This module NEVER sends Swift source messages or trades. It is disabled by
configuration and does not reuse the Telegram MTProto user session. Delivery
is at-least-once (network acknowledgement and local state cannot be atomic).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from swift_autotrader.config import LIVE_BACKEND
from swift_autotrader.notifications.alerts import NotificationCenter
from swift_autotrader.windows_runtime import _atomic_json, validate_install_root


def deliver_status_notifications(
    root: Path, *, env: dict[str, str] | None = None,
    post: Callable[..., object] | None = None, limit: int = 10,
) -> int:
    root = validate_install_root(root)
    env = dict(os.environ if env is None else env)
    if env.get("ENABLE_TELEGRAM_STATUS_NOTIFICATIONS", "false").lower() != "true":
        return 0
    paper_locked = env.get("PAPER_ONLY") == "true" and env.get("ALLOW_BROKER_WRITES") == "false"
    live_complete = (env.get("PAPER_ONLY") == "false" and env.get("LIVE_ENABLED") == "true"
                     and env.get("ALLOW_BROKER_WRITES") == "true"
                     and env.get("BROKER_BACKEND") == LIVE_BACKEND)
    if not paper_locked and not live_complete:
        raise PermissionError("Status service requires paper-locked or complete live settings")
    token = env.get("TELEGRAM_STATUS_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_STATUS_CHAT_ID", "")
    source_channel = env.get("SWIFT_TELEGRAM_CHANNEL_ID")
    if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Status bot token unavailable or invalid")
    if not re.fullmatch(r"-?\d{1,20}", chat_id) or chat_id == source_channel:
        raise ValueError("Invalid or source-channel status target")
    if not 1 <= limit <= 50:
        raise ValueError("invalid notification delivery cap")
    seen_file = root / "state" / "status_delivery.json"
    if seen_file.is_symlink():
        raise RuntimeError("status delivery file may not be a symlink")
    seen = {}
    if seen_file.is_file():
        raw = json.loads(seen_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or len(raw) > 20000:
            raise RuntimeError("corrupt or unbounded status delivery journal")
        seen = raw
    center = NotificationCenter(root / "data" / "swift.sqlite3")
    notifications = center.pending(limit=min(50, limit * 5))
    deliver = post or urllib.request.urlopen
    sent = 0
    for entry in reversed(notifications):
        if entry.fingerprint in seen:
            continue
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", entry.code):
            raise RuntimeError("invalid notification metadata")
        # Fixed, bounded, metadata-only operator message. No paid text or IDs.
        message = "Swift PAPER status: " + entry.severity + " / " + entry.code
        payload = json.dumps({"chat_id": chat_id, "text": message,
                              "disable_notification": False}).encode("utf-8")
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data=payload, headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with deliver(request, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError("Telegram status delivery rejected")
                body = json.loads(response.read(1024))
                if body.get("ok") is not True:
                    raise RuntimeError("Telegram status not acknowledged")
        except (urllib.error.URLError, TimeoutError):
            # Do not log exception or request URL; it contains bot token.
            raise RuntimeError("Telegram status transport unavailable") from None
        seen[entry.fingerprint] = entry.created_at_utc
        _atomic_json(seen_file, seen)
        sent += 1
        if sent >= limit:
            break
    return sent
