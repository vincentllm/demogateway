from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Tokenizer(ABC):
    """Abstract tokenizer used by the token-granularity rate limiter."""

    @abstractmethod
    def count(self, request_body: dict[str, Any]) -> int:
        """Return the estimated token cost for the request."""
        raise NotImplementedError


class DummyTokenizer(Tokenizer):
    """A dummy tokenizer that estimates token count from message count.

    It is intentionally simplistic: in production this would be replaced by
    tiktoken or a provider-specific tokenizer.
    """

    def __init__(self, tokens_per_message: int = 10, overhead: int = 5) -> None:
        self.tokens_per_message = tokens_per_message
        self.overhead = overhead

    def count(self, request_body: dict[str, Any]) -> int:
        messages = request_body.get("messages", [])
        if not isinstance(messages, list):
            return self.overhead
        return len(messages) * self.tokens_per_message + self.overhead
