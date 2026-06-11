from __future__ import annotations

from typing import Any

from fastapi import Request

from ai_gateway.ratelimit.base import CostFunction
from ai_gateway.tokenizer.dummy import Tokenizer


class TokenCostFunction(CostFunction):
    """Request cost is determined by the configured tokenizer."""

    def __init__(self, tokenizer: Tokenizer) -> None:
        self.tokenizer = tokenizer

    def calculate(self, request: Request, body: dict[str, Any] | None = None) -> int:
        return self.tokenizer.count(body or {})
