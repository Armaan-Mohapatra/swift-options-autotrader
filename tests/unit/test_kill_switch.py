"""Requirement-driven unit test suite for Mobile Emergency Kill Switch Bot (R3 / Feature 8).

Covers:
- Tier 1: Feature Coverage (/status, /pause, /resume, /stop emergency halting,
  /flatten challenge).
- Tier 2: Boundary & Corner Cases (Unauthorized sender allowlist, token expiration,
  invalid token, resume guard during active emergency stop, unknown commands, idempotent stops).
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.broker.base import OrderSide
from swift_autotrader.broker.live_broker import LiveBroker
from swift_autotrader.notifications.kill_switch import TelegramKillSwitchBot
from swift_autotrader.orders.engine import PaperOrderEngine
from swift_autotrader.orders.risk import PaperPreflight, RiskCeilings
from swift_autotrader.signals.identity import OptionContract, OptionRight

AUTHORIZED_CHAT_ID = 123456789
UNAUTHORIZED_CHAT_ID = 999999999


@pytest.fixture
def mock_bot() -> TelegramKillSwitchBot:
    """Production Telegram kill switch bot with single authorized chat ID."""
    return TelegramKillSwitchBot(
        bot_token="test_token_secret",
        allowed_chat_ids=(AUTHORIZED_CHAT_ID,),
        token_ttl_seconds=60.0,
    )


@pytest.fixture
def test_engine(live_db: Path, fake_live_broker: LiveBroker, fake_clock) -> PaperOrderEngine:
    """PaperOrderEngine in LIVE mode with the canonical LiveBroker on the offline MCP fake."""
    broker = fake_live_broker
    ceilings = RiskCeilings(daily_net_loss_usd=Decimal("500"))
    return PaperOrderEngine(
        live_db,
        broker,
        preflight=PaperPreflight(),
        limits=ceilings,
        clock=fake_clock,
        live_mode=True,
    )


# ============================================================================
# Tier 1: Feature Coverage (Core Functionality & Happy Paths)
# ============================================================================


def test_kill_switch_status_command(mock_bot: TelegramKillSwitchBot, test_engine: PaperOrderEngine) -> None:
    """T1-F8-01: /status queries operational health and position counts."""
    reply = mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/status", engine=test_engine)
    assert "Mode: LIVE" in reply
    assert "Paused: False" in reply
    assert "Emergency Stop: False" in reply
    assert len(mock_bot.sent_messages) == 1
    assert mock_bot.sent_messages[0] == (AUTHORIZED_CHAT_ID, reply)


def test_kill_switch_pause_command(mock_bot: TelegramKillSwitchBot, test_engine: PaperOrderEngine) -> None:
    """T1-F8-02: /pause pauses engine and suppresses new entries."""
    reply = mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/pause", engine=test_engine)
    assert "Engine paused" in reply
    assert test_engine._control()[0] is True  # paused is True
    assert mock_bot.engine_control["paused"] is True


def test_kill_switch_resume_command(mock_bot: TelegramKillSwitchBot, test_engine: PaperOrderEngine) -> None:
    """T1-F8-03: /resume restores engine entry processing when not stopped."""
    test_engine.set_paused(True)
    mock_bot.engine_control["paused"] = True

    reply = mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/resume", engine=test_engine)
    assert "Engine resumed" in reply
    assert test_engine._control()[0] is False  # paused is False
    assert mock_bot.engine_control["paused"] is False


def test_kill_switch_stop_emergency_command(mock_bot: TelegramKillSwitchBot, test_engine: PaperOrderEngine) -> None:
    """T1-F8-04: /stop triggers emergency halt, cancels open buys, and stops engine."""
    reply = mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/stop", engine=test_engine)
    assert "EMERGENCY STOP ACTIVATED" in reply
    paused, emergency_stop = test_engine._control()
    assert paused is True
    assert emergency_stop is True
    assert mock_bot.engine_control["emergency_stop"] is True


def test_kill_switch_flatten_challenge(mock_bot: TelegramKillSwitchBot, test_engine: PaperOrderEngine) -> None:
    """T1-F8-05: /flatten initiates two-step verification with a confirmation token."""
    reply = mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/flatten", engine=test_engine)
    assert "CONFIRM FLATTEN" in reply
    assert "/confirm_flatten" in reply
    assert len(mock_bot.pending_tokens) == 1

    # Extract generated token
    token = list(mock_bot.pending_tokens.keys())[0]
    assert len(token) == 4
    assert token in reply


# ============================================================================
# Tier 2: Boundary & Corner Cases (Robustness & Edge Conditions)
# ============================================================================


def test_unauthorized_sender_dropped(mock_bot: TelegramKillSwitchBot, test_engine: PaperOrderEngine) -> None:
    """T2-F8-01: Commands from non-allowlisted chat IDs fail closed without engine state change."""
    reply = mock_bot.handle_command(UNAUTHORIZED_CHAT_ID, "/stop", engine=test_engine)
    assert "UNAUTHORIZED" in reply
    paused, emergency_stop = test_engine._control()
    assert paused is False
    assert emergency_stop is False
    assert mock_bot.engine_control["emergency_stop"] is False


def test_flatten_token_expiry(mock_bot: TelegramKillSwitchBot, test_engine: PaperOrderEngine) -> None:
    """T2-F8-02: Flatten confirmation token expires after 60 seconds."""
    # Initiate flatten at t=1000.0
    reply = mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/flatten", now_ts=1000.0, engine=test_engine)
    token = list(mock_bot.pending_tokens.keys())[0]

    # Attempt confirmation after 61 seconds (t=1061.0)
    confirm_reply = mock_bot.handle_command(
        AUTHORIZED_CHAT_ID,
        f"/confirm_flatten {token}",
        now_ts=1061.0,
        engine=test_engine,
    )
    assert "Confirmation token expired" in confirm_reply
    # Engine is NOT stopped or flattened
    assert test_engine._control()[1] is False


def test_flatten_invalid_token(mock_bot: TelegramKillSwitchBot, test_engine: PaperOrderEngine) -> None:
    """T2-F8-03: Invalid confirmation token is rejected."""
    mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/flatten", now_ts=1000.0, engine=test_engine)
    confirm_reply = mock_bot.handle_command(
        AUTHORIZED_CHAT_ID,
        "/confirm_flatten wrong_token_xyz",
        now_ts=1010.0,
        engine=test_engine,
    )
    assert "Invalid or unknown confirmation token" in confirm_reply
    assert test_engine._control()[1] is False


def test_resume_during_active_emergency_stop_rejected(
    mock_bot: TelegramKillSwitchBot,
    test_engine: PaperOrderEngine,
) -> None:
    """T2-F8-04: Cannot resume while emergency stop is engaged; manual reset required."""
    mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/stop", engine=test_engine)
    reply = mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/resume", engine=test_engine)
    assert "Cannot resume: Emergency Stop is active" in reply
    assert test_engine._control()[1] is True


def test_unknown_command_handling(mock_bot: TelegramKillSwitchBot) -> None:
    """T2-F8-05: Unrecognized commands return help text without crashing."""
    reply = mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/not_a_real_command")
    assert "Unknown command" in reply
    assert "/status" in reply
    assert "/flatten" in reply


def test_idempotent_duplicate_stop_commands(mock_bot: TelegramKillSwitchBot, test_engine: PaperOrderEngine) -> None:
    """T2-F8-06: Multiple /stop commands in succession execute idempotently."""
    reply1 = mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/stop", engine=test_engine)
    reply2 = mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/stop", engine=test_engine)
    assert "EMERGENCY STOP ACTIVATED" in reply1
    assert "EMERGENCY STOP ACTIVATED" in reply2
    assert test_engine._control()[1] is True


def test_flatten_cross_chat_spoofing_rejected(test_engine: PaperOrderEngine) -> None:
    """T2-F8-07: Confirmation token is strictly bound to originating chat ID."""
    bot = TelegramKillSwitchBot(
        bot_token="test_token_secret",
        allowed_chat_ids=(100, 200),
        token_ttl_seconds=60.0,
    )
    # Chat 100 initiates flatten
    init_reply = bot.handle_command(100, "/flatten", now_ts=1000.0, engine=test_engine)
    assert "CONFIRM FLATTEN" in init_reply
    token = list(bot.pending_tokens.keys())[0]

    # Chat 200 attempts to confirm Chat 100 token
    spoofed_reply = bot.handle_command(200, f"/confirm_flatten {token}", now_ts=1010.0, engine=test_engine)
    assert "Confirmation token was issued to a different chat ID" in spoofed_reply
    # Engine must NOT be stopped or paused
    assert test_engine._control()[1] is False
    # Token must remain valid and pending for Chat 100
    assert token in bot.pending_tokens

    # Chat 100 legitimately confirms its own token
    legit_reply = bot.handle_command(100, f"/confirm_flatten {token}", now_ts=1020.0, engine=test_engine)
    assert "FLATTEN EXECUTED" in legit_reply
    assert test_engine._control()[1] is True
    assert token not in bot.pending_tokens


def test_kill_switch_cancel_command(mock_bot: TelegramKillSwitchBot, test_engine: PaperOrderEngine) -> None:
    """T1-F8-06: /cancel cancels open pending buy orders without triggering emergency halt."""
    contract = OptionContract("SPY", OptionRight.CALL, Decimal("680"), datetime(2026, 11, 20).date())
    now = datetime(2026, 11, 16, 14, 30, 0, tzinfo=UTC)

    # Insert signal and create resting entry order via engine
    with test_engine._db() as db:
        db.execute(
            "INSERT INTO option_signals(signal_id, underlying, option_right, strike, expiration_date, original_limit, created_at_utc, state) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'OPEN')",
            ("test_cancel_buy", contract.underlying, contract.right.value, str(contract.strike), contract.expiration.isoformat(), "1.20", now.isoformat()),
        )
        sig_row = db.execute("SELECT * FROM option_signals WHERE signal_id='test_cancel_buy'").fetchone()

    intent, _ = test_engine._intent(sig_row, OrderSide.BUY_TO_OPEN, 1, Decimal("1.20"), now)
    test_engine._submit_existing(intent, now)

    reply = mock_bot.handle_command(AUTHORIZED_CHAT_ID, "/cancel", engine=test_engine)
    assert "CANCELED: 1 open pending buy orders cancelled" in reply

    # Verify order state is CANCELED
    with test_engine._db() as db:
        row = db.execute("SELECT state FROM order_intents WHERE logical_order_id=?", (intent.logical_order_id,)).fetchone()
        assert row[0] == "CANCELED"

    # Emergency stop is NOT activated
    paused, stopped = test_engine._control()
    assert stopped is False


def test_kill_switch_user_id_allowlist(test_engine: PaperOrderEngine) -> None:
    """T2-F8-08: Sender user ID is validated against allowed_user_ids."""
    bot = TelegramKillSwitchBot(
        bot_token="test_token",
        allowed_chat_ids=(AUTHORIZED_CHAT_ID,),
        allowed_user_ids=(111, 222),
    )
    # Authorized user ID
    reply = bot.handle_command(AUTHORIZED_CHAT_ID, "/status", engine=test_engine, user_id=111)
    assert "Mode: LIVE" in reply

    # Unauthorized user ID in authorized chat
    reply_unauth = bot.handle_command(AUTHORIZED_CHAT_ID, "/status", engine=test_engine, user_id=999)
    assert "UNAUTHORIZED: Sender user ID not authorized" in reply_unauth


def test_kill_switch_service_poll_once_dispatches() -> None:
    """T1-F8-07: TelegramKillSwitchService polls getUpdates and dispatches commands."""
    from swift_autotrader.notifications.kill_switch import TelegramKillSwitchService

    bot = TelegramKillSwitchBot(
        bot_token="test_token",
        allowed_chat_ids=(AUTHORIZED_CHAT_ID,),
    )

    mock_updates = {
        "ok": True,
        "result": [
            {
                "update_id": 501,
                "message": {
                    "chat": {"id": AUTHORIZED_CHAT_ID},
                    "from": {"id": 111},
                    "text": "/pause",
                },
            }
        ],
    }

    def fake_requester(method: str, params: dict):
        if method == "getUpdates":
            return mock_updates
        return {"ok": True}

    service = TelegramKillSwitchService(bot, requester=fake_requester)
    count = service.poll_once()

    assert count == 1
    assert service.last_update_id == 501
    assert bot.engine_control["paused"] is True
    assert len(bot.sent_messages) == 1
    assert "Engine paused" in bot.sent_messages[0][1]

