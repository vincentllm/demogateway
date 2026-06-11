"""Integration tests that wire MockLLMBackendSender behind the gateway.

These tests demonstrate the intended deployment shape: the gateway exposes a
single OpenAI-compatible endpoint, and the mock backend services request it.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_gateway.config import BackendConfig, GatewayConfig
from ai_gateway.main import create_app
from ai_gateway.testing import MockLLMBackendSender, SenderBehavior


def _gateway_config_with_backend(base_url: str) -> GatewayConfig:
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
                    },
                    {
                        "name": "token-limit",
                        "enabled": True,
                        "algorithm": "fixed_window",
                        "granularity": "token",
                        "window_seconds": 60,
                        "limit": 10000,
                    },
                ],
                "default_key_source": "api_key",
            },
            "tokenizer": {"type": "dummy", "tokens_per_message": 10, "overhead": 5},
            "backends": [
                {
                    "id": "mock-backend",
                    "provider": "openai",
                    "base_url": base_url,
                    "api_key": "mock-api-key",
                    "weight": 1,
                    "models": ["gpt-4o-mini"],
                }
            ],
            "resilience": {
                "max_retries": 2,
                "retry_status_codes": [429, 502, 503, 504],
                "timeout": 5,
                "fallback": {"enabled": True, "message": "gateway fallback"},
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


def test_gateway_endpoint_uses_mock_backend() -> None:
    """A single external endpoint proxies to the mock backend."""
    mock_sender = MockLLMBackendSender(SenderBehavior(latency_ms=0))
    transport = httpx.MockTransport(mock_sender.as_transport_handler())
    http_client = httpx.AsyncClient(transport=transport)

    config = _gateway_config_with_backend("http://mock.backend")
    app = create_app(config, http_client=http_client)
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Hello!"}],
        },
        headers={"authorization": "Bearer client-key"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert "Hello!" in data["choices"][0]["message"]["content"]
    assert data["model"] == "gpt-4o-mini"
    assert len(mock_sender.request_log) == 1
    assert mock_sender.request_log[0].parsed_body["model"] == "gpt-4o-mini"


def test_gateway_fallback_when_mock_backend_fails() -> None:
    """When the mock backend fails, the gateway returns a fallback response."""
    mock_sender = MockLLMBackendSender(
        SenderBehavior(latency_ms=0, failure_rate=1.0, failure_mode="http_503")
    )
    transport = httpx.MockTransport(mock_sender.as_transport_handler())
    http_client = httpx.AsyncClient(transport=transport)

    config = _gateway_config_with_backend("http://mock.backend")
    app = create_app(config, http_client=http_client)
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Hello!"}],
        },
        headers={"authorization": "Bearer client-key"},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "gateway fallback"


def test_gateway_streaming_through_mock_backend() -> None:
    """The gateway can stream SSE chunks produced by the mock backend."""
    mock_sender = MockLLMBackendSender(SenderBehavior(latency_ms=0))
    transport = httpx.MockTransport(mock_sender.as_transport_handler())
    http_client = httpx.AsyncClient(transport=transport)

    config = _gateway_config_with_backend("http://mock.backend")
    app = create_app(config, http_client=http_client)
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Hello!"}],
            "stream": True,
        },
        headers={"authorization": "Bearer client-key"},
    )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert b"data: [DONE]" in response.content
