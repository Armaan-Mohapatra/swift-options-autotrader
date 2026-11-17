from decimal import Decimal

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
