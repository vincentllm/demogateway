#!/usr/bin/env python3
"""Load test script for the AI Gateway.

Runs concurrent OpenAI-style requests against a gateway endpoint and reports:
- throughput (requests/second)
- outcome counts (json success, sse success, 429 rejected, stream error, other error)
- latency percentiles p50/p95/p99
- TTFT percentiles for streaming responses

Example:
    python scripts/load_test.py --url http://127.0.0.1:8000/v1/chat/completions \
        --concurrency 10 --total 100 --mode mixed
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class Result:
    outcome: str
    latency: float
    ttft: float | None = None


@dataclass
class Stats:
    total: int = 0
    json_success: int = 0
    sse_success: int = 0
    rejected_429: int = 0
    stream_error: int = 0
    other_error: int = 0
    latencies: list[float] = field(default_factory=list)
    ttfts: list[float] = field(default_factory=list)
    start: float = 0.0
    end: float = 0.0

    @property
    def throughput(self) -> float:
        elapsed = self.end - self.start
        return self.total / elapsed if elapsed > 0 else 0.0

    @property
    def errors(self) -> int:
        return self.rejected_429 + self.stream_error + self.other_error

    @property
    def success(self) -> int:
        return self.json_success + self.sse_success

    def add(self, result: Result) -> None:
        self.total += 1
        self.latencies.append(result.latency)
        if result.outcome == "sse_success" and result.ttft is not None:
            self.ttfts.append(result.ttft)
        if result.outcome == "json_success":
            self.json_success += 1
        elif result.outcome == "sse_success":
            self.sse_success += 1
        elif result.outcome == "rejected_429":
            self.rejected_429 += 1
        elif result.outcome == "stream_error":
            self.stream_error += 1
        else:
            self.other_error += 1


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def _valid_stream_chunk(chunk: Any) -> bool:
    """Check the gateway's canonical single-choice chunk and usage shapes.

    Envelope metadata is optional here; choices/delta and cumulative usage
    follow the adapter contract. Empty choices are reserved for usage frames.
    """
    if not isinstance(chunk, dict) or "error" in chunk:
        return False
    choices = chunk.get("choices")
    if not isinstance(choices, list) or len(choices) > 1:
        return False
    usage = chunk.get("usage")
    if not choices and usage is None:
        return False
    if usage is not None:
        if not isinstance(usage, dict):
            return False
        counts = [usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")]
        if any(type(value) is not int or value < 0 for value in counts) or counts[0] + counts[1] != counts[2]:
            return False
    for choice in choices:
        if not isinstance(choice, dict) or type(choice.get("index")) is not int or choice["index"] != 0:
            return False
        reason = choice.get("finish_reason")
        if reason is not None and (not isinstance(reason, str) or reason not in {"stop", "length", "tool_calls", "content_filter"}):
            return False
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return False
        if "role" in delta and delta["role"] != "assistant":
            return False
        if delta.get("content") is not None and not isinstance(delta["content"], str):
            return False
        if "tool_calls" in delta:
            if not isinstance(delta["tool_calls"], list):
                return False
            for tool in delta["tool_calls"]:
                if not isinstance(tool, dict) or type(tool.get("index")) is not int or tool["index"] < 0:
                    return False
                if "id" in tool and not isinstance(tool["id"], str):
                    return False
                if "type" in tool and tool["type"] != "function":
                    return False
                function = tool.get("function", {})
                if not isinstance(function, dict) or any(key in function and not isinstance(function[key], str) for key in ("name", "arguments")):
                    return False
    return True


def _meaningful_delta(chunk: dict[str, Any]) -> bool:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if not isinstance(delta, dict):
            continue
        if isinstance(delta.get("content"), str) and delta["content"]:
            return True
        tools = delta.get("tool_calls")
        if isinstance(tools, list):
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                function = tool.get("function")
                if isinstance(function, dict) and any(isinstance(function.get(key), str) and function[key] for key in ("name", "arguments")):
                    return True
    return False


def _validate_args(args: argparse.Namespace) -> None:
    for name in ("concurrency", "total", "max_tokens"):
        if type(getattr(args, name)) is not int or getattr(args, name) <= 0:
            raise ValueError(f"{name.replace('_', '-')} must be a positive integer")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("timeout must be finite and positive")


async def _one_request(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    model: str,
    max_tokens: int,
    mode: str,
    timeout: float,
) -> Result:
    stream = mode == "sse"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Hello, this is a load test prompt."}],
        "stream": stream,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    start = time.perf_counter()
    ttft: float | None = None
    stream_started = False
    try:
        if stream:
            async with client.stream(
                "POST", url, json=payload, headers=headers, timeout=timeout
            ) as response:
                if response.status_code == 429:
                    return Result("rejected_429", time.perf_counter() - start)
                if response.status_code != 200:
                    return Result("other_error", time.perf_counter() - start)
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                    return Result("stream_error", time.perf_counter() - start)
                stream_started = True
                finished = False
                data_lines: list[str] = []
                event = ""
                async for line in response.aiter_lines():
                    if line:
                        if line.startswith(":"):
                            continue
                        key, _, value = line.partition(":")
                        value = value[1:] if value.startswith(" ") else value
                        if key == "data":
                            data_lines.append(value)
                        elif key == "event":
                            event = value
                        continue
                    if event == "error":
                        return Result("stream_error", time.perf_counter() - start, ttft)
                    data = "\n".join(data_lines)
                    data_lines, event = [], ""
                    if not data:
                        continue
                    if data.strip() == "[DONE]":
                        return Result("sse_success" if finished else "stream_error", time.perf_counter() - start, ttft)
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        return Result("stream_error", time.perf_counter() - start, ttft)
                    if not _valid_stream_chunk(parsed) or (finished and parsed["choices"]):
                        return Result("stream_error", time.perf_counter() - start, ttft)
                    if any(choice.get("finish_reason") is not None for choice in parsed["choices"]):
                        finished = True
                    if ttft is None and _meaningful_delta(parsed):
                        ttft = time.perf_counter() - start
                return Result("stream_error", time.perf_counter() - start, ttft)
        else:
            response = await client.post(url, json=payload, headers=headers, timeout=timeout)
            latency = time.perf_counter() - start
            if response.status_code == 429:
                return Result("rejected_429", latency)
            if response.status_code != 200:
                return Result("other_error", latency)
            try:
                body = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                return Result("other_error", latency)
            if not isinstance(body, dict) or "error" in body or not isinstance(body.get("choices"), list) or not body["choices"]:
                return Result("other_error", latency)
            if any(not isinstance(choice, dict) or not isinstance(choice.get("message"), dict) for choice in body["choices"]):
                return Result("other_error", latency)
            return Result("json_success", latency)
    except httpx.HTTPError:
        return Result("stream_error" if stream_started else "other_error", time.perf_counter() - start, ttft)


async def _worker(
    queue: asyncio.Queue[int],
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    model: str,
    max_tokens: int,
    mode: str,
    timeout: float,
    results: list[Result],
    lock: asyncio.Lock,
) -> None:
    while True:
        try:
            index = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        request_mode = ("sse" if index % 2 else "json") if mode == "mixed" else mode
        result = await _one_request(client, url, api_key, model, max_tokens, request_mode, timeout)
        async with lock:
            results.append(result)


async def run(args: argparse.Namespace) -> Stats:
    _validate_args(args)
    stats = Stats()
    stats.start = time.perf_counter()
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(args.total):
        queue.put_nowait(i)

    results: list[Result] = []
    lock = asyncio.Lock()
    limits = httpx.Limits(max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(limits=limits) as client:
        workers = [
            _worker(
                queue,
                client,
                args.url,
                args.api_key,
                args.model,
                args.max_tokens,
                args.mode,
                args.timeout,
                results,
                lock,
            )
            for _ in range(args.concurrency)
        ]
        await asyncio.gather(*workers)

    stats.end = time.perf_counter()
    for result in results:
        stats.add(result)
    return stats


def _print_text(stats: Stats) -> None:
    print("Load test results")
    print("=" * 40)
    print(f"Total requests:     {stats.total}")
    print(f"Throughput:         {stats.throughput:.2f} req/s")
    print(f"JSON success:       {stats.json_success}")
    print(f"SSE success:        {stats.sse_success}")
    print(f"429 rejected:       {stats.rejected_429}")
    print(f"Stream errors:      {stats.stream_error}")
    print(f"Other errors:       {stats.other_error}")
    for label, values in (("Latency", stats.latencies), ("TTFT   ", stats.ttfts)):
        formatted = ["N/A" if (value := _percentile(values, p)) is None else f"{value:.3f}s" for p in (0.5, 0.95, 0.99)]
        print(f"{label} p50/p95/p99: " + " / ".join(formatted))


def _print_json(stats: Stats) -> None:
    report = {
        "total": stats.total,
        "throughput": stats.throughput,
        "outcomes": {
            "json_success": stats.json_success,
            "sse_success": stats.sse_success,
            "rejected_429": stats.rejected_429,
            "stream_error": stats.stream_error,
            "other_error": stats.other_error,
        },
        "latency": {
            "p50": _percentile(stats.latencies, 0.5),
            "p95": _percentile(stats.latencies, 0.95),
            "p99": _percentile(stats.latencies, 0.99),
        },
        "ttft": {
            "p50": _percentile(stats.ttfts, 0.5),
            "p95": _percentile(stats.ttfts, 0.95),
            "p99": _percentile(stats.ttfts, 0.99),
        },
    }
    print(json.dumps(report, indent=2))


def _print_csv(stats: Stats) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(["metric", "value"])
    writer.writerow(["total", stats.total])
    writer.writerow(["throughput", f"{stats.throughput:.4f}"])
    writer.writerow(["json_success", stats.json_success])
    writer.writerow(["sse_success", stats.sse_success])
    writer.writerow(["rejected_429", stats.rejected_429])
    writer.writerow(["stream_error", stats.stream_error])
    writer.writerow(["other_error", stats.other_error])
    writer.writerow(["latency_p50", _percentile(stats.latencies, 0.5)])
    writer.writerow(["latency_p95", _percentile(stats.latencies, 0.95)])
    writer.writerow(["latency_p99", _percentile(stats.latencies, 0.99)])
    writer.writerow(["ttft_p50", _percentile(stats.ttfts, 0.5)])
    writer.writerow(["ttft_p95", _percentile(stats.ttfts, 0.95)])
    writer.writerow(["ttft_p99", _percentile(stats.ttfts, 0.99)])


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test the AI Gateway")
    parser.add_argument("--url", default="http://127.0.0.1:8000/v1/chat/completions")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--total", type=int, default=100)
    parser.add_argument("--mode", choices=["json", "sse", "mixed"], default="mixed")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--max-tokens", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--api-key", default="demo-key")
    parser.add_argument("--output", choices=["text", "json", "csv"], default="text")
    args = parser.parse_args()

    try:
        _validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    stats = asyncio.run(run(args))

    if args.output == "text":
        _print_text(stats)
    elif args.output == "json":
        _print_json(stats)
    else:
        _print_csv(stats)

    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
