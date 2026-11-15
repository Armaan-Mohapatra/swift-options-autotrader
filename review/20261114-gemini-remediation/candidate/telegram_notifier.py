"""Optional, outbound-only Telegram notifications. No broker access or approval.
Use only explicitly configured status-bot environment credentials. Never reuse the
Swift listener session or OAuth credentials. Transport can be injected for tests.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import urllib.request

_SECRET_PATTERNS = (
    re.compile(r'\b[0-9]{6,16}:[A-Za-z0-9_-]{20,}\b'),
    re.compile(r'\b[a-fA-F0-9]{32}\b'),
    re.compile(r'Bearer\s+[A-Za-z0-9._~+/=-]+', re.I),
)
_FIELDS = frozenset(
    'contract side price reason action source source_message_id entry_source_id '
    'state filled_assumed mode environment telegram_notifier_ready component exit_code '
    'emergency_stop date_et realized_gross_pnl_usd realized_net_pnl_usd '
    'unrealized_gross_pnl_usd total_fees_usd requested_premium_usd limit_usd '
    'daily_loss_usd stop_usd current_exposure_usd new_trade_usd ceiling_usd '
    'available_usd required_usd limit_price'.split()
)


def redact_sensitive(text: str, *, pin: str | None = None) -> str:
    cleaned = str(text)
    if pin:
        cleaned = cleaned.replace(pin, '[PINNED_MCP]')
    for pat in _SECRET_PATTERNS:
        cleaned = pat.sub('[REDACTED_SECRET]', cleaned)
    return cleaned


@dataclass(frozen=True)
class NotificationReceipt:
    delivered: bool
    event_code: str
    fingerprint: str
    reason: str
    timestamp_utc: str


class TelegramNotifier:
    def __init__(
        self,
        *,
        bot_token=None,
        chat_id=None,
        pin=None,
        sent_fingerprints=None,
        timeout_seconds=8.0,
        transport=None,
    ):
        self._bot_token = (
            bot_token if bot_token is not None else os.environ.get('TELEGRAM_STATUS_BOT_TOKEN', '')
        ).strip()
        self._chat_id = (
            chat_id if chat_id is not None else os.environ.get('TELEGRAM_STATUS_CHAT_ID', '')
        ).strip()
        self._pin = pin
        self._timeout = timeout_seconds
        self._transport = transport
        self.sent_fingerprints = list(sent_fingerprints or [])[-500:]
        self.last_error = None
        self.consecutive_failures = 0

    @classmethod
    def from_install_root(cls, root: Path, *, pin=None, sent_fingerprints=None, transport=None):
        return cls(pin=pin, sent_fingerprints=sent_fingerprints, transport=transport)

    @property
    def configured(self):
        return bool(
            re.fullmatch(r'[0-9]{6,16}:[A-Za-z0-9_-]{20,}', self._bot_token)
            and re.fullmatch(r'-?[0-9]{1,20}', self._chat_id)
        )

    def _format_message(self, event_code, fields):
        lines = ['[SWIFT AUTOTRADER] ' + self._redact(event_code)]
        for k in sorted(fields):
            if k not in _FIELDS or not isinstance(fields[k], (str, int, float, bool, type(None))):
                continue
            lines.append(f'• {k}: {self._redact(fields[k])}')
        return '\n'.join(lines)[:3900]

    def _redact(self, value):
        text = redact_sensitive(str(value), pin=self._pin)
        for secret in (self._bot_token, self._chat_id):
            if secret:
                text = text.replace(secret, '[REDACTED_SECRET]')
        return text

    def notify(self, event_code, *, dedupe_key=None, **fields):
        ts = datetime.now(timezone.utc).isoformat()
        fp = hashlib.sha256(
            f'{event_code}:{dedupe_key or json.dumps(fields, sort_keys=True, default=str)}'.encode()
        ).hexdigest()[:24]

        def receipt(delivered, reason):
            return NotificationReceipt(delivered, event_code, fp, reason, ts)

        if dedupe_key is not None and fp in self.sent_fingerprints:
            return receipt(False, 'DUPLICATE_SUPPRESSED')
        if not self.configured:
            self.last_error = 'TELEGRAM_NOTIFIER_UNCONFIGURED'
            return receipt(False, self.last_error)
        payload = json.dumps(
            {
                'chat_id': self._chat_id,
                'text': self._format_message(event_code, fields),
                'disable_web_page_preview': True,
            }
        ).encode()
        try:
            if self._transport is not None:
                resp = self._transport(self._bot_token, payload, self._timeout)
            else:
                req = urllib.request.Request(
                    f'https://api.telegram.org/bot{self._bot_token}/sendMessage',
                    data=payload,
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                ctx = ssl.create_default_context()
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2

                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *args, **kwargs):
                        return None

                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}),
                    urllib.request.HTTPSHandler(context=ctx),
                    NoRedirect(),
                )
                with opener.open(req, timeout=self._timeout) as response:
                    resp = json.loads(response.read(65537))
            if not isinstance(resp, Mapping) or resp.get('ok') is not True:
                self.last_error = 'TELEGRAM_API_REJECTED'
                self.consecutive_failures += 1
                return receipt(False, self.last_error)
            self.sent_fingerprints.append(fp)
            self.sent_fingerprints = self.sent_fingerprints[-500:]
            self.last_error = None
            self.consecutive_failures = 0
            return receipt(True, 'DELIVERED')
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.consecutive_failures += 1
            return receipt(False, f'SEND_FAILED:{type(exc).__name__}')
