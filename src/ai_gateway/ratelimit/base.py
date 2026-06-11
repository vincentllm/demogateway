from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from fastapi import Request


class RateLimitAlgorithm(ABC):
    """Backend algorithm for rate limiting (e.g. fixed window, token bucket)."""

    @abstractmethod
    async def acquire(self, key: str, cost: int, now: float) -> bool:
        """Try to consume ``cost`` tokens for ``key`` at time ``now``.

        Returns True if the request is allowed, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def reset(self, key: str | None = None) -> None:
        """Reset state for a given key, or all keys if None."""
        raise NotImplementedError


class CostFunction(ABC):
    """Determines the cost of a single request for a rate limit policy."""

    @abstractmethod
    def calculate(self, request: Request, body: dict[str, Any] | None = None) -> int:
        """Return the integer cost of the request."""
        raise NotImplementedError


class KeyBuilder(ABC):
    """Builds the rate limit key for a request."""

    @abstractmethod
    def build(self, request: Request, policy_name: str, source: str = "api_key") -> str:
        """Return a unique key identifying the client for the policy."""
        raise NotImplementedError
