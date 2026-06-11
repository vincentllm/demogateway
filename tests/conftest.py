from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_gateway.config import GatewayConfig
from ai_gateway.main import create_app


@pytest.fixture
def minimal_config() -> GatewayConfig:
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
                        "limit": 5,
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
                "max_retries": 1,
                "retry_status_codes": [429, 502, 503, 504],
                "timeout": 5,
                "fallback": {
                    "enabled": True,
                    "message": "fallback message",
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


@pytest.fixture
def client(minimal_config: GatewayConfig) -> TestClient:
    app = create_app(minimal_config)
    return TestClient(app)
