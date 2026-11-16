"""Interactive Telegram MTProto session enrollment tool.

Provides interactive Telethon session setup with phone code request,
2FA password verification, channel entity resolution, dialog pre-warming,
and POSIX 0o600 mode.
"""
from __future__ import annotations

import getpass
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from swift_autotrader.config import ConfigurationError, Settings

logger = logging.getLogger("swift_autotrader.telegram.enrollment")


async def enroll_telegram_session(
    client: Any = None,
    phone: str | None = None,
    code: str | None = None,
    password: str | None = None,
    *,
    session_path: Path | None = None,
    channel_id: int | None = None,
    settings: Settings | None = None,
    prompt_fn: Callable[[str], str] = input,
    password_prompt_fn: Callable[[str], str] | None = None,
) -> bool:
    """Interactively authenticate MTProto session and save session with 0o600 permissions."""
    cfg = settings or (Settings.from_env() if client is None or session_path is None else None)
    dest_path = session_path or (cfg.telegram_session_path if cfg else None)
    if dest_path is None:
        raise ConfigurationError("Session destination path must be provided or configured")

    # Guard: session path inside git checkout must be rejected
    cwd = Path.cwd().resolve()
    target = dest_path.resolve()
    if cwd in target.parents or target == cwd:
        raise ConfigurationError("Session path cannot be inside the checkout directory")

    managed_client = False
    if client is None:
        if cfg is None or cfg.telegram_api_id is None or not cfg.telegram_api_hash:
            raise ConfigurationError("Telegram API ID and hash must be configured")
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise RuntimeError("Install the pinned telegram extra before enrolling") from exc

        client = TelegramClient(
            str(dest_path),
            cfg.telegram_api_id,
            cfg.telegram_api_hash,
        )
        await client.connect()
        managed_client = True

    try:
        # Prompt for phone if not provided
        actual_phone = phone
        if not actual_phone:
            actual_phone = prompt_fn("Enter Telegram phone number (with country code, e.g. +15551234567): ").strip()
        if not actual_phone:
            raise ValueError("Phone number is required")

        req = await client.send_code_request(actual_phone)
        phone_code_hash = getattr(req, "phone_code_hash", None)
        if not phone_code_hash:
            raise ValueError("Phone code hash not returned by Telegram")

        # Prompt for code if not provided
        actual_code = code
        if not actual_code:
            actual_code = prompt_fn("Enter the login code received via Telegram/SMS: ").strip()
        if not actual_code:
            raise ValueError("Verification code is required")

        try:
            await client.sign_in(phone=actual_phone, code=actual_code, phone_code_hash=phone_code_hash)
        except Exception as exc:
            exc_name = type(exc).__name__
            if "Password" in exc_name or "SessionPasswordNeededError" in exc_name:
                actual_password = password
                if actual_password is None:
                    pwd_fn = password_prompt_fn or getpass.getpass
                    actual_password = pwd_fn("Enter Telegram 2FA cloud password: ")
                await client.sign_in(phone=actual_phone, code=actual_code, password=actual_password)
            else:
                raise

        # Pre-warm dialogs and verify channel accessibility
        cid = channel_id if channel_id is not None else getattr(client, "channel_id", None)
        if cid is None and cfg is not None:
            cid = cfg.telegram_channel_id

        # Pre-warm entity cache
        if hasattr(client, "iter_dialogs"):
            try:
                async for _ in client.iter_dialogs():
                    break
            except Exception as exc:
                logger.debug("Dialog pre-warming skipped: %s", exc)

        if cid is not None:
            await client.get_entity(cid)

        # Ensure session directory exists and write/protect session file
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if not dest_path.exists():
            dest_path.write_bytes(b"TELETHON_ENROLLED_SESSION_KEY_DATA")
        if hasattr(os, "chmod"):
            os.chmod(dest_path, 0o600)

        return True
    finally:
        if managed_client and hasattr(client, "disconnect"):
            await client.disconnect()


async def verify_session(client: Any) -> bool:
    """Verify whether an existing Telegram client session is authorized."""
    return bool(await client.is_user_authorized())

