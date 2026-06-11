from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ai_gateway.backend.pool import BackendPool
from ai_gateway.config import GatewayConfig
from ai_gateway.models.openai import ChatCompletionRequest
from ai_gateway.providers.base import ProviderRegistry
from ai_gateway.ratelimit.facade import RateLimiterFacade
from ai_gateway.resilience.fallback import FallbackResponseBuilder
from ai_gateway.resilience.retry import RetryConfig, with_retry

logger = logging.getLogger(__name__)


def get_facade(request: Request) -> RateLimiterFacade:
    return request.app.state.facade


def get_pool(request: Request) -> BackendPool:
    return request.app.state.pool


def get_provider_registry(request: Request) -> ProviderRegistry:
    return request.app.state.providers


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def get_fallback_builder(request: Request) -> FallbackResponseBuilder:
    return request.app.state.fallback_builder


def get_retry_config(request: Request) -> RetryConfig:
    return request.app.state.retry_config


def get_config(request: Request) -> GatewayConfig:
    return request.app.state.config


router = APIRouter()


@router.get("/health")
async def gateway_health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/admin/backends")
async def list_backends(pool: BackendPool = Depends(get_pool)) -> dict[str, Any]:
    return {"backends": {bid: m.model_dump() for bid, m in pool.get_metrics().items()}}


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    facade: RateLimiterFacade = Depends(get_facade),
    pool: BackendPool = Depends(get_pool),
    providers: ProviderRegistry = Depends(get_provider_registry),
    client: httpx.AsyncClient = Depends(get_http_client),
    fallback_builder: FallbackResponseBuilder = Depends(get_fallback_builder),
    retry_config: RetryConfig = Depends(get_retry_config),
    config: GatewayConfig = Depends(get_config),
) -> Response:
    # 1. Rate limiting
    request_body = body.model_dump_for_provider()
    allowed, blocking_policy = await facade.allow(request, request_body)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {blocking_policy}",
            headers={"X-RateLimit-Policy": blocking_policy or ""},
        )

    # 2. Resolve provider
    provider = providers.resolve("/v1/chat/completions", request_body)
    if provider is None:
        raise HTTPException(status_code=400, detail="Unsupported provider or route")

    # 3. Proxy request with retry / failover
    response, status = await _proxy_with_retry(
        request_body,
        provider,
        pool,
        client,
        fallback_builder,
        retry_config,
        config.resilience.timeout,
    )

    # 4. Model downgrade fallback: if the model has a downgrade target and all
    # attempts for the original model failed, try the cheaper model once.
    model = request_body.get("model", "")
    if status in retry_config.retry_status_codes and model in config.resilience.fallback.model_downgrade:
        downgrade_body = {**request_body, "model": config.resilience.fallback.model_downgrade[model]}
        response, status = await _proxy_with_retry(
            downgrade_body,
            provider,
            pool,
            client,
            fallback_builder,
            retry_config,
            config.resilience.timeout,
        )

    if isinstance(response, JSONResponse):
        return response
    return response


async def _proxy_with_retry(
    request_body: dict[str, Any],
    provider: Any,
    pool: BackendPool,
    client: httpx.AsyncClient,
    fallback_builder: FallbackResponseBuilder,
    retry_config: RetryConfig,
    timeout: float,
) -> tuple[Response, int]:
    """Proxy a request to a selected backend, retrying on configured failures."""
    excluded: set[str] = set()
    last_backend_id: str | None = None

    async def attempt() -> tuple[Response, int]:
        nonlocal last_backend_id
        backend = pool.select(request_body.get("model"), excluded=excluded)
        if backend is None:
            return fallback_builder.build(request_body), 200

        last_backend_id = backend.config.id
        target_url, transformed_body, headers = provider.transform_request(
            backend, "/v1/chat/completions", request_body
        )
        start = time.perf_counter()
        pool.record_request_start(backend.config.id)
        try:
            upstream = await client.post(
                target_url,
                json=transformed_body,
                headers=headers,
                timeout=timeout,
            )
            latency_ms = (time.perf_counter() - start) * 1000
            pool.record_request_end(
                backend.config.id,
                latency_ms,
                upstream.is_success,
            )
            response = provider.transform_response(upstream, request_body)
            return response, upstream.status_code
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            pool.record_request_end(backend.config.id, latency_ms, False)
            logger.debug("Backend %s request failed: %s", backend.config.id, exc)
            raise

    async def on_retry(attempt_index: int, status: int) -> None:
        if last_backend_id:
            excluded.add(last_backend_id)
        logger.debug("Retrying after attempt %s with status %s", attempt_index, status)

    response, status = await with_retry(attempt, retry_config, on_retry)

    # If every retry still ended in a configured failure code, fall back to a
    # synthetic response rather than surfacing the upstream error directly.
    if status in retry_config.retry_status_codes:
        return fallback_builder.build(request_body), 200
    return response, status
