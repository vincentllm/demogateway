from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse


class Provider(ABC):
    """Abstract interface for LLM service providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def supports(self, route: str, body: dict[str, Any] | None = None) -> bool:
        """Return True if this provider can handle the route."""
        raise NotImplementedError

    @abstractmethod
    def transform_request(
        self,
        backend: Any,
        route: str,
        body: dict[str, Any],
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        """Return (target_url, transformed_body, extra_headers)."""
        raise NotImplementedError

    @abstractmethod
    def transform_response(
        self,
        raw: httpx.Response,
        request_body: dict[str, Any],
    ) -> Response | StreamingResponse:
        raise NotImplementedError


class ProviderRegistry:
    """Holds available providers and picks one by name or route."""

    def __init__(self, providers: list[Provider]) -> None:
        self._by_name = {p.name: p for p in providers}

    def get(self, name: str) -> Provider | None:
        return self._by_name.get(name)

    def resolve(self, route: str, body: dict[str, Any] | None = None) -> Provider | None:
        for provider in self._by_name.values():
            if provider.supports(route, body):
                return provider
        return None
