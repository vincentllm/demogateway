"""Known wire fixtures exercise the actual adapter and transport together."""
import json

import httpx
import pytest

from ai_gateway.adapters import ProtocolAdapter
from ai_gateway.auth import AuthConfig, AuthManager, hash_client_key
from ai_gateway.transport import StreamingTransport


class Source(httpx.AsyncByteStream):
    def __init__(self, wire):
        self.wire = wire
        self.closed = False

    async def __aiter__(self):
        # Split UTF-8 and SSE frames; parser and adapter both run.
        for offset in range(0, len(self.wire), 3):
            yield self.wire[offset:offset + 3]

    async def aclose(self):
        self.closed = True


def frame(data, event=None):
    return ((f"event: {event}\n" if event else "") + "data: " + (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)) + "\n\n").encode()


def wire(provider, tool, ending):
    if provider == "claude":
        parts = [frame({"type": "message_start", "message": {"id": "m1", "model": "upstream", "usage": {"input_tokens": 4, "output_tokens": 1}}}, "message_start")]
        block = {"type": "tool_use", "id": "c1", "name": "fn", "input": {}} if tool else {"type": "text", "text": ""}
        parts += [frame({"type": "content_block_start", "index": 0, "content_block": block}, "content_block_start")]
        deltas = [{"type": "input_json_delta", "partial_json": '{"city":'}, {"type": "input_json_delta", "partial_json": '"北京"}'}] if tool else [{"type": "text_delta", "text": "你好"}]
        parts += [frame({"type": "content_block_delta", "index": 0, "delta": delta}, "content_block_delta") for delta in deltas]
        parts += [frame({"type": "content_block_stop", "index": 0}, "content_block_stop")]
        if ending == "success":
            parts += [frame({"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn"}, "usage": {"output_tokens": 6}}, "message_delta"), frame({"type": "message_stop"}, "message_stop")]
    else:
        delta = {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "fn", "arguments": '{"city":"北京"}'}}]} if tool else {"content": "你好"}
        parts = [frame({"id": "m1", "model": "upstream", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})]
        if ending == "success":
            parts += [frame({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}]}), frame({"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}}), frame("[DONE]")]
    if ending == "error":
        parts += [frame({"error": {"message": "private upstream text"}})]
    return b"".join(parts)


@pytest.mark.parametrize("provider", ["openai", "deepseek", "claude"])
@pytest.mark.parametrize("tool", [False, True])
@pytest.mark.parametrize("ending", ["success", "error", "truncated"])
async def test_actual_adapter_transport(provider, tool, ending):
    adapter = ProtocolAdapter(provider, {"public": "upstream"})
    source = Source(wire(provider, tool, ending))
    terminals = []
    async def send(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)
    encoded = adapter.encode_request({"model": "public", "messages": [{"role": "user", "content": "hi"}], "stream": True, "max_tokens": 20}, {"provider": provider, "base_url": "https://example.test"}, "fake-provider-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        async with StreamingTransport(client).stream(encoded, adapter.decode_event, lambda *x: terminals.append(x)) as stream:
            output = b"".join([chunk async for chunk in stream])
    assert source.closed
    assert len(terminals) == 1
    assert b"private upstream text" not in output
    if ending == "success":
        assert output.count(b"data: [DONE]\n\n") == 1
        assert b"event: error" not in output
        assert terminals == [({"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}, "success")]
        chunks = [json.loads(line[6:]) for line in output.decode().splitlines() if line.startswith("data: {")]
        if tool:
            calls = [call for chunk in chunks for choice in chunk["choices"] for call in choice["delta"].get("tool_calls", [])]
            assert "".join(call["function"].get("arguments", "") for call in calls) == '{"city":"北京"}'
        else:
            assert "".join(choice["delta"].get("content", "") for chunk in chunks for choice in chunk["choices"]) == "你好"
    else:
        assert b"event: error" in output and b"[DONE]" not in output
        assert terminals[0][1] == "upstream_error"


@pytest.mark.parametrize("provider", ["openai", "deepseek", "claude"])
def test_auth_to_adapter_key_separation(provider):
    manager = AuthManager(AuthConfig.from_mapping({"clients": [{"client_id": "a", "allowed_models": ["public"], "keys": [{"key_id": "client-1", "sha256": hash_client_key("fake-client-token")}]}], "upstream_pools": [{"key_ref": "pool", "keys": [{"key_id": "upstream-1", "secret": "fake-provider-token"}]}]}))
    identity = manager.authenticate("Bearer fake-client-token", "public")
    assert identity.client_id == "a"
    adapter = ProtocolAdapter(provider, {"public": "upstream"})
    encoded = adapter.encode_request({"model": "public", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 20}, {"provider": provider, "base_url": "https://example.test"}, manager.resolve_upstream_key("pool"))
    assert "fake-client-token" not in json.dumps(encoded)
    assert "fake-provider-token" in json.dumps(encoded["headers"])
