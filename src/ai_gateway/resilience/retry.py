from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryConfig:
    def __init__(self, max_retries: int, retry_status_codes: set[int], timeout: float) -> None:
        self.max_retries = max_retries
        self.retry_status_codes = retry_status_codes
        self.timeout = timeout


async def with_retry(
    operation: Callable[[], Awaitable[tuple[T, int]]],
    config: RetryConfig,
    on_retry: Callable[[int, int], Awaitable[None]] | None = None,
) -> tuple[T, int]:
    """Execute ``operation`` and retry on configured status codes.

    ``operation`` should return (result, status_code). Returns the last
    attempt's result even if it failed.
    """
    last_status = 0
    last_result: T | None = None
    for attempt in range(config.max_retries + 1):
        try:
            result, status = await asyncio.wait_for(operation(), timeout=config.timeout)
            last_result = result
            last_status = status
            if status not in config.retry_status_codes:
                return result, status
            if attempt < config.max_retries and on_retry:
                await on_retry(attempt, status)
        except asyncio.TimeoutError:
            last_status = 504
            logger.debug("Request timed out on attempt %s", attempt)
            if attempt < config.max_retries and on_retry:
                await on_retry(attempt, last_status)
        except Exception as exc:
            logger.debug("Request exception on attempt %s: %s", attempt, exc)
            last_status = 502
            if attempt < config.max_retries and on_retry:
                await on_retry(attempt, last_status)
    return last_result, last_status  # type: ignore[return-value]
