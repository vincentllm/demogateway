import asyncio
import json

import httpx
import pytest

from ai_gateway.transport import StreamingTransport, TransportError, parse_sse


REQUEST = {"method": "POST", "url": "https://upstream.invalid/v1/chat/completions",
           "headers": {"Authorization": "Bearer secret"}, "json": {"stream": True}}
CHUNK = {"choices": [{"delta": {"content": "你好"}}]}
WIRE = ("data: " + json.dumps(CHUNK) + "\n\ndata: [DONE]\n\n").encode()
USAGE = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}


def adapter(event, data, state):
    return [data] if data == "[DONE]" else [json.loads(data)]


class Source(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


def client_for(source, status=200, calls=None):
    async def handle(request):
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, headers={"content-type": "text/event-stream"}, stream=source)
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


@pytest.mark.parametrize("width", [1, 2, 7, 1000])
async def test_framing(width):
    wire = '\ufeff: comment\r\nevent: delta\rdata: 你\r\ndata: 好\n\n: ping\n\ndata:\n\ndata: ignored'.encode()
    source = Source([wire[i:i + width] for i in range(0, len(wire), width)])
    assert [x async for x in parse_sse(source.__aiter__())] == [("delta", "你\n好"), ("message", "")]


async def test_bounded_and_invalid_utf8():
    for wire in [b"data: " + b"x" * 30, b"data: \xff\n\n", b"data: \xe4"]:
        with pytest.raises((TransportError, UnicodeDecodeError)):
            _ = [x async for x in parse_sse(Source([wire]).__aiter__(), max_event_bytes=20)]


async def test_empty_event_type_is_message():
    source = Source([b"event:\ndata: ok\n\n"])
    assert [x async for x in parse_sse(source.__aiter__())] == [("message", "ok")]


async def test_success_usage_and_backpressure():
    usage_wire = ("data: " + json.dumps({"usage": USAGE}) + "\n\n").encode()
    source = Source([WIRE.split(b"data: [DONE]")[0], usage_wire, b"data: [DONE]\n\n"])
    terminals = []
    async with client_for(source) as client:
        async with StreamingTransport(client).stream(REQUEST, adapter, lambda *x: terminals.append(x)) as stream:
            assert source.reads == 1  # Prefetch one event, not the complete response.
            first = await anext(stream)
            assert "你好" in first.decode()
            assert source.reads == 1
            rest = b"".join([x async for x in stream])
        assert not client.is_closed
    assert rest.count(b"[DONE]") == 1
    assert terminals == [(USAGE, "success")]
    assert source.closed


@pytest.mark.parametrize("status", [401, 429, 500, 503])
async def test_http_errors_before_commit_are_safe(status):
    source = Source([b"secret body"])
    terminals = []
    async with client_for(source, status) as client:
        with pytest.raises(TransportError) as result:
            async with StreamingTransport(client).stream(REQUEST, adapter, lambda *x: terminals.append(x)):
                pytest.fail("must fail before yielding response")
    assert result.value.status_code == status
    assert "secret" not in str(result.value.body)
    assert terminals == [(None, "upstream_error")]
    assert source.closed and source.reads == 0


@pytest.mark.parametrize("error,outcome", [(httpx.ReadTimeout("secret"), "timeout"),
                                         (httpx.ReadError("secret"), "upstream_error"),
                                         (None, "upstream_error")])
async def test_midstream_failure_no_done_or_retry(error, outcome):
    source = Source([WIRE.split(b"data: [DONE]")[0]], error)
    terminals, calls = [], []
    async with client_for(source, calls=calls) as client:
        async with StreamingTransport(client, max_retries=3).stream(REQUEST, adapter, lambda *x: terminals.append(x)) as stream:
            wire = b"".join([x async for x in stream])
    assert b"event: error\n" in wire and b"[DONE]" not in wire and b"secret" not in wire
    assert len(calls) == 1 and terminals == [(None, outcome)] and source.closed


async def test_retry_explicit_and_bounded():
    calls, terminals, sources = [], [], []
    async def handle(request):
        calls.append(request)
        source = Source([WIRE])
        sources.append(source)
        return httpx.Response(503 if len(calls) < 3 else 200,
                              headers={"content-type": "text/event-stream"}, stream=source)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with StreamingTransport(client, max_retries=2).stream(REQUEST, adapter, lambda *x: terminals.append(x)) as stream:
            assert b"[DONE]" in b"".join([x async for x in stream])
    assert len(calls) == 3 and all(s.closed for s in sources)
    assert terminals == [(None, "success")]


async def test_context_exit_without_consumption():
    source, terminals = Source([WIRE]), []
    async with client_for(source) as client:
        async with StreamingTransport(client).stream(REQUEST, adapter, lambda *x: terminals.append(x)):
            pass
    assert source.closed and terminals == [(None, "cancelled")]


async def test_task_cancellation_during_prefetch():
    entered = asyncio.Event()
    class Blocking(Source):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""
    source, terminals = Blocking([]), []
    async with client_for(source) as client:
        async def consume():
            async with StreamingTransport(client).stream(REQUEST, adapter, lambda *x: terminals.append(x)) as stream:
                _ = [x async for x in stream]
        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert source.closed and terminals == [(None, "cancelled")]


@pytest.mark.parametrize("wire", [b"event: error\ndata: secret\n\n", b"data: {\n\n", b"data: {\"error\":\"secret\"}\n\n", b""])
async def test_bad_first_event(wire):
    source, terminals = Source([wire]), []
    async with client_for(source) as client:
        with pytest.raises(TransportError):
            async with StreamingTransport(client).stream(REQUEST, adapter, lambda *x: terminals.append(x)):
                pytest.fail("invalid stream committed")
    assert source.closed and terminals == [(None, "upstream_error")]


async def test_adapter_state_completion_and_async_hook():
    source, terminals = Source([b"data: stop\n\n"]), []
    def decode(event, data, state):
        state.update(done=True, usage=USAGE)
        return []
    async def terminal(*args):
        await asyncio.sleep(0)
        terminals.append(args)
    async with client_for(source) as client:
        async with StreamingTransport(client).stream(REQUEST, decode, terminal) as stream:
            assert [x async for x in stream] == [b"data: [DONE]\n\n"]
    assert terminals == [(USAGE, "success")]


async def test_cancel_during_body_and_slow_terminal():
    entered, hook_entered = asyncio.Event(), asyncio.Event()
    class Blocking(Source):
        async def __aiter__(self):
            yield WIRE.split(b"data: [DONE]")[0]
            entered.set()
            await asyncio.Event().wait()
    source, terminals = Blocking([]), []
    async def terminal(*args):
        hook_entered.set()
        await asyncio.sleep(0.01)
        terminals.append(args)
    async with client_for(source) as client:
        async def consume():
            async with StreamingTransport(client).stream(REQUEST, adapter, terminal) as stream:
                _ = [x async for x in stream]
        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel()
        await hook_entered.wait()
        task.cancel()  # Repeated cancellation during cleanup.
        with pytest.raises(asyncio.CancelledError):
            await task
    assert source.closed and terminals == [(None, "cancelled")]


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("secret"), httpx.ConnectError("secret")])
async def test_connect_failure_retry_limit(error):
    calls, terminals = [], []
    async def handle(request):
        calls.append(request)
        raise error
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(TransportError) as exc:
            async with StreamingTransport(client, max_retries=1).stream(REQUEST, adapter, lambda *x: terminals.append(x)):
                pytest.fail("must not commit")
    expected = "timeout" if isinstance(error, httpx.TimeoutException) else "upstream_error"
    assert exc.value.status_code == (504 if expected == "timeout" else 502)
    assert len(calls) == 2 and terminals == [(None, expected)]


async def test_non_sse_response_rejected_without_reading_body():
    source, terminals = Source([b"secret"]), []
    async def handle(request):
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=source)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(TransportError):
            async with StreamingTransport(client).stream(REQUEST, adapter, lambda *x: terminals.append(x)):
                pytest.fail("must not commit")
    assert source.closed and source.reads == 0 and terminals == [(None, "upstream_error")]


async def test_terminal_hook_failure_does_not_leak_response():
    source, calls = Source([WIRE]), []
    def terminal(*args):
        calls.append(args)
        raise RuntimeError("hook failed")
    async with client_for(source) as client:
        with pytest.raises(RuntimeError, match="hook failed"):
            async with StreamingTransport(client).stream(REQUEST, adapter, terminal) as stream:
                _ = [x async for x in stream]
    assert source.closed and calls == [(None, "success")]


async def test_provider_error_on_http_200_is_not_retried():
    source, calls, terminals = Source([b"event: error\ndata: secret\n\n"]), [], []
    async with client_for(source, calls=calls) as client:
        with pytest.raises(TransportError):
            async with StreamingTransport(client, max_retries=3).stream(REQUEST, adapter, lambda *x: terminals.append(x)):
                pytest.fail("provider error must fail before commit")
    assert len(calls) == 1 and terminals == [(None, "upstream_error")]
