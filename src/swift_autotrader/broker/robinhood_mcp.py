"""Deny-all Robinhood adapter placeholder. No auth, network, MCP or write capability."""

from __future__ import annotations

from swift_autotrader.broker.base import BrokerOrderStatus
from swift_autotrader.orders.commands import OrderIntent


class BrokerWritesDisabledError(PermissionError):
    pass


class RobinhoodMCPBroker:
    def submit_order(self, intent: OrderIntent) -> BrokerOrderStatus:
        raise BrokerWritesDisabledError("All real brokerage writes are disabled in Phase 0")

    def get_order(self, logical_order_id: str) -> BrokerOrderStatus:
        raise NotImplementedError("Read-only MCP connectivity is Phase 4")

    def cancel_order(self, logical_order_id: str) -> BrokerOrderStatus:
        raise BrokerWritesDisabledError("All real brokerage writes are disabled in Phase 0")
