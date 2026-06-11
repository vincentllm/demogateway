from __future__ import annotations

from typing import Any

from fastapi import Request

from ai_gateway.ratelimit.policy import RateLimitPolicy


class RateLimiterFacade:
    """Facade that composes multiple rate limit policies.

    A request is allowed only if every enabled policy allows it (AND semantics).
    """

    def __init__(self, policies: list[RateLimitPolicy]) -> None:
        self.policies = policies

    async def allow(self, request: Request, body: dict[str, Any] | None = None) -> tuple[bool, str | None]:
        """Return (allowed, blocking_policy_name).

        If allowed is False, blocking_policy_name is the name of the first policy
        that rejected the request.
        """
        for policy in self.policies:
            allowed, _ = await policy.allow(request, body)
            if not allowed:
                return False, policy.name
        return True, None

    def add_policy(self, policy: RateLimitPolicy) -> None:
        self.policies.append(policy)

    def reset(self, policy_name: str | None = None) -> None:
        for policy in self.policies:
            if policy_name is None or policy.name == policy_name:
                policy.algorithm.reset()
