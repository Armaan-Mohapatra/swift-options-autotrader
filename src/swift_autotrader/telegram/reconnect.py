"""Bounded reconnect policy; authorization/configuration errors are not retried."""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

FATAL_AUTH_EXCEPTIONS: frozenset[str] = frozenset({
    "LocalAuthorizationRequired",
    "AuthKeyUnregisteredError",
    "SessionPasswordNeededError",
    "UserDeactivatedError",
    "UserDeactivatedBanError",
    "SessionRevokedError",
    "SessionExpiredError",
})


def is_fatal_auth_error(exc: BaseException) -> bool:
    """Check if an exception is a fatal authorization revocation."""
    curr: BaseException | None = exc
    while curr is not None:
        if type(curr).__name__ in FATAL_AUTH_EXCEPTIONS:
            return True
        curr = curr.__cause__ or curr.__context__
    return False


def is_transient_network_error(exc: BaseException) -> bool:
    """Check if an exception represents a transient network disconnect."""
    if is_fatal_auth_error(exc):
        return False
    if isinstance(exc, (ConnectionError, TimeoutError, OSError, asyncio.TimeoutError)):
        return True
    name = type(exc).__name__
    return name in {
        "FloodWaitError",
        "TimedOutError",
        "DisconnectedError",
        "ConnectionError",
        "RpcError",
    }


def retry_delays(
    attempts: int,
    *,
    initial_seconds: float = 1.0,
    cap: float = 30.0,
    jitter: bool = False,
    max_jitter: float = 1.0,
) -> list[float]:
    if not 1 <= attempts <= 20 or initial_seconds <= 0 or cap <= 0:
        raise ValueError("invalid reconnect policy")
    base_delays = [min(cap, initial_seconds * (2**i)) for i in range(attempts - 1)]
    if jitter:
        return [min(cap, d + random.uniform(0.0, max_jitter)) for d in base_delays]
    return base_delays


async def with_reconnect(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    initial_seconds: float = 1.0,
    cap: float = 30.0,
    jitter: bool = False,
    max_jitter: float = 1.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    delays = retry_delays(
        attempts,
        initial_seconds=initial_seconds,
        cap=cap,
        jitter=jitter,
        max_jitter=max_jitter,
    )
    for delay in delays:
        try:
            return await operation()
        except BaseException as exc:
            if is_fatal_auth_error(exc):
                logging.getLogger("swift_autotrader.telegram").critical(
                    "Fatal authorization revocation: %s", exc
                )
                raise
            if not is_transient_network_error(exc):
                raise
            logging.getLogger("swift_autotrader.telegram").warning(
                "Transient network disconnect (%s); retrying in %.2fs",
                type(exc).__name__,
                delay,
            )
            await sleep(delay)
    return await operation()

