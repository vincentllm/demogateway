from __future__ import annotations

import pytest
import respx
from httpx import Response as HttpxResponse

from ai_gateway.config import GatewayConfig
from ai_gateway.main import create_app


@pytest.fixture
def proxy_config() -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            "server": {"host": "127.0.0.1", "port": 8000},
            "rate_limiter": {
                "policies": [
                    {
                        "name": "request-limit",
                        "enabled": True,
                        "algorithm": "fixed_window",
                        "granularity": "request",
                        "window_seconds": 60,
                        "limit": 100,
                    }
                ],
                "default_key_source": "api_key",
            },
            "tokenizer": {"type": "dummy", "tokens_per_message": 10, "overhead": 5},
            "backends": [
                {
                    "id": "openai-primary",
                    "provider": "openai",
                    "base_url": "http://localhost:9999",
                    "api_key": "test-key",
                    "weight": 1,
                    "models": ["gpt-3.5-turbo"],
                }
            ],
            "resilience": {
                "max_retries": 0,
                "retry_status_codes": [],
                "timeout": 5,
                "fallback": {"enabled": True, "message": "fallback"},
            },
            "health_check": {
                "interval_seconds": 60,
                "timeout_seconds": 1,
                "unhealthy_threshold": 2,
            },
            "load_metrics": {
                "latency_threshold_ms": 2000,
                "error_rate_threshold": 0.2,
                "window_size": 10,
            },
        }
    )


def test_chat_completions_proxy(proxy_config: GatewayConfig) -> None:
    app = create_app(proxy_config)
    from fastapi.testclient import TestClient

    client = TestClient(app)

    upstream_response = {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "created": 1677652288,
        "model": "gpt-3.5-turbo",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
    }

    with respx.mock:
        route = respx.post("http://localhost:9999/v1/chat/completions").mock(
            return_value=HttpxResponse(200, json=upstream_response)
        )
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers={"authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "Hello!"
        assert route.called


def test_chat_completions_rate_limit(proxy_config: GatewayConfig) -> None:
    proxy_config.rate_limiter.policies[0].limit = 2
    app = create_app(proxy_config)
    from fastapi.testclient import TestClient

    client = TestClient(app)

    with respx.mock:
        respx.post("http://localhost:9999/v1/chat/completions").mock(
            return_value=HttpxResponse(200, json={"choices": []})
        )
        for _ in range(2):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers={"authorization": "Bearer test-key"},
            )
            assert response.status_code == 200

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers={"authorization": "Bearer test-key"},
        )
        assert response.status_code == 429
