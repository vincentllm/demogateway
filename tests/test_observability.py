"""Observability lifecycle tests (GW-005).

These tests exercise the observability interface with synthetic request
lifecycles.  They do not depend on Starlette middleware, real upstream
providers, or other GW modules.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest


class FakeRegistry:
    """Minimal registry implementation used to assert label shapes."""

    def __init__(self) -> None:
        self.samples: list[tuple[str, str, float | int, dict[str, str]]] = []

    def inc(self, name: str, amount: int, labels: dict[str, str]) -> None:
        self.samples.append(("inc", name, amount, labels))

    def dec(self, name: str, amount: int, labels: dict[str, str]) -> None:
        self.samples.append(("dec", name, amount, labels))

    def observe(self, name: str, value: float, labels: dict[str, str]) -> None:
        self.samples.append(("observe", name, value, labels))


class JsonCapture(logging.Handler):
    """Capture log records formatted as JSON strings."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(json.loads(self.format(record)))


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def observability(registry: FakeRegistry) -> Any:
    from ai_gateway.observability.lifecycle import Observability

    return Observability(registry=registry, configured_routes={("chat", provider) for provider in ("openai", "claude", "deepseek")})


@pytest.fixture
def json_logger(observability: Any) -> JsonCapture:
    capture = JsonCapture()
    capture.setFormatter(logging.Formatter("%(message)s"))
    observability.logger.addHandler(capture)
    observability.logger.setLevel(logging.INFO)
    yield capture
    observability.logger.removeHandler(capture)


def _find(samples, op, name):
    return [s for s in samples if s[0] == op and s[1] == name]


def test_start_records_in_progress_and_request_counter(
    observability: Any,
    registry: FakeRegistry,
    json_logger: JsonCapture,
) -> None:
    handle = observability.start(
        request_id="req-1",
        client_id="alice",
        configured_route={"provider": "openai", "route": "chat"},
        streaming=False,
    )
    assert handle is not None

    incs = _find(registry.samples, "inc", "requests_total")
    assert len(incs) == 1
    assert incs[0][3] == {"route": "chat", "provider": "openai", "outcome": "in_progress"}

    gauges = _find(registry.samples, "inc", "inprogress_requests")
    assert len(gauges) == 1

    assert json_logger.records[0]["event"] == "request_started"
    assert "req-1" not in json_logger.records[0].get("labels", {})


def test_first_token_records_ttft_only_on_content_delta(
    observability: Any,
    registry: FakeRegistry,
) -> None:
    handle = observability.start(
        request_id="req-2",
        client_id="alice",
        configured_route={"provider": "claude", "route": "chat"},
        streaming=True,
    )
    # A heartbeat/comment must not advance TTFT.
    observability.first_token(handle, kind="heartbeat")
    assert _find(registry.samples, "observe", "time_to_first_token_seconds") == []

    observability.first_token(handle, kind="content")
    ttft = _find(registry.samples, "observe", "time_to_first_token_seconds")
    assert len(ttft) == 1
    assert ttft[0][3] == {"route": "chat", "provider": "claude", "outcome": "in_progress"}


def test_finish_idempotent_and_in_progress_zero(
    observability: Any,
    registry: FakeRegistry,
    json_logger: JsonCapture,
) -> None:
    handle = observability.start(
        request_id="req-3",
        client_id="bob",
        configured_route={"provider": "openai", "route": "chat"},
        streaming=False,
    )
    usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    observability.finish(handle, outcome="success", usage=usage)
    observability.finish(handle, outcome="success", usage=usage)

    decs = _find(registry.samples, "dec", "inprogress_requests")
    assert len(decs) == 1

    usage_samples = _find(registry.samples, "inc", "token_usage_total")
    assert len(usage_samples) == 1
    assert usage_samples[0][2] == 8

    finished = [r for r in json_logger.records if r.get("event") == "request_finished"]
    assert len(finished) == 1


def test_cancelled_finish_zeros_in_progress(
    observability: Any,
    registry: FakeRegistry,
) -> None:
    handle = observability.start(
        request_id="req-4",
        client_id="alice",
        configured_route={"provider": "deepseek", "route": "chat"},
        streaming=True,
    )
    observability.finish(handle, outcome="cancelled", usage=None)
    decs = _find(registry.samples, "dec", "inprogress_requests")
    assert len(decs) == 1


def test_labels_do_not_include_high_cardinality_fields(
    observability: Any,
    registry: FakeRegistry,
) -> None:
    handle = observability.start(
        request_id="req-5",
        client_id="alice",
        configured_route={"provider": "openai", "route": "chat"},
        streaming=False,
    )
    observability.finish(handle, outcome="upstream_error", usage=None)
    for _op, _name, _value, labels in registry.samples:
        assert "request_id" not in labels
        assert "client_id" not in labels
        assert "model" not in labels
        assert set(labels.keys()) <= {"route", "provider", "outcome"}


def test_logs_do_not_leak_secrets(
    observability: Any,
    json_logger: JsonCapture,
) -> None:
    handle = observability.start(
        request_id="req-6",
        client_id="alice",
        configured_route={"provider": "openai", "route": "chat"},
        streaming=False,
    )
    observability.finish(
        handle,
        outcome="upstream_error",
        usage=None,
        error_detail={
            "authorization": "Bearer sk-secret",
            "body": {"messages": [{"role": "user", "content": "my password is 123"}]},
            "upstream": "Internal error with api-key=abc",
        },
    )
    text = json.dumps(json_logger.records)
    assert "sk-secret" not in text
    assert "my password" not in text
    assert "api-key=abc" not in text


@pytest.mark.parametrize("outcome", ["success", "cancelled", "upstream_error", "timeout", "unexpected secret"])
def test_gauge_balances_on_exact_series(observability, registry, outcome):
    handle = observability.start("r", "c", {"route": "chat", "provider": "openai"}, True)
    observability.finish(handle, outcome)
    totals = {}
    for op, name, value, labels in registry.samples:
        if name == "inprogress_requests":
            key = tuple(sorted(labels.items()))
            totals[key] = totals.get(key, 0) + (value if op == "inc" else -value)
    assert set(totals.values()) == {0}


@pytest.mark.parametrize("detail", ["Bearer SENTINEL", {"message": "SENTINEL", "type": "SENTINEL", "status_code": "SENTINEL"}, {"nested": {"unknown": "SENTINEL"}}])
def test_free_text_never_logged(observability, json_logger, detail):
    handle = observability.start("SENTINEL", "SENTINEL", {"route": "SENTINEL", "provider": "SENTINEL"}, False)
    observability.finish(handle, "SENTINEL", usage={"total_tokens": True, "prompt_tokens": -1, "completion_tokens": "SENTINEL", "extra": "SENTINEL"}, error_detail=detail)
    text = json.dumps(json_logger.records)
    assert "SENTINEL" not in text
    assert "usage" not in json_logger.records[-1]


def test_safe_projected_metadata(observability, json_logger):
    handle = observability.start("r", "c", {}, False)
    observability.finish(handle, "error", {"total_tokens": 4, "prompt_tokens": 2, "arbitrary": "secret"},
                         {"status_code": 503, "type": "upstream_error", "retryable": True, "message": "secret"})
    assert json_logger.records[-1]["usage"] == {"total_tokens": 4, "prompt_tokens": 2}
    assert json_logger.records[-1]["error"] == {"status_code": 503, "type": "upstream_error", "retryable": True}


def test_real_prometheus_isolated_bounded_and_balanced():
    from ai_gateway.observability import Observability
    from prometheus_client.parser import text_string_to_metric_families
    obs = Observability(configured_routes={("chat", "openai")})
    def scrape():
        return [sample for family in text_string_to_metric_families(obs.render_metrics().decode()) for sample in family.samples]
    for index in range(100):
        handle = obs.start(str(index), "c", {"route": f"user-{index}", "provider": f"provider-{index}"}, True)
        obs.first_token(handle)
        obs.finish(handle, f"error-{index}", {"total_tokens": 2})
    samples = scrape()
    assert next(s.value for s in samples if s.name == "inprogress_requests") == 0
    assert next(s.value for s in samples if s.name == "requests_total") == 100
    assert next(s.value for s in samples if s.name == "token_usage_total") == 200
    assert {s.labels["route"] for s in samples} == {"unknown"}
    assert not hasattr(obs._registry, "_samples")
    assert b"requests_total{" not in Observability().render_metrics()
    assert "text/plain" in obs.metrics_content_type
    count = len(samples)
    for index in range(100):
        obs.finish(obs.start("r", "c", {"route": str(index), "provider": "new"}, False), "new")
    assert len(scrape()) == count


def test_concurrent_finish_and_late_token(observability, registry):
    from concurrent.futures import ThreadPoolExecutor
    handle = observability.start("r", "c", {}, True)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: observability.finish(handle, "success"), range(100)))
    observability.first_token(handle)
    assert len(_find(registry.samples, "dec", "inprogress_requests")) == 1
    assert not _find(registry.samples, "observe", "time_to_first_token_seconds")
    from ai_gateway.observability import Observability
    with pytest.raises(ValueError):
        Observability().finish(handle, "success")


def test_role_nonstream_and_duplicate_token_ignored(observability, registry):
    handle = observability.start("r", "c", {}, True)
    observability.first_token(handle, "role")
    observability.first_token(handle, "tool_calls")
    observability.first_token(handle, "content")
    plain = observability.start("r", "c", {}, False)
    observability.first_token(plain)
    assert len(_find(registry.samples, "observe", "time_to_first_token_seconds")) == 1
