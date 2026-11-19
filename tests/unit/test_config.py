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
        "MAX_PREMIUM_PER_SIGNAL_USD": "999", "MAX_TOTAL_EXPOSURE_USD": "9999",
        "ALLOW_BROKER_WRITES": "true",
        "BROKER_BACKEND": "robinhood_mcp",
        "ROBINHOOD_MCP_ACCOUNT": "RH_ACC_123456",
        "TELEGRAM_ENABLED": "true",
        "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890",
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
        "TELEGRAM_SESSION_PATH": str(session.resolve()),
        "MAX_DAILY_LOSS_USD": "750",
        "LIVE_JOURNAL_PATH": str((tmp_path / "live_journal.sqlite3").resolve()),
        "KILL_SWITCH_ENABLED": "true",
    }
    settings = Settings.from_env(env)
    assert settings.is_live is True
    assert settings.mode == "LIVE"
    assert settings.robinhood_mcp_account == "RH_ACC_123456"
    assert settings.max_daily_loss_usd == Decimal("750")
    # Owner contract: no trade-count or aggregate-exposure cap is configured or implied.
    assert not hasattr(settings, "max_concurrent_positions")
    assert settings.public_summary()["entry_price_policy"] == "EXACT_ALERT_LIMIT"
    summary = settings.public_summary()
    assert summary["mode"] == "LIVE"
    assert "robinhood_mcp_account" not in summary


def test_live_configuration_validation_failures(tmp_path: Path) -> None:
    session = tmp_path / "valid.session"
    session.write_bytes(b"dummy session bytes")
    base_env = {
        "PAPER_ONLY": "false",
        "LIVE_ENABLED": "true",
        "MAX_PREMIUM_PER_SIGNAL_USD": "999", "MAX_TOTAL_EXPOSURE_USD": "9999",
        "ALLOW_BROKER_WRITES": "true",
        "BROKER_BACKEND": "robinhood_mcp",
        "ROBINHOOD_MCP_ACCOUNT": "RH_ACC_123456",
        "TELEGRAM_ENABLED": "true",
        "SWIFT_TELEGRAM_CHANNEL_ID": "-1001234567890",
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
        "TELEGRAM_SESSION_PATH": str(session.resolve()),
        "LIVE_JOURNAL_PATH": str((tmp_path / "live_journal.sqlite3").resolve()),
    }

    # Live never inherits an implicit premium ceiling; it must be explicit, positive, <= $999.
    for value in (None, "0", "-1", "NaN", "999.01", "10000"):
        env = dict(base_env)
        if value is None:
            env.pop("MAX_PREMIUM_PER_SIGNAL_USD")
        else:
            env["MAX_PREMIUM_PER_SIGNAL_USD"] = value
        with pytest.raises(ConfigurationError):
            Settings.from_env(env)
    # No aggregate-exposure cap is required: buying power is the bound. An optional
    # voluntary value must still be finite and non-negative.
    uncapped = Settings.from_env({k: v for k, v in base_env.items()
                                  if k != "MAX_TOTAL_EXPOSURE_USD"})
    assert uncapped.is_live and uncapped.max_total_exposure_usd is None
    for value in ("-1", "NaN"):
        with pytest.raises(ConfigurationError):
            Settings.from_env(dict(base_env, MAX_TOTAL_EXPOSURE_USD=value))
    configured = Settings.from_env(base_env)
    assert (configured.max_premium_per_signal_usd, configured.max_total_exposure_usd) == (
        Decimal("999"), Decimal("9999"))

    # The live journal is mandatory and must be absolute.
    for value in (None, "relative/journal.sqlite3"):
        env = dict(base_env)
        if value is None:
            env.pop("LIVE_JOURNAL_PATH")
        else:
            env["LIVE_JOURNAL_PATH"] = value
        with pytest.raises(ConfigurationError, match="LIVE_JOURNAL_PATH"):
            Settings.from_env(env)

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

    # Any price-chasing allowance is refused; an explicit zero is tolerated.
    for value in ("-5", "10", "0.01", "NaN"):
        env = dict(base_env, SLIPPAGE_TOLERANCE_PCT=value)
        with pytest.raises(ConfigurationError):
            Settings.from_env(env)
    assert Settings.from_env(dict(base_env, SLIPPAGE_TOLERANCE_PCT="0")).is_live

    # Mobile bot is optional; this validates settings only, not execution permission.
    env = dict(base_env, KILL_SWITCH_ENABLED="false")
    assert Settings.from_env(env).kill_switch_enabled is False

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

    # $750 is the hard ceiling; 750.01 is refused, 750 accepted.
    with pytest.raises(ConfigurationError, match="MAX_DAILY_LOSS_USD cannot exceed"):
        Settings.from_env(dict(base_env, MAX_DAILY_LOSS_USD="750.01"))
    assert Settings.from_env(dict(base_env, MAX_DAILY_LOSS_USD="750")).max_daily_loss_usd == 750

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
        "LIVE_JOURNAL_PATH",
        "SLIPPAGE_TOLERANCE_PCT",
        "ROBINHOOD_MCP_ACCOUNT",
        "LIVE_ENABLED",
        "ALLOW_BROKER_WRITES",
        "BROKER_BACKEND",
        "KILL_SWITCH_ENABLED",
    ]:
        assert expected_key in content, f"Missing {expected_key} in .env.example"




@pytest.mark.parametrize("value", ["750.01", "0", "-1", "NaN"])
def test_daily_loss_hard_maximum_enforced_in_paper_mode_too(value: str) -> None:
    with pytest.raises(ConfigurationError, match="MAX_DAILY_LOSS_USD"):
        Settings.from_env({"MAX_DAILY_LOSS_USD": value})
