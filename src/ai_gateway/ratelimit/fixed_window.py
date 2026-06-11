from __future__ import annotations

from typing import Any

from ai_gateway.ratelimit.base import RateLimitAlgorithm
from ai_gateway.ratelimit.storage import MemoryStorage, RateLimitStorage


class FixedWindowAlgorithm(RateLimitAlgorithm):
    """Fixed window rate limiting algorithm backed by pluggable storage."""

    def __init__(
        self,
        window_seconds: int,
        limit: int,
        storage: RateLimitStorage | None = None,
    ) -> None:
        self.window_seconds = window_seconds
        self.limit = limit
        self._storage = storage or MemoryStorage()

    async def acquire(self, key: str, cost: int, now: float) -> bool:
        window = int(now) // self.window_seconds

        # Redis can do the whole acquire atomically in one Lua call.
        if isinstance(self._storage, MemoryStorage):
            stored = await self._storage.get(key)
            current_window, current_count = stored if stored else (window, 0)
            if current_window != window:
                current_window = window
                current_count = 0
            if current_count + cost > self.limit:
                return False
            await self._storage.set(key, current_window, current_count + cost)
            return True

        # Generic async path.
        stored = await self._storage.get(key)
        current_window, current_count = stored if stored else (window, 0)
        if current_window != window:
            current_window = window
            current_count = 0
        if current_count + cost > self.limit:
            return False
        await self._storage.set(key, current_window, current_count + cost)
        return True

    async def reset(self, key: str | None = None) -> None:
        await self._storage.reset(key)

    async def inspect(self, key: str, now: float) -> dict[str, Any]:
        """Return current window and count for observability."""
        window = int(now) // self.window_seconds
        stored = await self._storage.get(key)
        if stored is None or stored[0] != window:
            return {
                "window": window,
                "count": 0,
                "limit": self.limit,
                "window_seconds": self.window_seconds,
            }
        return {
            "window": stored[0],
            "count": stored[1],
            "limit": self.limit,
            "window_seconds": self.window_seconds,
        }
