from __future__ import annotations

import threading
import time
from collections import deque
from enum import Enum

from pydantic import BaseModel


class BackendStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class LoadMetrics(BaseModel):
    """Snapshot of a backend's current load/pressure."""

    backend_id: str
    status: BackendStatus = BackendStatus.HEALTHY
    latency_ms: float = 0.0
    error_rate: float = 0.0
    inflight: int = 0
    last_success: float | None = None
    consecutive_failures: int = 0
    total_requests: int = 0

    def update_status(self, latency_threshold_ms: float, error_rate_threshold: float) -> None:
        if self.consecutive_failures > 0 and self.status != BackendStatus.UNHEALTHY:
            self.status = BackendStatus.DEGRADED
        if self.error_rate >= error_rate_threshold:
            self.status = BackendStatus.DEGRADED
        if self.latency_ms >= latency_threshold_ms:
            self.status = BackendStatus.DEGRADED


class RollingMetrics:
    """Thread-safe rolling metrics collector for a single backend."""

    def __init__(self, backend_id: str, window_size: int = 20) -> None:
        self.backend_id = backend_id
        self.window_size = window_size
        self._latencies: deque[float] = deque(maxlen=window_size)
        self._errors: deque[bool] = deque(maxlen=window_size)
        self._inflight = 0
        self._last_success: float | None = None
        self._consecutive_failures = 0
        self._total = 0
        self._lock = threading.Lock()

    def record_request_start(self) -> None:
        with self._lock:
            self._inflight += 1

    def record_request_end(
        self,
        latency_ms: float,
        success: bool,
        now: float | None = None,
    ) -> None:
        if now is None:
            now = time.time()
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            self._latencies.append(latency_ms)
            self._errors.append(not success)
            self._total += 1
            if success:
                self._last_success = now
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1

    def record_health_check(self, success: bool) -> None:
        with self._lock:
            if success:
                self._consecutive_failures = max(0, self._consecutive_failures - 1)
            else:
                self._consecutive_failures += 1

    def snapshot(
        self,
        latency_threshold_ms: float,
        error_rate_threshold: float,
    ) -> LoadMetrics:
        with self._lock:
            latencies = list(self._latencies)
            errors = list(self._errors)
            latency_ms = sum(latencies) / len(latencies) if latencies else 0.0
            error_rate = sum(errors) / len(errors) if errors else 0.0
            metrics = LoadMetrics(
                backend_id=self.backend_id,
                latency_ms=latency_ms,
                error_rate=error_rate,
                inflight=self._inflight,
                last_success=self._last_success,
                consecutive_failures=self._consecutive_failures,
                total_requests=self._total,
            )
        # Status evaluation happens outside the lock to keep the lock short.
        metrics.update_status(latency_threshold_ms, error_rate_threshold)
        if metrics.consecutive_failures >= 2:
            metrics.status = BackendStatus.UNHEALTHY
        return metrics
