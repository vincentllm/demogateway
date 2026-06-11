#!/usr/bin/env python3
"""End-to-end demo of AI Gateway + Mock LLM Backend.

This script:
1. Starts the mock LLM backend server on port 9999.
2. Starts the AI Gateway on port 8000 pointing at the mock backend.
3. Sends non-streaming and streaming OpenAI-format requests to the gateway.
4. Prints the responses.
5. Prints what the mock backend actually received, proving the gateway
   forwarded/transformed the request.

Usage:
    python scripts/e2e_demo.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def _wait_for(url: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def _print_section(title: str) -> None:
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def _send_non_streaming_request() -> None:
    _print_section("1. Non-streaming request through Gateway")
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Hello, how is the weather?"}],
        "stream": False,
    }
    response = httpx.post(
        "http://127.0.0.1:8000/v1/chat/completions",
        headers={"Authorization": "Bearer demo-key"},
        json=payload,
        timeout=30,
    )
    print(f"Gateway response status: {response.status_code}")
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))


def _send_streaming_request() -> None:
    _print_section("2. Streaming request through Gateway")
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Hello!"}],
        "stream": True,
    }
    with httpx.stream(
        "POST",
        "http://127.0.0.1:8000/v1/chat/completions",
        headers={"Authorization": "Bearer demo-key"},
        json=payload,
        timeout=30,
    ) as response:
        print(f"Gateway response status: {response.status_code}")
        print(f"Content-Type: {response.headers.get('content-type')}")
        for line in response.iter_lines():
            if line.startswith("data: "):
                data = line[6:]
                if data == "[DONE]":
                    break
                print(data)


def _send_rate_limit_burst() -> None:
    _print_section("3. Rate limit test (request limit = 5 in config.mock.yaml)")
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Rate limit test"}],
    }
    statuses = []
    for i in range(7):
        response = httpx.post(
            "http://127.0.0.1:8000/v1/chat/completions",
            headers={"Authorization": "Bearer rate-limit-demo"},
            json=payload,
            timeout=30,
        )
        statuses.append((i + 1, response.status_code))
    for idx, status in statuses:
        print(f"Request {idx}: HTTP {status}")


def main() -> int:
    env = {**dict(subprocess.os.environ), "GATEWAY_CONFIG": str(ROOT / "config.mock.yaml")}

    _print_section("Starting Mock LLM Backend (port 9999)")
    mock_proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "mock_backend_server.py"), "--port", "9999"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if not _wait_for("http://127.0.0.1:9999/health"):
        print("Mock backend failed to start", file=sys.stderr)
        mock_proc.terminate()
        return 1
    print("Mock backend is ready")

    _print_section("Starting AI Gateway (port 8000)")
    gateway_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ai_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if not _wait_for("http://127.0.0.1:8000/health"):
        print("Gateway failed to start", file=sys.stderr)
        gateway_proc.terminate()
        mock_proc.terminate()
        return 1
    print("Gateway is ready")

    try:
        _send_non_streaming_request()
        _send_streaming_request()
        _send_rate_limit_burst()

        _print_section("4. What the mock backend received")
        # The mock backend exposes its request log via an internal endpoint
        # that we add for observability in this demo.
        log_response = httpx.get("http://127.0.0.1:9999/admin/request-log", timeout=5)
        if log_response.status_code == 200:
            entries = log_response.json()["requests"]
            print(f"Total requests received by mock backend: {len(entries)}")
            for idx, entry in enumerate(entries, 1):
                print(f"\n--- Request {idx} ---")
                print(f"Method: {entry['method']}")
                print(f"URL: {entry['url']}")
                print(f"Model: {entry.get('body', {}).get('model')}")
                print(f"Stream: {entry.get('body', {}).get('stream')}")
                print(f"Messages: {entry.get('body', {}).get('messages')}")
        else:
            print(f"Could not fetch request log: {log_response.status_code}")

    finally:
        _print_section("Shutting down")
        gateway_proc.terminate()
        mock_proc.terminate()
        try:
            gateway_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gateway_proc.kill()
        try:
            mock_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock_proc.kill()

    return 0


if __name__ == "__main__":
    sys.exit(main())
