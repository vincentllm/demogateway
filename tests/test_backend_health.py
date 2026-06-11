from __future__ import annotations

import time

from ai_gateway.backend.load_metrics import BackendStatus
from ai_gateway.backend.pool import BackendPool
from ai_gateway.config import BackendConfig, LoadMetricsConfig


def make_pool() -> BackendPool:
    backends = [
        BackendConfig(
            id="fast",
            provider="openai",
            base_url="http://localhost:1",
            weight=1,
            models=["gpt-3.5-turbo"],
        ),
        BackendConfig(
            id="slow",
            provider="openai",
            base_url="http://localhost:2",
            weight=1,
            models=["gpt-3.5-turbo"],
        ),
    ]
    load_config = LoadMetricsConfig(
        latency_threshold_ms=500,
        error_rate_threshold=0.5,
        window_size=10,
    )
    return BackendPool(backends, load_config)


def test_pool_selects_healthy_backend() -> None:
    pool = make_pool()
    for _ in range(5):
        pool.record_request_end("fast", latency_ms=50, success=True)
    for _ in range(5):
        pool.record_request_end("slow", latency_ms=1000, success=True)

    selected = {pool.select("gpt-3.5-turbo").config.id for _ in range(20)}
    # slow backend exceeds latency threshold and becomes degraded, fast remains healthy
    assert "fast" in selected


def test_pool_excludes_unhealthy() -> None:
    pool = make_pool()
    for _ in range(5):
        pool.record_request_end("slow", latency_ms=100, success=False)
    pool.record_request_end("fast", latency_ms=100, success=True)

    selected = pool.select("gpt-3.5-turbo")
    assert selected is not None
    assert selected.config.id == "fast"


def test_metrics_snapshot_status() -> None:
    pool = make_pool()
    for _ in range(5):
        pool.record_request_end("slow", latency_ms=100, success=False)

    metrics = pool.get_metrics()["slow"]
    assert metrics.status == BackendStatus.UNHEALTHY
    assert metrics.error_rate == 1.0
