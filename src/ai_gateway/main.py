from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI

from ai_gateway.api.router import router
from ai_gateway.backend.health import BackendHealthMonitor
from ai_gateway.backend.pool import BackendPool
from ai_gateway.config import (
    GatewayConfig,
    RateLimitPolicyConfig,
    StorageConfig,
    load_config,
)
from ai_gateway.providers import BUILTIN_PROVIDERS, ProviderRegistry
from ai_gateway.ratelimit.facade import RateLimiterFacade
from ai_gateway.ratelimit.fixed_window import FixedWindowAlgorithm
from ai_gateway.ratelimit.policy import DefaultKeyBuilder, RateLimitPolicy
from ai_gateway.ratelimit.request_cost import RequestCostFunction
from ai_gateway.ratelimit.storage import MemoryStorage, RateLimitStorage, RedisStorage
from ai_gateway.ratelimit.token_cost import TokenCostFunction
from ai_gateway.resilience.fallback import FallbackResponseBuilder
from ai_gateway.resilience.retry import RetryConfig
from ai_gateway.tokenizer.dummy import DummyTokenizer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def build_facade(config: GatewayConfig, tokenizer: DummyTokenizer) -> RateLimiterFacade:
    storage = build_storage(config.rate_limiter.storage)
    policies: list[RateLimitPolicy] = []
    for policy_config in config.rate_limiter.policies:
        policy = build_policy(policy_config, config, tokenizer, storage)
        policies.append(policy)
    return RateLimiterFacade(policies)


def build_storage(storage_config: StorageConfig) -> RateLimitStorage:
    """Build the rate limit storage backend from configuration."""
    if storage_config.type == "redis":
        try:
            import redis.asyncio as redis
        except ImportError as exc:
            raise RuntimeError(
                "Redis storage requires the 'redis' package. "
                "Install it with: pip install redis"
            ) from exc
        redis_client = redis.Redis(
            host=storage_config.redis.host,
            port=storage_config.redis.port,
            db=storage_config.redis.db,
            password=storage_config.redis.password,
            decode_responses=True,
        )
        return RedisStorage(redis_client, key_prefix=storage_config.redis.key_prefix)
    return MemoryStorage()


def build_policy(
    policy_config: RateLimitPolicyConfig,
    config: GatewayConfig,
    tokenizer: DummyTokenizer,
    storage: RateLimitStorage,
) -> RateLimitPolicy:
    if policy_config.algorithm == "fixed_window":
        algorithm = FixedWindowAlgorithm(
            window_seconds=policy_config.window_seconds,
            limit=policy_config.limit,
            storage=storage,
        )
    else:
        raise ValueError(f"Unsupported rate limit algorithm: {policy_config.algorithm}")

    if policy_config.granularity == "token":
        cost_function = TokenCostFunction(tokenizer)
    else:
        cost_function = RequestCostFunction()

    return RateLimitPolicy(
        name=policy_config.name,
        algorithm=algorithm,
        cost_function=cost_function,
        key_builder=DefaultKeyBuilder(),
        key_source=config.rate_limiter.default_key_source,
        enabled=policy_config.enabled,
    )


def create_app(
    config: GatewayConfig | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    if config is None:
        config = load_config()

    tokenizer = DummyTokenizer(
        tokens_per_message=config.tokenizer.tokens_per_message,
        overhead=config.tokenizer.overhead,
    )
    facade = build_facade(config, tokenizer)
    pool = BackendPool(config.backends, config.load_metrics)
    providers = ProviderRegistry(BUILTIN_PROVIDERS)
    if http_client is None:
        http_client = httpx.AsyncClient()
    fallback_builder = FallbackResponseBuilder(config.resilience.fallback.message)
    retry_config = RetryConfig(
        max_retries=config.resilience.max_retries,
        retry_status_codes=set(config.resilience.retry_status_codes),
        timeout=config.resilience.timeout,
    )

    health_monitor = BackendHealthMonitor(
        backends=config.backends,
        metrics_map={instance.config.id: instance.metrics for instance in pool.instances},
        health_config=config.health_check,
        load_config=config.load_metrics,
        client=http_client,
    )

    app = FastAPI(title="AI Gateway")
    app.state.config = config
    app.state.facade = facade
    app.state.pool = pool
    app.state.providers = providers
    app.state.http_client = http_client
    app.state.fallback_builder = fallback_builder
    app.state.retry_config = retry_config
    app.state.tokenizer = tokenizer
    app.state.health_monitor = health_monitor

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        health_monitor.start()
        logger.info("AI Gateway started on %s:%s", config.server.host, config.server.port)
        yield
        await health_monitor.stop()
        await http_client.aclose()
        logger.info("AI Gateway shutdown complete")

    app.router.lifespan_context = lifespan
    app.include_router(router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    config = load_config()
    uvicorn.run(
        "ai_gateway.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=True,
    )
