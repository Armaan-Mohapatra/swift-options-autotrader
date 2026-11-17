"""Single source of truth for the operator-approved PAPER risk limits.

These are HARD MAXIMUMS. Every configuration path (``config.Settings``,
``orders.risk.RiskCeilings``, the Windows deployment settings and the engine
daily circuit breaker) validates against these constants, so a value above them
fails closed instead of being silently accepted. A configured value may be lower
than the maximum; zero dollar ceilings still deny every new entry.

Changing a limit means editing this module (and the matching deployment/doc
text) in a reviewed change. It must never be overridden by an environment
variable.
"""
from __future__ import annotations

from decimal import Decimal

# Realized net loss for the trading day at which new entries stop (USD).
DAILY_NET_LOSS_LIMIT_USD = Decimal("750")
# One long contract per alert. No scale-in, no averaging down; ADD is ignored.
MAX_CONTRACTS_PER_ALERT = 1
# Largest premium (limit x quantity x 100) a single alert may risk (USD).
PREMIUM_CEILING_USD = Decimal("999")
# Largest total open entry exposure across all active signals (USD).
EXPOSURE_CEILING_USD = Decimal("10000")
