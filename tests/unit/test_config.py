from decimal import Decimal
from pathlib import Path

import pytest

from swift_autotrader.config import ConfigurationError, Settings


def test_default_is_paper_only_and_disabled():
    settings = Settings.from_env({})
    assert settings.paper_only
    assert not settings.live_enabled
    assert not settings.allow_broker_writes
    assert settings.max_premium_per_signal_usd == Decimal("0")


@pytest.mark.parametrize(
    "overrides",
    [
        {"PAPER_ONLY": "false"},
        {"LIVE_ENABLED": "true"},
        {"ALLOW_BROKER_WRITES": "true"},
        {"BROKER_BACKEND": "robinhood"},
        {"TELEGRAM_ENABLED": "true"},
        {"ENTRY_TIMEOUT_SECONDS": "3599"},
        {"MAX_CONTRACTS_PER_SIGNAL": "0"},
        {"MAX_CONTRACTS_PER_SIGNAL": "101"},
        {"MAX_PREMIUM_PER_SIGNAL_USD": "-1"},
        {"MAX_PREMIUM_PER_SIGNAL_USD": "NaN"},
        {"MAX_PREMIUM_PER_SIGNAL_USD": "Infinity"},
        {"LOG_LEVEL": "INVALID"},
        {"PAPER_ONLY": "yes"},
        {"MAX_CONTRACTS_PER_SIGNAL": "abc"},
    ],
)
def test_rejects_unsafe_configuration(overrides):
    with pytest.raises(ConfigurationError):
        Settings.from_env(overrides)


def test_max_premium_is_decimal_and_does_not_change_live_guards():
    settings = Settings.from_env({"MAX_PREMIUM_PER_SIGNAL_USD": "25.50"})
    assert settings.max_premium_per_signal_usd == Decimal("25.50")
    assert settings.public_summary()["allow_broker_writes"] is False
    assert settings.mode == "PAPER_ONLY"
    assert settings.is_live is False


def test_live_configuration_validation_success(tmp_path: Path) -> None:
    session = tmp_path / "valid.session"
    session.write_bytes(b"dummy session bytes")
    env = {
        "PAPER_ONLY": "false",
        "LIVE_ENABLED": "true",
        "ALLOW_BROKER_WRITES": "true",
        "BROKER_BACKEND": "robinhood_mcp",
        "ROBINHOOD_MCP_ACCOUNT": "RH_ACC_123456",
        "TELEGRAM_ENABLED": "true",
        "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890",
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
        "TELEGRAM_SESSION_PATH": str(session.resolve()),
        "MAX_DAILY_LOSS_USD": "750",
        "MAX_CONCURRENT_POSITIONS": "4",
        "SLIPPAGE_TOLERANCE_PCT": "12",
        "KILL_SWITCH_ENABLED": "true",
    }
    settings = Settings.from_env(env)
    assert settings.is_live is True
    assert settings.mode == "LIVE"
    assert settings.robinhood_mcp_account == "RH_ACC_123456"
    assert settings.max_daily_loss_usd == Decimal("750")
    assert settings.max_concurrent_positions == 4
    assert settings.slippage_tolerance_pct == Decimal("12")
    summary = settings.public_summary()
    assert summary["mode"] == "LIVE"
    assert "robinhood_mcp_account" not in summary


def test_live_configuration_validation_failures(tmp_path: Path) -> None:
    session = tmp_path / "valid.session"
    session.write_bytes(b"dummy session bytes")
    base_env = {
        "PAPER_ONLY": "false",
        "LIVE_ENABLED": "true",
        "ALLOW_BROKER_WRITES": "true",
        "BROKER_BACKEND": "robinhood_mcp",
        "ROBINHOOD_MCP_ACCOUNT": "RH_ACC_123456",
        "TELEGRAM_ENABLED": "true",
        "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890",
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
        "TELEGRAM_SESSION_PATH": str(session.resolve()),
    }

    # Missing account
    env = dict(base_env, ROBINHOOD_MCP_ACCOUNT="")
    with pytest.raises(ConfigurationError, match="ROBINHOOD_MCP_ACCOUNT"):
        Settings.from_env(env)

    # Wrong broker backend
    env = dict(base_env, BROKER_BACKEND="paper")
    with pytest.raises(ConfigurationError, match="broker_backend='robinhood_mcp'"):
        Settings.from_env(env)

    # Excessive daily loss
    env = dict(base_env, MAX_DAILY_LOSS_USD="50000")
    with pytest.raises(ConfigurationError, match="MAX_DAILY_LOSS_USD cannot exceed"):
        Settings.from_env(env)

    # Negative slippage
    env = dict(base_env, SLIPPAGE_TOLERANCE_PCT="-5")
    with pytest.raises(ConfigurationError, match="SLIPPAGE_TOLERANCE_PCT"):
        Settings.from_env(env)

    # Kill switch disabled in live mode
    env = dict(base_env, KILL_SWITCH_ENABLED="false")
    with pytest.raises(ConfigurationError, match="KILL_SWITCH_ENABLED is required"):
        Settings.from_env(env)

    # Telegram disabled in live mode
    env = dict(base_env, TELEGRAM_ENABLED="false")
    with pytest.raises(ConfigurationError, match="TELEGRAM_ENABLED=true"):
        Settings.from_env(env)

    # Zero or negative daily loss
    env = dict(base_env, MAX_DAILY_LOSS_USD="0")
    with pytest.raises(ConfigurationError, match="MAX_DAILY_LOSS_USD must be positive"):
        Settings.from_env(env)

    env = dict(base_env, MAX_DAILY_LOSS_USD="-50")
    with pytest.raises(ConfigurationError, match="MAX_DAILY_LOSS_USD must be positive"):
        Settings.from_env(env)

    # Invalid concurrent positions (0 or > 10)
    env = dict(base_env, MAX_CONCURRENT_POSITIONS="0")
    with pytest.raises(ConfigurationError, match="MAX_CONCURRENT_POSITIONS"):
        Settings.from_env(env)

    env = dict(base_env, MAX_CONCURRENT_POSITIONS="11")
    with pytest.raises(ConfigurationError, match="MAX_CONCURRENT_POSITIONS"):
        Settings.from_env(env)

    # Slippage tolerance > 25%
    env = dict(base_env, SLIPPAGE_TOLERANCE_PCT="25.5")
    with pytest.raises(ConfigurationError, match="SLIPPAGE_TOLERANCE_PCT"):
        Settings.from_env(env)


def test_env_example_documents_all_live_risk_settings() -> None:
    example_path = Path(".env.example")
    assert example_path.exists()
    content = example_path.read_text(encoding="utf-8")
    for expected_key in [
        "MAX_DAILY_LOSS_USD",
        "MAX_CONCURRENT_POSITIONS",
        "SLIPPAGE_TOLERANCE_PCT",
        "ROBINHOOD_MCP_ACCOUNT",
        "LIVE_ENABLED",
        "ALLOW_BROKER_WRITES",
        "BROKER_BACKEND",
        "KILL_SWITCH_ENABLED",
    ]:
        assert expected_key in content, f"Missing {expected_key} in .env.example"


