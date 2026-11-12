"""Bounded reconnect policy; authorization/configuration errors are not retried."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


def retry_delays(attempts: int, *, initial_seconds: float = 1.0, cap: float = 30.0) -> list[float]:
    if not 1 <= attempts <= 20 or initial_seconds <= 0 or cap <= 0:
        raise ValueError("invalid reconnect policy")
    return [min(cap, initial_seconds * (2**i)) for i in range(attempts - 1)]


async def with_reconnect(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    delays = retry_delays(attempts)
    for delay in delays:
        try:
            return await operation()
        except (ConnectionError, TimeoutError, OSError):
            await sleep(delay)
    return await operation()
