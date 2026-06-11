from __future__ import annotations

from typing import Any

from fastapi import Request

from ai_gateway.ratelimit.base import CostFunction, KeyBuilder, RateLimitAlgorithm


class DefaultKeyBuilder(KeyBuilder):
    """Builds a rate limit key from the configured source and policy name."""

    def build(self, request: Request, policy_name: str, source: str = "api_key") -> str:
        if source == "ip":
            client = request.client
            identity = client.host if client else "unknown"
        else:
            # Use Authorization header as the API key source.
            auth = request.headers.get("authorization", "")
            identity = auth.split()[-1] if auth else "anonymous"
        return f"{policy_name}:{source}:{identity}"


class RateLimitPolicy:
    """An atomic rate limit policy that combines algorithm, cost, and key."""

    def __init__(
        self,
        name: str,
        algorithm: RateLimitAlgorithm,
        cost_function: CostFunction,
        key_builder: KeyBuilder,
        key_source: str,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.algorithm = algorithm
        self.cost_function = cost_function
        self.key_builder = key_builder
        self.key_source = key_source
        self.enabled = enabled

    async def allow(self, request: Request, body: dict[str, Any] | None = None) -> tuple[bool, int]:
        """Return (allowed, cost)."""
        if not self.enabled:
            return True, 0
        cost = self.cost_function.calculate(request, body)
        key = self.key_builder.build(request, self.name, self.key_source)
        now = request.scope.get("state", {}).get("now")  # injected in tests
        if now is None:
            import time

            now = time.time()
        allowed = await self.algorithm.acquire(key, cost, now)
        return allowed, cost
