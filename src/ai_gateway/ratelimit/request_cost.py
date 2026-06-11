from __future__ import annotations

from typing import Any

from fastapi import Request

from ai_gateway.ratelimit.base import CostFunction


class RequestCostFunction(CostFunction):
    """Each request consumes exactly one unit."""

    def calculate(self, request: Request, body: dict[str, Any] | None = None) -> int:
        return 1
