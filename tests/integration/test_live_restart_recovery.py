"""Cross-process restart recovery for the single LIVE writer. Fake MCP only.

Each scenario closes the broker (simulating a crash/restart) and opens a brand-new
LiveBroker on the same durable journal. Nothing here touches Robinhood or the network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from swift_autotrader.broker.base import BrokerOrderState
from swift_autotrader.broker.live_broker import LiveBroker, LiveSafetyError
from tests.unit.test_live_broker import Clock, FakeMCPClient, entry


def _broker(path: Path, fake: FakeMCPClient, clock: Clock) -> LiveBroker:
    return LiveBroker(path, fake, account_number=fake.account, writes_enabled=True,
                      kill_switch=lambda: False, signal_gate=lambda _s: 'ENTRY',
                      clock=clock, loss_limit=None)


@pytest.fixture
def journal(tmp_path: Path) -> Path:
    path = tmp_path / 'live.sqlite'
    LiveBroker.create_journal(path)
    return path


def test_crash_after_broker_accepted_restart_never_resends(journal: Path) -> None:
    clock = Clock()
    fake = FakeMCPClient(clock)
    first = _broker(journal, fake, clock)
    fake.disconnect_place = 'after'  # the order reached the broker; the response was lost
    # Reconciliation against authoritative history finds the accepted order.
    assert first.submit_order(entry()).state is BrokerOrderState.ACCEPTED
    first.close()  # crash

    fake.disconnect_place = None
    restarted = _broker(journal, fake, clock)
    status = restarted.submit_order(entry())  # the same signal re-delivered after restart
    assert fake.place_count == 1
    assert len(fake.orders) == 1
    assert status.state is BrokerOrderState.ACCEPTED
    restarted.close()


def test_crash_before_send_restart_stays_unknown_and_never_places(journal: Path) -> None:
    clock = Clock()
    fake = FakeMCPClient(clock)
    first = _broker(journal, fake, clock)
    fake.disconnect_place = 'before'  # journal marked write_attempted; broker never saw it
    assert first.submit_order(entry()).state is BrokerOrderState.UNKNOWN
    first.close()

    fake.disconnect_place = None
    restarted = _broker(journal, fake, clock)
    # Reconciliation finds no matching broker order, so it must not claim absence nor resend.
    assert restarted.submit_order(entry()).state is BrokerOrderState.UNKNOWN
    # One attempt was made before the crash; the restart must not add a second one.
    assert fake.place_count == 1
    assert not fake.orders
    # With no broker id there is nothing safe to cancel: cancel must not guess an id.
    assert restarted.cancel_order(entry().logical_order_id).state is BrokerOrderState.UNKNOWN
    assert fake.cancel_count == 0
    restarted.close()


def test_restart_with_different_limit_for_same_signal_never_places_second_entry(
        journal: Path) -> None:
    clock = Clock()
    fake = FakeMCPClient(clock)
    first = _broker(journal, fake, clock)
    first.submit_order(entry())
    first.close()

    restarted = _broker(journal, fake, clock)
    # A re-sent or altered Swift price for the same signal is refused, never a second buy.
    with pytest.raises(LiveSafetyError, match='idempotency identity reused'):
        restarted.submit_order(entry(price='2.50', key='changed-key'))
    assert fake.place_count == 1
    assert len(fake.orders) == 1
    restarted.close()
