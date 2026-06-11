from __future__ import annotations

import time

import pytest
from fastapi import Request
from starlette.datastructures import Headers

from ai_gateway.config import GatewayConfig, RateLimitPolicyConfig
from ai_gateway.ratelimit.facade import RateLimiterFacade
from ai_gateway.ratelimit.fixed_window import FixedWindowAlgorithm
from ai_gateway.ratelimit.policy import DefaultKeyBuilder, RateLimitPolicy
from ai_gateway.ratelimit.request_cost import RequestCostFunction
from ai_gateway.ratelimit.token_cost import TokenCostFunction
from ai_gateway.tokenizer.dummy import DummyTokenizer

def build_request(api_key: str = "test-key") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": Headers({"authorization": f"Bearer {api_key}"}).raw,
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_fixed_window_request_limit() -> None:
    algo = FixedWindowAlgorithm(window_seconds=60, limit=3)
    now = time.time()
    assert await algo.acquire("user-a", 1, now)
    assert await algo.acquire("user-a", 1, now)
    assert await algo.acquire("user-a", 1, now)
    assert not await algo.acquire("user-a", 1, now)

    # Different key unaffected
    assert await algo.acquire("user-b", 1, now)


@pytest.mark.asyncio
async def test_fixed_window_reset() -> None:
    algo = FixedWindowAlgorithm(window_seconds=60, limit=1)
    now = time.time()
    assert await algo.acquire("user-a", 1, now)
    assert not await algo.acquire("user-a", 1, now)
    await algo.reset("user-a")
    assert await algo.acquire("user-a", 1, now)


def test_token_cost_function() -> None:
    tokenizer = DummyTokenizer(tokens_per_message=10, overhead=5)
    cost_fn = TokenCostFunction(tokenizer)
    request = build_request()
    body = {"messages": [{}, {}, {}]}
    assert cost_fn.calculate(request, body) == 35


@pytest.mark.asyncio
async def test_facade_combines_policies_and_blocks_on_first_failure() -> None:
    tokenizer = DummyTokenizer(tokens_per_message=10, overhead=5)
    request = build_request()
    body = {"messages": [{}, {}]}  # 25 tokens

    request_policy = RateLimitPolicy(
        name="request-limit",
        algorithm=FixedWindowAlgorithm(window_seconds=60, limit=1),
        cost_function=RequestCostFunction(),
        key_builder=DefaultKeyBuilder(),
        key_source="api_key",
    )
    token_policy = RateLimitPolicy(
        name="token-limit",
        algorithm=FixedWindowAlgorithm(window_seconds=60, limit=100),
        cost_function=TokenCostFunction(tokenizer),
        key_builder=DefaultKeyBuilder(),
        key_source="api_key",
    )

    facade = RateLimiterFacade([request_policy, token_policy])

    # First request exhausts request-limit (limit=1); token-limit has room.
    assert (await facade.allow(request, body))[0]
    allowed, blocking = await facade.allow(request, body)
    assert not allowed
    assert blocking == "request-limit"


@pytest.mark.asyncio
async def test_facade_allows_when_all_policies_pass() -> None:
    tokenizer = DummyTokenizer(tokens_per_message=1, overhead=0)
    request = build_request()
    body = {"messages": [{}]}  # 1 token

    request_policy = RateLimitPolicy(
        name="request-limit",
        algorithm=FixedWindowAlgorithm(window_seconds=60, limit=5),
        cost_function=RequestCostFunction(),
        key_builder=DefaultKeyBuilder(),
        key_source="api_key",
    )
    token_policy = RateLimitPolicy(
        name="token-limit",
        algorithm=FixedWindowAlgorithm(window_seconds=60, limit=5),
        cost_function=TokenCostFunction(tokenizer),
        key_builder=DefaultKeyBuilder(),
        key_source="api_key",
    )

    facade = RateLimiterFacade([request_policy, token_policy])
    for _ in range(5):
        assert (await facade.allow(request, body))[0]
