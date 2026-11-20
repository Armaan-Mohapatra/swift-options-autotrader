"""Source of truth for PAPER-only daily risk and shared per-trade limits.

The PAPER daily-loss maximum is not applied in LIVE: the owner separately
authorized unlimited LIVE daily loss. Per-trade contract and premium maxima
continue to apply in both modes. A configured value may be lower
than the maximum; zero dollar ceilings still deny every new entry.

Changing a limit means editing this module (and the matching deployment/doc
text) in a reviewed change. It must never be overridden by an environment
variable.
"""
from __future__ import annotations

from decimal import Decimal

# PAPER-only realized net loss at which PAPER new entries stop (USD).
DAILY_NET_LOSS_LIMIT_USD = Decimal("750")
# One long contract per alert. No scale-in, no averaging down; ADD is ignored.
MAX_CONTRACTS_PER_ALERT = 1
# Largest premium (limit x quantity x 100) a single alert may risk (USD).
PREMIUM_CEILING_USD = Decimal("999")
# Deliberately NO cap on the number of trades or on total exposure: how many
# trades may be open is bounded only by available buying power (see
# orders.risk.rejection_reason, INSUFFICIENT_BUYING_POWER).
