"""Phase 0 intentionally does not simulate fills; paper engine arrives in Phase 3."""

from __future__ import annotations

from swift_autotrader.broker.base import BrokerOrderStatus
from swift_autotrader.orders.commands import OrderIntent


class PaperBroker:
    def submit_order(self, intent: OrderIntent) -> BrokerOrderStatus:
        raise NotImplementedError("Paper execution is Phase 3, not Phase 0")

    def get_order(self, logical_order_id: str) -> BrokerOrderStatus:
        raise NotImplementedError("Paper order status is Phase 3")

    def cancel_order(self, logical_order_id: str) -> BrokerOrderStatus:
        raise NotImplementedError("Paper cancellation is Phase 3")
