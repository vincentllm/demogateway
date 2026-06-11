from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any


class RateLimitStorage(ABC):
    """Backend storage for rate limit counters.

    Implementations can be in-memory (single process) or distributed (Redis).
    The storage is intentionally low-level: the algorithm decides the key
    schema and window semantics; the storage only stores (window, count).
    """

    @abstractmethod
    async def get(self, key: str) -> tuple[int, int] | None:
        """Return the stored (window, count) for ``key``, or None."""
        raise NotImplementedError

    @abstractmethod
    async def set(self, key: str, window: int, count: int) -> None:
        """Atomically set (window, count) for ``key``."""
        raise NotImplementedError

    @abstractmethod
    async def reset(self, key: str | None = None) -> None:
        """Reset state for a given key, or all keys if None."""
        raise NotImplementedError


class MemoryStorage(RateLimitStorage):
    """Thread-safe in-memory storage for rate limit counters."""

    def __init__(self) -> None:
        self._state: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    async def get(self, key: str) -> tuple[int, int] | None:
        with self._lock:
            return self._state.get(key)

    async def set(self, key: str, window: int, count: int) -> None:
        with self._lock:
            self._state[key] = (window, count)

    async def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._state.clear()
            else:
                self._state.pop(key, None)


class RedisStorage(RateLimitStorage):
    """Distributed Redis-backed storage for rate limit counters.

    This is a placeholder/production-ready implementation. It requires the
    optional ``redis`` package. The implementation uses a Lua script to
    atomically compare-and-set the counter within the correct window.
    """

    def __init__(self, redis_client: Any, key_prefix: str = "ai_gateway:rl") -> None:
        self._redis = redis_client
        self._key_prefix = key_prefix
        # Lua script atomically checks the current window and updates the count.
        self._acquire_script = self._redis.register_script(
            """
            local key = KEYS[1]
            local window = tonumber(ARGV[1])
            local cost = tonumber(ARGV[2])
            local limit = tonumber(ARGV[3])
            local current = redis.call('hmget', key, 'window', 'count')
            local current_window = tonumber(current[1])
            local current_count = tonumber(current[2]) or 0
            if current_window ~= window then
                current_count = 0
            end
            if current_count + cost > limit then
                return 0
            end
            redis.call('hmset', key, 'window', window, 'count', current_count + cost)
            redis.call('expire', key, 86400)
            return 1
            """
        )

    def _full_key(self, key: str) -> str:
        return f"{self._key_prefix}:{key}"

    async def get(self, key: str) -> tuple[int, int] | None:
        full = self._full_key(key)
        result = await self._redis.hmget(full, "window", "count")
        if result[0] is None:
            return None
        return int(result[0]), int(result[1] or 0)

    async def set(self, key: str, window: int, count: int) -> None:
        full = self._full_key(key)
        await self._redis.hmset(full, {"window": window, "count": count})
        await self._redis.expire(full, 86400)

    async def reset(self, key: str | None = None) -> None:
        if key is None:
            # Scan and delete all rate limit keys.
            pattern = f"{self._key_prefix}:*"
            async for k in self._redis.scan_iter(match=pattern):
                await self._redis.delete(k)
        else:
            await self._redis.delete(self._full_key(key))

    async def acquire(self, key: str, window: int, cost: int, limit: int) -> bool:
        """Atomic compare-and-set used by FixedWindowAlgorithm.

        This method is exposed on the storage because Redis can execute the
        entire acquire logic in one round trip via Lua.
        """
        full = self._full_key(key)
        allowed = await self._acquire_script(keys=[full], args=[window, cost, limit])
        return bool(allowed)
