"""Deterministic protocol and report regressions for the standalone CLI."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("gateway_load_test", Path(__file__).parents[1] / "scripts/load_test.py")
load = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = load
spec.loader.exec_module(load)


@pytest.mark.parametrize("empty", [False, True])
def test_reports_without_ttft(empty, capsys):
    stats = load.Stats()
    if not empty:
        stats.add(load.Result("json_success", 1.0))
    load._print_text(stats)
    assert "N/A" in capsys.readouterr().out
    load._print_json(stats)
    assert json.loads(capsys.readouterr().out)["ttft"] == {"p50": None, "p95": None, "p99": None}
    load._print_csv(stats)
    assert "ttft_p50," in capsys.readouterr().out


class TimedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, clock, broken=False):
        self.chunks, self.clock, self.broken = chunks, clock, broken

    async def __aiter__(self):
        for timestamp, chunk in self.chunks:
            self.clock[0] = timestamp
            yield chunk.encode()
        if self.broken:
            raise httpx.ReadError("disconnected")


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [{"content": "hello"}, {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}])
async def test_ttft_first_meaningful_delta(monkeypatch, delta):
    clock = [0.0]
    monkeypatch.setattr(load.time, "perf_counter", lambda: clock[0])
    chunks = [(1, ": heartbeat\n\n"), (2, 'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'),
              (3, 'data: {"choices":[{"index":0,"delta":{"content":"","tool_calls":[]}}]}\n\n'),
              (4, 'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":0,"total_tokens":1}}\n\n'),
              (10, 'data: ' + json.dumps({"choices": [{"index": 0, "delta": delta}]}) + '\n\n'), (11, 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'), (12, 'data: [DONE]\n\n')]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=TimedStream(chunks, clock)))) as client:
        result = await load._one_request(client, "http://test", "key", "model", 1, "sse", 10)
    assert (result.outcome, result.ttft, result.latency) == ("sse_success", 10, 12)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,mode,expected", [
    (429, '{"error":"limited"}', "sse", "rejected_429"),
    (500, 'data: {"error":"fail"}\n\n', "sse", "other_error"),
    (200, 'data: {"error":{"message":"fail"}}\n\n', "sse", "stream_error"),
    (200, 'event: error\ndata: {}\n\n', "sse", "stream_error"),
    (200, 'data: invalid\n\ndata: [DONE]\n\n', "sse", "stream_error"),
    (200, 'data: {"choices":[]}\n\n', "sse", "stream_error"),
    (200, 'data: [DONE]\n', "sse", "stream_error"),
    (200, '[]', "json", "other_error"),
    (200, '{"choices":[{}]}', "json", "other_error"),
    (200, '{"choices":["invalid"]}', "json", "other_error"),
    (200, 'invalid', "json", "other_error"),
    (200, '{"error":"bad","choices":[{}]}', "json", "other_error"),
    (429, '{}', "json", "rejected_429"),
    (200, '{"choices":[{"message":{"content":"hi"}}]}', "json", "json_success"),
])
async def test_outcome_classification(status, body, mode, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, headers={"content-type": "text/event-stream"}, content=body))) as client:
        result = await load._one_request(client, "http://test", "key", "model", 1, mode, 10)
    assert result.outcome == expected


@pytest.mark.asyncio
async def test_stream_disconnect_is_stream_error():
    stream = TimedStream([], [0], broken=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream))) as client:
        result = await load._one_request(client, "http://test", "key", "model", 1, "sse", 10)
    assert result.outcome == "stream_error"


@pytest.mark.asyncio
async def test_mixed_schedule_uses_index(monkeypatch):
    import asyncio
    modes = []
    async def request(*args):
        modes.append(args[-2])
        return load.Result("json_success", 0)
    monkeypatch.setattr(load, "_one_request", request)
    queue = asyncio.Queue()
    for index in range(5):
        queue.put_nowait(index)
    await load._worker(queue, None, "url", "key", "model", 1, "mixed", 1, [], asyncio.Lock())
    assert modes == ["json", "sse", "json", "sse", "json"]


@pytest.mark.parametrize("field,value", [("concurrency", 0), ("total", -1), ("max_tokens", 0), ("timeout", 0), ("timeout", float("nan")), ("timeout", float("inf"))])
def test_invalid_options(field, value):
    options = dict(concurrency=1, total=1, max_tokens=1, timeout=1.0)
    options[field] = value
    with pytest.raises(ValueError):
        load._validate_args(argparse.Namespace(**options))


@pytest.mark.parametrize("argument", ["--concurrency=0", "--total=-1", "--max-tokens=0", "--timeout=nan"])
def test_cli_invalid_options_exit_before_network(argument):
    import subprocess
    completed = subprocess.run([sys.executable, str(Path(load.__file__)), argument], capture_output=True, text=True)
    assert completed.returncode == 2
    assert "must be" in completed.stderr


def test_failed_streams_excluded_from_ttft_percentiles():
    stats = load.Stats()
    stats.add(load.Result("stream_error", 10, 5))
    stats.add(load.Result("sse_success", 4, 2))
    assert stats.ttfts == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk", [
    {"choices": "invalid"},
    {"choices": [{"index": 0, "delta": 42}]},
    {"arbitrary": "not a chunk"},
    {"choices": [42]},
    {"choices": [{"index": True, "delta": {}}]},
    {"choices": [{"index": 0, "delta": {"content": 42}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": "invalid"}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": 42}]}}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": []}]},
    {"choices": [], "usage": 42},
    {"choices": [], "usage": {"prompt_tokens": True, "completion_tokens": 0, "total_tokens": 1}},
    {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 99}},
    {"choices": []},
])
async def test_malformed_chunk_before_done_never_succeeds(chunk):
    # A genuine terminal follows corruption so the assertion detects structural
    # validation even when a parser otherwise demands a finish_reason.
    body = 'data: ' + json.dumps(chunk) + '\n\n'
    body += 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    body += 'data: [DONE]\n\n'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body))) as client:
        result = await load._one_request(client, "http://test", "key", "model", 1, "sse", 10)
    assert result.outcome == "stream_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", [
    'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":0,"total_tokens":1}}\n\n',
    '',
])
async def test_valid_role_and_terminal_with_optional_usage_succeeds(tail):
    body = 'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}],"usage":null}\n\n'
    body += 'data: {"choices":[{"index":0,"delta":{"content":null},"finish_reason":"stop"}]}\n\n'
    body += tail + 'data: [DONE]\n\n'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body))) as client:
        result = await load._one_request(client, "http://test", "key", "model", 1, "sse", 10)
    assert result.outcome == "sse_success"
    assert result.ttft is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [
    '',
    'data: {"choices":[{"index":0,"delta":{"content":"unfinished"}}]}\n\n',
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: {"choices":[{"index":0,"delta":{"content":"late"}}]}\n\n',
])
async def test_done_without_valid_terminal_or_late_content_fails(prefix):
    body = prefix + 'data: [DONE]\n\n'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body))) as client:
        result = await load._one_request(client, "http://test", "key", "model", 1, "sse", 10)
    assert result.outcome == "stream_error"
