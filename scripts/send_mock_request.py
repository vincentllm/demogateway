#!/usr/bin/env python3
"""Send a mock OpenAI chat completion request using the mock backend sender.

Usage:
    python scripts/send_mock_request.py [--stream]

This script demonstrates how to use ``MockLLMBackendSender`` as a standalone
HTTP transport replacement. It does not require a running gateway or backend.
"""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx

from ai_gateway.testing import MockLLMBackendSender, SenderBehavior


async def main() -> None:
    parser = argparse.ArgumentParser(description="Send a mock chat completion request")
    parser.add_argument("--stream", action="store_true", help="Use streaming response")
    parser.add_argument(
        "--failure-rate",
        type=float,
        default=0.0,
        help="Simulated failure probability (0-1)",
    )
    parser.add_argument(
        "--latency-ms",
        type=float,
        default=50.0,
        help="Simulated response latency in milliseconds",
    )
    args = parser.parse_args()

    behavior = SenderBehavior(
        latency_ms=args.latency_ms,
        failure_rate=args.failure_rate,
    )
    sender = MockLLMBackendSender(behavior)
    transport = httpx.MockTransport(sender.as_transport_handler())

    async with httpx.AsyncClient(transport=transport) as client:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Hello, how is the weather?"}],
            "stream": args.stream,
        }
        try:
            response = await client.post("https://mock.backend/v1/chat/completions", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            print(f"HTTP error: {exc.response.status_code}")
            print(exc.response.text)
            return
        except Exception as exc:
            print(f"Request failed: {type(exc).__name__}: {exc}")
            return

        if args.stream:
            print("Streaming response:")
            for line in response.text.splitlines():
                line = line.strip()
                if line.startswith("data: "):
                    data = line[len("data: "):]
                    if data == "[DONE]":
                        break
                    print(json.dumps(json.loads(data), indent=2, ensure_ascii=False))
        else:
            print("Non-streaming response:")
            print(json.dumps(response.json(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
