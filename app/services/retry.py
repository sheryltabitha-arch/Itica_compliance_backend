"""
app/services/retry.py

Exponential backoff with jitter, for any flaky I/O — vendor API calls,
Supabase writes during webhook fan-out, anything that can transiently fail.

This was the original gap flagged against webhook.py and the connector
framework: every Supabase call was a bare .execute() with no retry, and
outbound vendor calls had no resilience pattern at all. This is the fix.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryExhausted(Exception):
    """Raised when all retry attempts fail. Wraps the last underlying exception."""
    def __init__(self, attempts: int, last_error: Exception):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"Failed after {attempts} attempts: {last_error}")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    retryable: tuple[type[Exception], ...] = (Exception,),
    label: str = "operation",
) -> T:
    """
    Calls an async no-arg callable with exponential backoff + jitter.

    Usage:
        result = await with_retry(
            lambda: supabase.table("audit_events").insert(row).execute(),
            label="audit_events insert",
        )

    base_delay doubles each attempt (0.5s, 1s, 2s, 4s...) capped at
    max_delay, with up to 25% random jitter added so concurrent callers
    don't retry in lockstep against the same downstream service.
    """
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except retryable as e:
            last_error = e
            if attempt == max_attempts:
                logger.error(f"{label}: giving up after {attempt} attempts — {e}")
                raise RetryExhausted(attempt, e) from e

            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            jitter = delay * random.uniform(0, 0.25)
            wait = delay + jitter
            logger.warning(
                f"{label}: attempt {attempt}/{max_attempts} failed ({e}), "
                f"retrying in {wait:.1f}s"
            )
            await asyncio.sleep(wait)

    # unreachable, but keeps type checkers happy
    raise RetryExhausted(max_attempts, last_error or Exception("unknown error"))
