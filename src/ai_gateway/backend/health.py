from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ai_gateway.backend.load_metrics import BackendStatus, RollingMetrics
from ai_gateway.config import BackendConfig, HealthCheckConfig, LoadMetricsConfig

logger = logging.getLogger(__name__)


class BackendHealthMonitor:
    """Periodically probes backends and updates their health status."""

    def __init__(
        self,
        backends: list[BackendConfig],
        metrics_map: dict[str, RollingMetrics],
        health_config: HealthCheckConfig,
        load_config: LoadMetricsConfig,
        client: httpx.AsyncClient,
    ) -> None:
        self.backends = backends
        self.metrics_map = metrics_map
        self.health_config = health_config
        self.load_config = load_config
        self.client = client
        self._task: asyncio.Task[Any] | None = None
        self._stopped = False

    def start(self) -> None:
        if self._task is None:
            self._stopped = False
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopped:
            await self._check_all()
            await asyncio.sleep(self.health_config.interval_seconds)

    async def _check_all(self) -> None:
        for backend in self.backends:
            metrics = self.metrics_map.get(backend.id)
            if metrics is None:
                continue
            probe_url = self._probe_url(backend)
            try:
                response = await self.client.get(
                    probe_url,
                    timeout=self.health_config.timeout_seconds,
                )
                success = response.status_code < 500
            except Exception as exc:
                logger.debug("Health check failed for %s: %s", backend.id, exc)
                success = False
            metrics.record_health_check(success)
            snapshot = metrics.snapshot(
                self.load_config.latency_threshold_ms,
                self.load_config.error_rate_threshold,
            )
            if not success:
                snapshot.consecutive_failures += 1
            # Force unhealthy if health probe repeatedly fails.
            if snapshot.consecutive_failures >= self.health_config.unhealthy_threshold:
                snapshot.status = BackendStatus.UNHEALTHY
            logger.debug("Backend %s status: %s", backend.id, snapshot.status)

    @staticmethod
    def _probe_url(backend: BackendConfig) -> str:
        if backend.health_url:
            return backend.health_url
        return backend.base_url.rstrip("/") + "/health"
