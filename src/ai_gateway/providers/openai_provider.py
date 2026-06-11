from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

from ai_gateway.backend.pool import BackendInstance
from ai_gateway.providers.base import Provider


class OpenAIProvider(Provider):
    """Proxies OpenAI-compatible endpoints."""

    @property
    def name(self) -> str:
        return "openai"

    def supports(self, route: str, body: dict[str, Any] | None = None) -> bool:
        return route in {"/v1/chat/completions", "/v1/completions"}

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
        return target_url, body, headers

    def transform_response(
        self,
        raw: httpx.Response,
        request_body: dict[str, Any],
    ) -> Response | StreamingResponse:
        content_type = raw.headers.get("content-type", "application/json")
        if request_body.get("stream"):
            # If the upstream response has already been consumed (e.g. by a mock
            # transport), stream the buffered content; otherwise stream directly.
            if raw.is_stream_consumed:
                body_iter = iter([raw.content])
            else:
                body_iter = raw.aiter_raw()
            return StreamingResponse(
                content=body_iter,
                status_code=raw.status_code,
                headers={"content-type": content_type},
            )
        return Response(
            content=raw.content,
            status_code=raw.status_code,
            headers={"content-type": content_type},
        )
