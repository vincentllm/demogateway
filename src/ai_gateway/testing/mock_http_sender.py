"""Mock HTTP sender for backend LLM providers.

This module provides a programmable stand-in for the HTTP client that the
gateway uses to forward requests to backend LLM services. It is intended for
unit tests, integration tests, and local development where real backends are
not available or desirable.

The mock supports:

- OpenAI-compatible ``/v1/chat/completions`` responses (JSON and SSE streaming).
- Configurable latency, failure rate, and failure mode.
- Per-backend state simulation (healthy / degraded / unhealthy).
- Use as an ``httpx.MockTransport`` handler or as a standalone async callable.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import httpx


class FailureMode(str, Enum):
    """Kinds of failures the mock sender can simulate."""

    RANDOM = "random"
    TIMEOUT = "timeout"
    CONNECT_ERROR = "connect_error"
    HTTP_429 = "http_429"
    HTTP_500 = "http_500"
    HTTP_502 = "http_502"
    HTTP_503 = "http_503"


class HealthState(str, Enum):
    """Backend health states surfaced by the mock sender."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class SenderBehavior:
    """Configuration for how a mock backend responds to requests.

    Attributes:
        latency_ms: Average response latency in milliseconds. A small jitter
            is applied automatically.
        failure_rate: Probability of returning a failure, between 0 and 1.
        failure_mode: The kind of failure to return when a failure is triggered.
        health_state: The backend health state reported by the mock.
        response_model: The model name to include in completion responses.
        token_rate: Tokens generated per second, used to estimate response time
            for streaming responses.
        streaming: Whether streaming requests should return SSE chunks.
    """

    latency_ms: float = 50.0
    failure_rate: float = 0.0
    failure_mode: FailureMode = FailureMode.RANDOM
    health_state: HealthState = HealthState.HEALTHY
    response_model: str = "gpt-4o-mini"
    token_rate: float = 50.0
    streaming: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.failure_rate <= 1.0:
            raise ValueError("failure_rate must be between 0 and 1")


@dataclass
class _RequestContext:
    """Internal bookkeeping for a single request."""

    method: str
    url: httpx.URL
    headers: httpx.Headers
    body: bytes
    parsed_body: dict[str, Any] = field(default_factory=dict)


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _build_openai_response(
    request_body: dict[str, Any],
    model: str,
    content: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> dict[str, Any]:
    """Build a non-streaming OpenAI chat completion response."""
    return {
        "id": f"chatcmpl-mock-{random.getrandbits(32):08x}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request_body.get("model", model),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _build_stream_chunks(
    request_body: dict[str, Any],
    model: str,
    content: str,
    prompt_tokens: int,
) -> list[bytes]:
    """Build SSE chunks for a streaming OpenAI chat completion response."""
    chunks: list[bytes] = []
    request_model = request_body.get("model", model)

    # Initial role chunk
    initial = {
        "id": f"chatcmpl-mock-{random.getrandbits(32):08x}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": request_model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    chunks.append(f"data: {json.dumps(initial, ensure_ascii=False)}\n\n".encode("utf-8"))

    # Content chunks: split on whitespace for a realistic streaming feel.
    tokens = re.split(r"(\s+)", content)
    for token in tokens:
        if not token:
            continue
        chunk = {
            "id": f"chatcmpl-mock-{random.getrandbits(32):08x}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": request_model,
            "choices": [
                {"index": 0, "delta": {"content": token}, "finish_reason": None}
            ],
        }
        chunks.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))

    # Final chunk with stop reason and usage
    completion_tokens = max(1, len(tokens))
    final = {
        "id": f"chatcmpl-mock-{random.getrandbits(32):08x}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": request_model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    chunks.append(f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode("utf-8"))
    chunks.append(b"data: [DONE]\n\n")
    return chunks


def _count_tokens_from_messages(messages: list[dict[str, Any]]) -> int:
    """Return a rough token estimate for prompt messages.

    This is intentionally simple; the real gateway will use a proper tokenizer.
    """
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            # Very rough heuristic: ~4 characters per token.
            total += max(1, len(content) // 4)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    total += max(1, len(part["text"]) // 4)
    return max(1, total)


class MockLLMBackendSender:
    """Programmable mock for the gateway's outbound HTTP calls to LLM backends.

    The mock can be used in two ways:

    1. As an ``httpx.MockTransport`` handler::

           transport = httpx.MockTransport(sender.as_transport_handler())
           async with httpx.AsyncClient(transport=transport) as client:
               response = await client.post("/v1/chat/completions", json=...)

    2. As a standalone async callable::

           response = await sender(request)

    Behavior is configured per instance via :class:`SenderBehavior`. The same
    instance can be shared across many requests, and state can be updated at
    runtime to simulate backend degradation.
    """

    def __init__(self, behavior: SenderBehavior | None = None) -> None:
        self.behavior = behavior or SenderBehavior()
        self.request_log: list[_RequestContext] = []
        self._random = random.Random()

    @property
    def health_state(self) -> HealthState:
        """Current simulated health state of this backend."""
        return self.behavior.health_state

    def set_health_state(self, state: HealthState) -> None:
        """Update the simulated health state at runtime."""
        self.behavior.health_state = state

    def set_failure_rate(self, rate: float) -> None:
        """Update the simulated failure rate at runtime."""
        if not 0.0 <= rate <= 1.0:
            raise ValueError("failure_rate must be between 0 and 1")
        self.behavior.failure_rate = rate

    def reset_log(self) -> None:
        """Clear the captured request log."""
        self.request_log.clear()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Send a mock request and return a mock response."""
        return await self.send(request)

    async def send(self, request: httpx.Request) -> httpx.Response:
        """Handle a single request asynchronously and return a mock response."""
        ctx = self._build_context(request)

        # Simulate latency first so callers can observe timeouts even before parsing.
        await self._simulate_latency_async()

        return self._build_response(ctx)

    def send_sync(self, request: httpx.Request) -> httpx.Response:
        """Handle a single request synchronously and return a mock response.

        This is used by :meth:`as_transport_handler` because ``httpx.MockTransport``
        expects a synchronous handler. Latency is simulated with ``time.sleep``.
        """
        ctx = self._build_context(request)
        self._simulate_latency_sync()
        return self._build_response(ctx)

    def _build_context(self, request: httpx.Request) -> _RequestContext:
        """Build a request context and log it."""
        ctx = _RequestContext(
            method=request.method,
            url=request.url,
            headers=request.headers,
            body=request.content,
        )
        self.request_log.append(ctx)

        try:
            ctx.parsed_body = json.loads(ctx.body.decode("utf-8")) if ctx.body else {}
        except json.JSONDecodeError as exc:
            ctx.parsed_body = {"_parse_error": str(exc)}

        return ctx

    def _build_response(self, ctx: _RequestContext) -> httpx.Response:
        """Build the final HTTP response for a logged request context."""
        parse_error = ctx.parsed_body.get("_parse_error")
        if parse_error is not None:
            return httpx.Response(
                400,
                text=json.dumps({"error": {"message": f"Invalid JSON: {parse_error}"}}),
                headers={"content-type": "application/json"},
            )

        # Apply failure simulation.
        failure_response = self._maybe_failure_response()
        if failure_response is not None:
            return failure_response

        path = ctx.url.path.lower()
        if path == "/v1/chat/completions":
            return self._handle_chat_completions(ctx)

        return httpx.Response(
            404,
            text=json.dumps({"error": {"message": f"Unknown endpoint: {path}"}}),
            headers={"content-type": "application/json"},
        )

    def as_transport_handler(self) -> Callable[[httpx.Request], httpx.Response]:
        """Return a synchronous handler suitable for ``httpx.MockTransport``."""

        def handler(request: httpx.Request) -> httpx.Response:
            return self.send_sync(request)

        return handler

    async def _simulate_latency_async(self) -> None:
        """Pause to simulate network and backend latency (async variant)."""
        await asyncio.sleep(self._latency_seconds())

    def _simulate_latency_sync(self) -> None:
        """Pause to simulate network and backend latency (sync variant)."""
        time.sleep(self._latency_seconds())

    def _latency_seconds(self) -> float:
        """Return the current randomized latency in seconds."""
        jitter = self._random.gauss(0, self.behavior.latency_ms * 0.1)
        latency = max(0.0, self.behavior.latency_ms + jitter)
        return latency / 1000.0

    def _maybe_failure_response(self) -> httpx.Response | None:
        """Return a failure response if the current roll indicates failure."""
        if self._random.random() >= self.behavior.failure_rate:
            return None

        mode = self.behavior.failure_mode
        if mode == FailureMode.RANDOM:
            mode = self._random.choice(
                [
                    FailureMode.TIMEOUT,
                    FailureMode.HTTP_429,
                    FailureMode.HTTP_500,
                    FailureMode.HTTP_502,
                    FailureMode.HTTP_503,
                    FailureMode.CONNECT_ERROR,
                ]
            )

        if mode == FailureMode.TIMEOUT:
            raise asyncio.TimeoutError("Mock backend timeout")
        if mode == FailureMode.CONNECT_ERROR:
            raise httpx.ConnectError("Mock backend connection refused")

        status_map = {
            FailureMode.HTTP_429: 429,
            FailureMode.HTTP_500: 500,
            FailureMode.HTTP_502: 502,
            FailureMode.HTTP_503: 503,
        }
        status = status_map[mode]
        body = {
            "error": {
                "message": f"Mock backend returned {status}",
                "type": "mock_error",
                "code": status,
            }
        }
        return httpx.Response(
            status,
            text=json.dumps(body),
            headers={"content-type": "application/json"},
        )

    @staticmethod
    def _validate_openai_request(body: dict[str, Any]) -> tuple[bool, str]:
        """Validate that *body* conforms to the OpenAI chat completions schema.

        Returns (valid, error_message).  When *valid* is False the caller should
        return a 400 response with the error message.
        """
        if not isinstance(body, dict):
            return False, "Request body must be a JSON object."

        model = body.get("model")
        if not isinstance(model, str) or not model:
            return False, "Missing required field: 'model' must be a non-empty string."

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return False, "Missing required field: 'messages' must be a non-empty array."

        valid_roles = {"system", "user", "assistant", "tool", "function"}
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                return False, f"Message at index {idx} must be an object."
            role = msg.get("role")
            if role not in valid_roles:
                return (
                    False,
                    f"Message at index {idx} has invalid 'role': must be one of {valid_roles}.",
                )
            content = msg.get("content")
            if content is None:
                return False, f"Message at index {idx} is missing required field 'content'."
            if not isinstance(content, (str, list)):
                return (
                    False,
                    f"Message at index {idx} has invalid 'content': must be a string or array.",
                )

        return True, ""

    def _handle_chat_completions(self, ctx: _RequestContext) -> httpx.Response:
        """Handle an OpenAI chat completion request."""
        body = ctx.parsed_body

        valid, error_msg = self._validate_openai_request(body)
        if not valid:
            return httpx.Response(
                400,
                text=json.dumps(
                    {
                        "error": {
                            "message": error_msg,
                            "type": "invalid_request_error",
                            "param": None,
                            "code": None,
                        }
                    }
                ),
                headers={"content-type": "application/json"},
            )

        messages = body.get("messages", [])
        prompt_tokens = _count_tokens_from_messages(messages)

        # Extract the last user message as a basis for the reply.
        content = self._generate_reply_content(messages)
        completion_tokens = max(1, len(content.split()))

        is_streaming = body.get("stream", False) and self.behavior.streaming
        if is_streaming:
            chunks = _build_stream_chunks(
                body, self.behavior.response_model, content, prompt_tokens
            )
            return httpx.Response(
                200,
                content=b"".join(chunks),
                headers={"content-type": "text/event-stream"},
            )

        response_data = _build_openai_response(
            body,
            self.behavior.response_model,
            content,
            prompt_tokens,
            completion_tokens,
        )
        return httpx.Response(
            200,
            text=json.dumps(response_data, ensure_ascii=False),
            headers={"content-type": "application/json"},
        )

    @staticmethod
    def _generate_reply_content(messages: list[dict[str, Any]]) -> str:
        """Generate a plausible assistant reply based on the prompt."""
        user_texts: list[str] = []
        for message in messages:
            if message.get("role") == "user":
                content = message.get("content", "")
                if isinstance(content, str):
                    user_texts.append(content)
                elif isinstance(content, list):
                    texts = [
                        part["text"]
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ]
                    user_texts.extend(texts)

        if not user_texts:
            return "I'm a mock assistant. How can I help you today?"

        last_user = user_texts[-1].strip()
        lower = last_user.lower()
        if any(kw in lower for kw in ("hello", "hi", "hey")):
            return "Hello! This is a mock backend response."
        if "weather" in lower:
            return "The mock weather is sunny with a chance of tests."
        if any(kw in lower for kw in ("error", "fail")):
            return "This mock backend processed your error-related prompt."
        return f"Mock response for: {last_user[:60]}{'...' if len(last_user) > 60 else ''}"


class MockGatewayClient:
    """Convenience client that sends requests to the gateway itself.

    This is useful for end-to-end tests and local manual testing. It builds
    a realistic OpenAI chat completion payload and can assert on the response.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8000", api_key: str = "mock-key") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "MockGatewayClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat_completions(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        stream: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Send a chat completion request to the gateway.

        Returns the parsed JSON response for non-streaming requests, or a list
        of parsed SSE data payloads for streaming requests.
        """
        if self._client is None:
            raise RuntimeError("Client is not open. Use async with.")

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        response = await self._client.post("/v1/chat/completions", json=payload)
        response.raise_for_status()

        if stream:
            return self._parse_sse(response.text)
        return response.json()

    @staticmethod
    def _parse_sse(text: str) -> list[dict[str, Any]]:
        """Parse a text/event-stream body into a list of JSON payloads."""
        events: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: "):].strip()
            if data == "[DONE]":
                break
            try:
                events.append(json.loads(data))
            except json.JSONDecodeError:
                continue
        return events
