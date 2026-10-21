"""Bounded Prometheus metrics and deliberately projected lifecycle logs."""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest


@dataclass
class Handle:
    request_id: str
    client_id: str
    route: str
    provider: str
    streaming: bool
    start_time: float = field(default_factory=time.perf_counter)
    first_token_time: float | None = None
    finished: bool = False
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    _owner: Any = field(default=None, init=False, repr=False, compare=False)


_OUTCOMES = frozenset({"success", "cancelled", "upstream_error", "rate_limited", "timeout", "error", "client_error", "stream_error"})
_ERROR_TYPES = frozenset({"upstream_error", "timeout", "connection_error", "rate_limited", "cancelled", "validation_error", "internal_error", "stream_error"})
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _safe_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    return {key: usage[key] for key in _USAGE_KEYS if type(usage.get(key)) is int and 0 <= usage[key] <= 2**53 - 1}


def _safe_error(detail: Any) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {}
    projected: dict[str, Any] = {}
    status = detail.get("status_code")
    if type(status) is int and 100 <= status <= 599:
        projected["status_code"] = status
    kind = detail.get("type")
    if isinstance(kind, str) and kind in _ERROR_TYPES:
        projected["type"] = kind
    if type(detail.get("retryable")) is bool:
        projected["retryable"] = detail["retryable"]
    return projected


class _PrometheusMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        labels = ("route", "provider", "outcome")
        self.metrics = {
            "requests_total": Counter("requests_total", "Requests started", labels, registry=registry),
            "inprogress_requests": Gauge("inprogress_requests", "Active requests", labels, registry=registry),
            "request_latency_seconds": Histogram("request_latency_seconds", "Request latency", labels, registry=registry),
            "time_to_first_token_seconds": Histogram("time_to_first_token_seconds", "First meaningful streaming delta latency", labels, registry=registry),
            "token_usage_total": Counter("token_usage_total", "Reported total tokens", labels, registry=registry),
        }

    def inc(self, name: str, amount: int, labels: dict[str, str]) -> None:
        self.metrics[name].labels(**labels).inc(amount)

    def dec(self, name: str, amount: int, labels: dict[str, str]) -> None:
        self.metrics[name].labels(**labels).dec(amount)

    def observe(self, name: str, value: float, labels: dict[str, str]) -> None:
        self.metrics[name].labels(**labels).observe(value)


class Observability:
    """Each instance owns an isolated registry unless one is explicitly supplied.

    configured_routes contains trusted (route, provider) pairs. All other pairs
    collapse to unknown; request metadata must never expand this configuration.
    Recording registries with inc/dec/observe remain supported for consumers.
    """
    metrics_content_type = CONTENT_TYPE_LATEST

    def __init__(self, logger_name: str = "ai_gateway.observability", registry: Any | None = None,
                 *, configured_routes: Iterable[tuple[str, str]] | None = None):
        self.logger = logging.getLogger(logger_name)
        self._configured_routes = frozenset(configured_routes or ())
        self._prometheus_registry = registry if isinstance(registry, CollectorRegistry) else (CollectorRegistry() if registry is None else None)
        self._registry = _PrometheusMetrics(self._prometheus_registry) if self._prometheus_registry is not None else registry

    def render_metrics(self) -> bytes:
        if self._prometheus_registry is None:
            raise TypeError("Recording registry does not support Prometheus exposition")
        return generate_latest(self._prometheus_registry)

    def start(self, request_id: str, client_id: str, configured_route: dict[str, str], streaming: bool) -> Handle:
        route, provider = configured_route.get("route"), configured_route.get("provider")
        if not isinstance(route, str) or not isinstance(provider, str) or (route, provider) not in self._configured_routes:
            route, provider = "unknown", "unknown"
        handle = Handle(request_id, client_id, route, provider, streaming)
        handle._owner = self
        labels = {"route": route, "provider": provider, "outcome": "in_progress"}
        self._registry.inc("requests_total", 1, labels)
        self._registry.inc("inprogress_requests", 1, labels)
        self.logger.info(json.dumps({"event": "request_started", "route": route, "provider": provider, "streaming": bool(streaming)}))
        return handle

    def _check_owner(self, handle: Handle) -> None:
        if handle._owner is not self:
            raise ValueError("Lifecycle handle belongs to a different Observability instance")

    def first_token(self, handle: Handle, kind: str = "content") -> None:
        self._check_owner(handle)
        with handle._lock:
            if handle.finished or handle.first_token_time is not None or not handle.streaming or kind not in {"content", "tool_calls"}:
                return
            handle.first_token_time = time.perf_counter()
            self._registry.observe("time_to_first_token_seconds", handle.first_token_time - handle.start_time,
                                   {"route": handle.route, "provider": handle.provider, "outcome": "in_progress"})

    def finish(self, handle: Handle, outcome: str, usage: dict[str, int] | None = None, error_detail: Any | None = None) -> None:
        self._check_owner(handle)
        with handle._lock:
            if handle.finished:
                return
            handle.finished = True
            outcome = outcome if isinstance(outcome, str) and outcome in _OUTCOMES else "error"
            latency = time.perf_counter() - handle.start_time
            labels = {"route": handle.route, "provider": handle.provider, "outcome": outcome}
            self._registry.observe("request_latency_seconds", latency, labels)
            self._registry.dec("inprogress_requests", 1, {**labels, "outcome": "in_progress"})
            safe_usage = _safe_usage(usage)
            if "total_tokens" in safe_usage:
                self._registry.inc("token_usage_total", safe_usage["total_tokens"], labels)
            payload: dict[str, Any] = {"event": "request_finished", **labels, "latency_seconds": round(latency, 6)}
            if safe_usage:
                payload["usage"] = safe_usage
            safe_error = _safe_error(error_detail)
            if safe_error:
                payload["error"] = safe_error
            self.logger.info(json.dumps(payload))
