from __future__ import annotations

import pytest
import respx
from httpx import Response as HttpxResponse

from ai_gateway.config import GatewayConfig
from ai_gateway.main import create_app


@pytest.fixture
def resilience_config() -> GatewayConfig:
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
                    "id": "primary",
                    "provider": "openai",
                    "base_url": "http://localhost:9001",
                    "api_key": "key1",
                    "weight": 1,
                    "models": ["gpt-3.5-turbo"],
                },
                {
                    "id": "backup",
                    "provider": "openai",
                    "base_url": "http://localhost:9002",
                    "api_key": "key2",
                    "weight": 1,
                    "models": ["gpt-3.5-turbo"],
                },
            ],
            "resilience": {
                "max_retries": 2,
                "retry_status_codes": [502, 503, 504],
                "timeout": 5,
                "fallback": {
                    "enabled": True,
                    "message": "all backends are down",
                },
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


def test_failover_to_backup(resilience_config: GatewayConfig) -> None:
    app = create_app(resilience_config)
    from fastapi.testclient import TestClient

    client = TestClient(app)

    with respx.mock:
        primary = respx.post("http://localhost:9001/v1/chat/completions").mock(
            return_value=HttpxResponse(503)
        )
        backup = respx.post("http://localhost:9002/v1/chat/completions").mock(
            return_value=HttpxResponse(200, json={"choices": [{"message": {"content": "backup ok"}}]})
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
        assert primary.call_count >= 1
        assert backup.call_count >= 1


def test_fallback_when_all_backends_fail(resilience_config: GatewayConfig) -> None:
    app = create_app(resilience_config)
    from fastapi.testclient import TestClient

    client = TestClient(app)

    with respx.mock:
        respx.post("http://localhost:9001/v1/chat/completions").mock(
            return_value=HttpxResponse(503)
        )
        respx.post("http://localhost:9002/v1/chat/completions").mock(
            return_value=HttpxResponse(503)
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
        assert response.json()["choices"][0]["message"]["content"] == "all backends are down"
