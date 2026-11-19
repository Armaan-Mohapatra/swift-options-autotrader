"""Interactive, local-only creation of an unattended mandate. Never runs unattended itself.

Requires: a real console, the exact typed phrase, an existing broker approval contract file
(config/broker-approval-contract.json, written by the owner from a read-only tool catalog),
and no existing mandate. It never contacts the broker and never changes broker settings.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

import unattended_policy as up


def _optional_release_id(root: Path) -> str | None:
    pin_file = root / "active-release.txt"
    if pin_file.is_file() and not pin_file.is_symlink():
        val = pin_file.read_text(encoding="utf-8").strip()
        return val or None
    return None


def _optional_risk_sha256(root: Path) -> str | None:
    settings_path = root / "config" / "settings.json"
    if settings_path.is_file() and not settings_path.is_symlink():
        raw = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        return up.sha256_hex(raw)
    return None


def create_from_files(
    root: Path,
    *,
    pin: str,
    channel: int,
    days: int,
    max_premium: Decimal,
    max_orders_per_day: int,
    actions: set[str],
    typed_phrase: str,
    at: datetime,
    bind_release_and_config: bool = False,
) -> up.Mandate:
    contract_path = root / "config" / "broker-approval-contract.json"
    if not contract_path.is_file() or contract_path.is_symlink():
        raise up.MandateError("broker approval contract file missing; capture the tool catalog first")
    contract = json.loads(contract_path.read_text(encoding="utf-8-sig"))
    up.ConfiguredApprovalProbe(None, contract, {})  # validates scope/read-only/shape; no broker call
    release_id = _optional_release_id(root) if bind_release_and_config else None
    risk_sha = _optional_risk_sha256(root) if bind_release_and_config else None
    return up.create_mandate(
        root / "config" / "unattended-mandate.json",
        pin=pin,
        source_channel=channel,
        issued_at=at,
        validity_days=days,
        allowed_actions=actions,
        max_premium_usd=max_premium,
        max_orders_per_day=max_orders_per_day,
        broker_contract_sha256=up.sha256_hex(contract),
        typed_phrase=typed_phrase,
        release_id=release_id,
        risk_config_sha256=risk_sha,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--max-premium-usd", type=Decimal, default=Decimal("999"))
    ap.add_argument("--max-orders-per-day", type=int, default=10)
    ap.add_argument("--actions", default="BUY,SELL,CANCEL")
    ap.add_argument("--bind-release-config", action="store_true", help="Bind mandate to active-release.txt and settings.json hash")
    args = ap.parse_args()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Console required: the mandate must be typed by a person.")
        return 2
    import swift_reviewed as sr
    from swift_autotrader.config import Settings
    from swift_autotrader.windows_runtime import validate_install_root

    root = validate_install_root(args.root)
    cfg = Settings.from_env()
    pin = sr.load_pinned_agentic_account(root)
    at = datetime.now(timezone.utc)
    expires = at.replace(microsecond=0)
    phrase = up.enable_phrase(expires + timedelta(days=args.days))
    print(
        f"Mandate: up to {args.days} day(s), actions {args.actions}, premium <= ${args.max_premium_usd}, "
        f"<= {args.max_orders_per_day} orders/day, pinned MCP account only."
    )
    print("Broker approval settings are NOT changed by this. The broker must still permit each order.")
    print("Type exactly to confirm (or Enter to cancel):\n  " + phrase)
    typed = input("> ")
    if not typed:
        return 0
    m = create_from_files(
        root,
        pin=pin,
        channel=int(cfg.telegram_channel_id),
        days=args.days,
        max_premium=args.max_premium_usd,
        max_orders_per_day=args.max_orders_per_day,
        actions={a.strip() for a in args.actions.split(",")},
        typed_phrase=typed,
        at=at,
        bind_release_and_config=args.bind_release_config,
    )
    print(json.dumps({"status": "MANDATE_CREATED", "expires_at_utc": m.expires_at.isoformat()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())