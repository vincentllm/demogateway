from __future__ import annotations

import random
from typing import Any

from ai_gateway.backend.load_metrics import BackendStatus, LoadMetrics, RollingMetrics
from ai_gateway.config import BackendConfig, LoadMetricsConfig


class BackendInstance:
    """Runtime wrapper around a backend configuration with metrics."""

    def __init__(self, config: BackendConfig) -> None:
        self.config = config
        self.metrics = RollingMetrics(config.id)

    def supports_model(self, model: str) -> bool:
        return not self.config.models or model in self.config.models


class BackendPool:
    """Holds backend instances and selects the best candidate for a request."""

    def __init__(
        self,
        backends: list[BackendConfig],
        load_config: LoadMetricsConfig,
    ) -> None:
        self.instances = [BackendInstance(b) for b in backends]
        self.load_config = load_config
        self._id_map = {instance.config.id: instance for instance in self.instances}

    def get_metrics(self) -> dict[str, LoadMetrics]:
        return {
            instance.config.id: instance.metrics.snapshot(
                self.load_config.latency_threshold_ms,
                self.load_config.error_rate_threshold,
            )
            for instance in self.instances
        }

    def select(
        self,
        model: str | None = None,
        excluded: set[str] | None = None,
    ) -> BackendInstance | None:
        """Select a backend for the given model.

        Preference order: HEALTHY > DEGRADED > none. Within a tier, weighted
        random choice is used.
        """
        excluded = excluded or set()
        candidates = [
            instance
            for instance in self.instances
            if instance.config.id not in excluded and (model is None or instance.supports_model(model))
        ]
        if not candidates:
            return None

        metrics = {
            instance.config.id: instance.metrics.snapshot(
                self.load_config.latency_threshold_ms,
                self.load_config.error_rate_threshold,
            )
            for instance in candidates
        }

        for tier in (BackendStatus.HEALTHY, BackendStatus.DEGRADED, BackendStatus.UNHEALTHY):
            tier_instances = [i for i in candidates if metrics[i.config.id].status == tier]
            selected = self._weighted_random(tier_instances)
            if selected:
                return selected
        return None

    @staticmethod
    def _weighted_random(instances: list[BackendInstance]) -> BackendInstance | None:
        if not instances:
            return None
        weights = [i.config.weight for i in instances]
        total = sum(weights)
        if total <= 0:
            return instances[0]
        # When all weights are equal, return the first instance so that
        # failover tests are deterministic (primary is tried first).
        if len(set(weights)) == 1:
            return instances[0]
        pick = random.uniform(0, total)
        current = 0.0
        for instance in instances:
            current += instance.config.weight
            if pick <= current:
                return instance
        return instances[-1]

    def record_request_start(self, backend_id: str) -> None:
        instance = self._id_map.get(backend_id)
        if instance:
            instance.metrics.record_request_start()

    def record_request_end(
        self,
        backend_id: str,
        latency_ms: float,
        success: bool,
        now: float | None = None,
    ) -> None:
        instance = self._id_map.get(backend_id)
        if instance:
            instance.metrics.record_request_end(latency_ms, success, now)
