"""Requirement-driven integration checks without network provider credentials."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_gateway.auth import hash_client_key
from ai_gateway.config import GatewayConfig
from ai_gateway.main import create_app
from ai_gateway.runtime import GatewayStream
from ai_gateway.transport import StreamingTransport

KEY = "demo-client-key-with-randomness"
BODY = {"model": "public", "messages": [{"role": "user", "content": "private prompt"}]}


def config(**overrides):
    platform = {"auth": {"clients": [{"client_id": "alice", "keys": [{"key_id": "primary", "sha256": hash_client_key(KEY)}], "allowed_models": ["public"]}], "upstream_pools": [{"key_ref": "openai", "keys": [{"key_id": "server", "secret": "server-secret"}]}]},
                "routes": {"public": {"provider": "openai", "base_url": "https://provider.invalid", "upstream_model": "private-model", "key_ref": "openai", "input_token_bound": 100}},
                "quotas": {"alice": {"rpm": 100, "tpm": 10000, "total_tokens": 10000}}}
    platform.update(overrides)
    return GatewayConfig(platform=platform)


def completion(usage=True):
    data = {"id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": "private-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}]}
    if usage:
        data["usage"] = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    return data


def app_for(handler, **overrides):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return create_app(config(**overrides), upstream), upstream


def post(client, **body):
    return client.post("/v1/chat/completions", json={**BODY, **body}, headers={"Authorization": "Bearer " + KEY})


@pytest.mark.parametrize("headers,status", [({}, 401), ({"Authorization": "Bearer invalid"}, 401), ([("Authorization", "Bearer " + KEY), ("Authorization", "Bearer " + KEY)], 401)])
def test_denied_requests_never_dispatch(headers, status):
    calls = []
    app, _ = app_for(lambda r: calls.append(r) or httpx.Response(200, json=completion()))
    with TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=BODY, headers=headers).status_code == status
    assert calls == []


def test_json_alias_key_separation_settlement_and_metrics():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer server-secret"
        assert KEY not in str(request.headers)
        assert json.loads(request.content)["model"] == "private-model"
        return httpx.Response(200, json=completion())
    app, _ = app_for(handler, quotas={"alice": {"rpm": 10, "tpm": 233, "total_tokens": 233}})
    with TestClient(app) as client:
        for _ in range(2):
            response = post(client)
            assert response.status_code == 200
            assert response.json()["model"] == "public"
        metrics = client.get("/metrics")
        assert 'inprogress_requests{outcome="in_progress",provider="openai",route="public"} 0.0' in metrics.text
        assert client.get("/health").json()["mode"] == "platform"
    assert len(calls) == 2  # Full 228 reservation refunded to actual 5 permits call 2.


@pytest.mark.parametrize("extra", [{"stream": "true"}, {"max_tokens": True}, {"max_tokens": 1025}, {"temperature": "high"}, {"temperature": float("inf")}, {"top_p": -1}, {"unknown": "secret"}, {"messages": []}])
def test_invalid_input_never_dispatches(extra):
    calls = []
    app, _ = app_for(lambda r: calls.append(r) or httpx.Response(200, json=completion()))
    with TestClient(app) as client:
        # json.dumps permits Infinity; route must reject it without echo.
        response = client.post("/v1/chat/completions", content=json.dumps({**BODY, **extra}), headers={"Authorization": "Bearer " + KEY})
        assert response.status_code == 400
        assert "private prompt" not in response.text
    assert calls == []


def test_forbidden_model_and_quota_zero_no_dispatch():
    calls = []
    app, _ = app_for(lambda r: calls.append(r) or httpx.Response(200, json=completion()), quotas={"alice": {"rpm": 0, "tpm": 10000, "total_tokens": 10000}})
    with TestClient(app) as client:
        assert post(client, model="forbidden").status_code == 403
        assert post(client).status_code == 429
    assert calls == []


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_upstream_failure_retains_reservation_and_redacts(status):
    calls = []
    app, _ = app_for(lambda r: calls.append(r) or httpx.Response(status, text="secret upstream body"), quotas={"alice": {"rpm": 10, "tpm": 228, "total_tokens": 228}})
    with TestClient(app) as client:
        response = post(client)
        assert response.status_code == status
        assert "secret upstream" not in response.text
        assert post(client).status_code == 429
    assert len(calls) == 1


def test_stream_alias_usage_and_single_done():
    payload = {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "private-model", "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]}
    final = {**payload, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    usage = {**payload, "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    wire = b"".join(("data: " + json.dumps(p) + "\n\n").encode() for p in [payload, final, usage]) + b"data: [DONE]\n\n"
    app, _ = app_for(lambda r: httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"}), quotas={"alice": {"rpm": 10, "tpm": 233, "total_tokens": 233}})
    with TestClient(app) as client:
        for _ in range(2):
            response = post(client, stream=True, stream_options={"include_usage": True})
            assert response.status_code == 200
            assert response.text.count("data: [DONE]") == 1
            assert '"model":"public"' in response.text
            assert '"total_tokens":5' in response.text
        assert 'inprogress_requests{outcome="in_progress",provider="openai",route="public"} 0.0' in client.get("/metrics").text


@pytest.mark.asyncio
async def test_disconnect_during_prefetch_cancels_closes_and_terminalizes_once():
    opened, closed = asyncio.Event(), asyncio.Event()
    terminals = []
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            opened.set()
            await asyncio.Event().wait()
            yield b"unreachable"
        async def aclose(self):
            closed.set()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Slow())))
    async def receive():
        await opened.wait()
        return {"type": "http.disconnect"}
    async def terminal(usage, outcome):
        terminals.append((usage, outcome))
    sent = []
    async def send(message):
        sent.append(message)
    response = GatewayStream(StreamingTransport(upstream), {"method": "POST", "url": "https://provider.invalid", "headers": {}, "json": {}}, lambda *args: [], terminal)
    await asyncio.wait_for(response({}, receive, send), 2)
    assert closed.is_set()
    assert terminals == [(None, "cancelled")]
    assert sent == []
    await upstream.aclose()


def test_config_secrets_excluded_and_invalid_reservation_refused():
    cfg = config()
    assert "server-secret" not in repr(cfg)
    assert "server-secret" not in cfg.model_dump_json()
    with pytest.raises(ValueError):
        config(default_max_tokens=2048)


@pytest.mark.parametrize("raw", [b"not-json", b'{"secret":"private prompt"', b"[1]", b"[" * 2000 + b"]" * 2000])
def test_malformed_body_returns_safe_400_and_records_lifecycle(raw):
    calls = []
    app, _ = app_for(lambda r: calls.append(r) or httpx.Response(200, json=completion()))
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", content=raw)
        assert response.status_code == 400
        assert "private prompt" not in response.text
        assert 'requests_total{outcome="in_progress",provider="unknown",route="unknown"} 1.0' in client.get("/metrics").text
    assert not calls


def test_request_and_response_size_bounds_are_enforced():
    calls = []
    app, _ = app_for(lambda r: calls.append(r) or httpx.Response(200, json=completion()), max_request_bytes=10)
    with TestClient(app) as client:
        assert post(client).status_code == 413
    assert not calls
    app, _ = app_for(lambda r: httpx.Response(200, content=b"x" * 101), max_response_bytes=100)
    with TestClient(app) as client:
        assert post(client).status_code == 502


@pytest.mark.parametrize("data", [{"error": {"message": "provider-secret"}}, completion() | {"usage": {"prompt_tokens": 2**54, "completion_tokens": 0, "total_tokens": 2**54}}])
def test_embedded_errors_and_unsafe_usage_are_502(data):
    app, _ = app_for(lambda r: httpx.Response(200, json=data))
    with TestClient(app) as client:
        response = post(client)
        assert response.status_code == 502
        assert "provider-secret" not in response.text


@pytest.mark.asyncio
async def test_stream_settlement_failure_never_sends_done():
    from ai_gateway.quota import QuotaUnavailable
    payload = {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "private-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    wire = ("data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n").encode()
    app, upstream = app_for(lambda r: httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"}))
    async def unavailable(*args):
        raise QuotaUnavailable()
    app.state.quota.settle = unavailable
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway") as client:
        response = await client.post("/v1/chat/completions", json={**BODY, "stream": True}, headers={"Authorization": "Bearer " + KEY})
        assert response.status_code == 200
        assert "event: error" in response.text
        assert "quota_unavailable" in response.text
        assert "[DONE]" not in response.text
    await upstream.aclose()


@pytest.mark.asyncio
async def test_send_disconnect_closes_stream_and_terminalizes_cancelled():
    class Source(httpx.AsyncByteStream):
        def __init__(self):
            self.closed = False
        async def __aiter__(self):
            yield b'data: x\n\n'
            await asyncio.Event().wait()
        async def aclose(self):
            self.closed = True
    source = Source()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)))
    terminal_events = []
    async def terminal(usage, outcome):
        terminal_events.append(outcome)
    async def receive():
        await asyncio.Event().wait()
    async def send(message):
        raise OSError("closed")
    response = GatewayStream(StreamingTransport(upstream), {"method": "POST", "url": "https://provider.invalid", "headers": {}, "json": {}}, lambda *args: [{"choices": []}], terminal)
    await asyncio.wait_for(response({}, receive, send), 2)
    assert terminal_events == ["cancelled"]
    assert source.closed
    await upstream.aclose()


@pytest.mark.parametrize("field", ["temperature", "top_p"])
@pytest.mark.parametrize("value", [10**400, -(10**400)])
def test_huge_integer_sampling_inputs_return_structured_400(field, value):
    calls = []
    app, _ = app_for(lambda r: calls.append(r) or httpx.Response(200, json=completion()))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = post(client, **{field: value})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_" + field
    assert not calls


@pytest.mark.parametrize("role", [[], {}, ["user"], {"value": "user"}])
def test_unhashable_message_roles_return_structured_400_without_admission(role):
    calls = []
    app, _ = app_for(lambda r: calls.append(r) or httpx.Response(200, json=completion()))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = post(client, messages=[{"role": role, "content": "private prompt"}])
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert app.state.quota._store._leases == {}
        assert post(client).status_code == 200  # A valid role still dispatches.
    assert len(calls) == 1


@pytest.mark.parametrize("extra", [{"temperature": 0, "top_p": 0}, {"temperature": 2, "top_p": 1}, {"temperature": 0.5, "top_p": 0.5}])
def test_valid_sampling_boundary_negative_controls(extra):
    app, _ = app_for(lambda r: httpx.Response(200, json=completion()))
    with TestClient(app) as client:
        assert post(client, **extra).status_code == 200


def test_formatted_validation_errors_hide_all_secret_inputs():
    from ai_gateway.config import PlatformConfig, PlatformRoute
    from pydantic import ValidationError
    sentinel = "server-secret-that-must-not-appear"
    for cls, value in [
        (GatewayConfig, {"platform": {"auth": sentinel, "routes": {}, "quotas": {}}}),
        (PlatformConfig, {"auth": sentinel, "routes": {}, "quotas": {}}),
        (PlatformRoute, {"provider": "openai", "base_url": "https://provider.invalid", "upstream_model": "m", "key_ref": "k", "input_token_bound": sentinel}),
    ]:
        with pytest.raises(ValidationError) as caught:
            cls.model_validate(value)
        assert sentinel not in str(caught.value)
        assert "input_value=" not in str(caught.value)
    values = config().platform.model_dump()
    values["auth"] = config().platform.auth
    values.update(redis_url="redis://:" + sentinel + "@localhost", default_max_tokens=2048)
    with pytest.raises(ValidationError) as caught:
        GatewayConfig(platform=values)
    assert sentinel not in str(caught.value)
    assert "input_value=" not in str(caught.value)


@pytest.mark.parametrize("base_url", ["https://provider.invalid:bad", "https://provider.invalid:-1", "https://provider.invalid:0", "https://provider.invalid:65536", "https://:443"])
def test_invalid_provider_url_rejected_at_startup_without_input_echo(base_url):
    from pydantic import ValidationError
    route = config().platform.routes["public"].model_dump()
    route["base_url"] = base_url
    with pytest.raises(ValidationError) as caught:
        config(routes={"public": route})
    assert "Invalid provider URL" in str(caught.value)
    assert base_url not in str(caught.value)
    assert "server-secret" not in str(caught.value)


@pytest.mark.parametrize("base_url", ["https://provider.invalid", "https://provider.invalid:443", "http://provider.invalid:80", "http://127.0.0.1:1", "http://[::1]:65535"])
def test_valid_provider_url_ports_remain_supported(base_url):
    route = config().platform.routes["public"].model_dump()
    route["base_url"] = base_url
    assert config(routes={"public": route}).platform.routes["public"].base_url == base_url
