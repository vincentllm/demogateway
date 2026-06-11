from __future__ import annotations

from typing import Any

import httpx
from fastapi.responses import Response

from ai_gateway.backend.pool import BackendInstance
from ai_gateway.providers.base import Provider


class ZaiProvider(Provider):
    """Placeholder provider for the 'zai' service.

    Implements the same Provider interface as OpenAIProvider so it can be
    registered and selected at runtime. The actual request/response mapping
    should be filled in once the zai API contract is known.
    """

    @property
    def name(self) -> str:
        return "zai"

    def supports(self, route: str, body: dict[str, Any] | None = None) -> bool:
        # Example: zai may expose a different chat endpoint.
        return route.startswith("/zai/")

    def transform_request(
        self,
        backend: BackendInstance,
        route: str,
        body: dict[str, Any],
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        target_url = backend.config.base_url.rstrip("/") + route
        headers = {}
        if backend.config.api_key:
            headers["authorization"] = f"Bearer {backend.config.api_key}"
        # TODO: map OpenAI body to zai format when contract is known.
        return target_url, body, headers

    def transform_response(
        self,
        raw: httpx.Response,
        request_body: dict[str, Any],
    ) -> Response:
        # TODO: map zai response to OpenAI-compatible format if desired.
        return Response(
            content=raw.content,
            status_code=raw.status_code,
            headers={"content-type": raw.headers.get("content-type", "application/json")},
        )
