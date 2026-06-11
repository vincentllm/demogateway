"""Tests for the mock HTTP sender."""

import asyncio

import httpx
import pytest

from ai_gateway.testing import MockLLMBackendSender, SenderBehavior
from ai_gateway.testing.mock_http_sender import FailureMode, HealthState


@pytest.fixture
def sender() -> MockLLMBackendSender:
    return MockLLMBackendSender(SenderBehavior(latency_ms=0))


@pytest.mark.asyncio
async def test_chat_completions_json(sender: MockLLMBackendSender) -> None:
    request = httpx.Request(
        "POST",
        "https://example.com/v1/chat/completions",
        json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hello!"}],
        },
    )
    response = await sender.send(request)

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["usage"]["prompt_tokens"] > 0
    assert data["usage"]["completion_tokens"] > 0


@pytest.mark.asyncio
async def test_chat_completions_streaming(sender: MockLLMBackendSender) -> None:
    request = httpx.Request(
        "POST",
        "https://example.com/v1/chat/completions",
        json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hello!"}],
            "stream": True,
        },
    )
    response = await sender.send(request)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert b"data: [DONE]" in response.content


@pytest.mark.asyncio
async def test_chat_completions_uses_configured_model(sender: MockLLMBackendSender) -> None:
    sender.behavior.response_model = "custom-model"
    request = httpx.Request(
        "POST",
        "https://example.com/v1/chat/completions",
        json={
            "model": "upstream-model",
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )
    response = await sender.send(request)
    data = response.json()
    assert data["model"] == "upstream-model"


@pytest.mark.asyncio
async def test_unknown_endpoint(sender: MockLLMBackendSender) -> None:
    request = httpx.Request("GET", "https://example.com/v1/models")
    response = await sender.send(request)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_invalid_json(sender: MockLLMBackendSender) -> None:
    request = httpx.Request(
        "POST",
        "https://example.com/v1/chat/completions",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    response = await sender.send(request)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_failure_rate_http_500(sender: MockLLMBackendSender) -> None:
    sender.behavior.failure_rate = 1.0
    sender.behavior.failure_mode = FailureMode.HTTP_500
    request = httpx.Request(
        "POST",
        "https://example.com/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
    )
    response = await sender.send(request)
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_failure_rate_timeout(sender: MockLLMBackendSender) -> None:
    sender.behavior.failure_rate = 1.0
    sender.behavior.failure_mode = FailureMode.TIMEOUT
    request = httpx.Request(
        "POST",
        "https://example.com/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
    )
    with pytest.raises(asyncio.TimeoutError):
        await sender.send(request)


@pytest.mark.asyncio
async def test_transport_handler_async() -> None:
    sender = MockLLMBackendSender(SenderBehavior(latency_ms=0))
    transport = httpx.MockTransport(sender.as_transport_handler())
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post(
            "https://mock.backend/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]},
        )
    assert response.status_code == 200


def test_transport_handler_sync() -> None:
    sender = MockLLMBackendSender(SenderBehavior(latency_ms=0))
    transport = httpx.MockTransport(sender.as_transport_handler())
    with httpx.Client(transport=transport) as client:
        response = client.post(
            "https://mock.backend/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]},
        )
    assert response.status_code == 200


def test_sender_behavior_validation() -> None:
    with pytest.raises(ValueError):
        SenderBehavior(failure_rate=1.5)


def test_health_state_mutations(sender: MockLLMBackendSender) -> None:
    assert sender.health_state == HealthState.HEALTHY
    sender.set_health_state(HealthState.DEGRADED)
    assert sender.health_state == HealthState.DEGRADED
    sender.set_failure_rate(0.5)
    assert sender.behavior.failure_rate == 0.5


@pytest.mark.asyncio
async def test_request_log(sender: MockLLMBackendSender) -> None:
    request = httpx.Request(
        "POST",
        "https://example.com/v1/chat/completions",
        json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]},
    )
    await sender.send(request)
    assert len(sender.request_log) == 1
    assert sender.request_log[0].parsed_body["model"] == "gpt-4"
