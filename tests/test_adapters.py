"""Protocol fixtures; no provider credentials, networking, transport or quota."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from ai_gateway.adapters import AdapterError, ProtocolAdapter


PROVIDERS = ["openai", "deepseek", "claude"]


def adapter(provider="claude"):
    return ProtocolAdapter(provider, {"public": "upstream"})


def body(**kwargs):
    return {"model": "public", "messages": [{"role": "user", "content": "你好"}], "max_tokens": 32, **kwargs}


def backend(provider="claude", base="https://example.test"):
    return {"provider": provider, "base_url": base}


def tool():
    return {"type": "function", "function": {"name": "weather", "description": "weather lookup", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}


def call():
    return {"id": "call-1", "type": "function", "function": {"name": "weather", "arguments": '{"city":"北京"}'}}


def event(a, s, kind, **payload):
    return a.decode_event(kind, {"type": kind, **payload}, s)


def start(a, s):
    return event(a, s, "message_start", message={"id": "m1", "model": "upstream", "usage": {"input_tokens": 10, "output_tokens": 1}})


def block_start(a, s, index=0, block=None):
    return event(a, s, "content_block_start", index=index, content_block=block or {"type": "text", "text": ""})


def block_stop(a, s, index=0):
    return event(a, s, "content_block_stop", index=index)


def finish(a, s, reason="end_turn", tokens=5):
    event(a, s, "message_delta", delta={"stop_reason": reason}, usage={"output_tokens": tokens})
    return event(a, s, "message_stop")


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/"])
def test_text_request_url_headers_alias_and_no_mutation(provider, suffix):
    a = adapter(provider)
    original = body(stream=True, temperature=0, top_p=0.8, stream_options={"include_usage": True})
    snapshot = deepcopy(original)
    result = a.encode_request(original, SimpleNamespace(**backend(provider, "https://example.test" + suffix)), "server-key")
    assert result["url"] == "https://example.test/v1/" + ("messages" if provider == "claude" else "chat/completions")
    assert result["method"] == "POST"
    assert result["json"]["model"] == "upstream"
    assert result["json"]["temperature"] == 0
    assert original == snapshot
    assert result["headers"].get("x-api-key" if provider == "claude" else "Authorization") == ("server-key" if provider == "claude" else "Bearer server-key")
    assert "server-key" not in json.dumps(result["json"])


@pytest.mark.parametrize("provider", PROVIDERS)
def test_tool_request_round_trip(provider):
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": "checking", "tool_calls": [call()]},
                {"role": "tool", "tool_call_id": "call-1", "content": "sunny"}]
    result = adapter(provider).encode_request(body(messages=messages, tools=[tool()], tool_choice={"type": "function", "function": {"name": "weather"}}), backend(provider), "key")["json"]
    if provider == "claude":
        assert result["system"] == [{"type": "text", "text": "rules"}]
        assert result["messages"][1]["content"] == [{"type": "text", "text": "checking"}, {"type": "tool_use", "id": "call-1", "name": "weather", "input": {"city": "北京"}}]
        assert result["messages"][2]["content"] == [{"type": "tool_result", "tool_use_id": "call-1", "content": "sunny"}]
        assert result["tool_choice"] == {"type": "tool", "name": "weather"}
        assert result["tools"][0]["input_schema"] == tool()["function"]["parameters"]
    else:
        assert result["messages"] == messages
        assert result["tools"] == [tool()]


@pytest.mark.parametrize("choice, expected", [("auto", "auto"), ("required", "any"), ("none", None)])
def test_claude_tool_choice(choice, expected):
    result = adapter().encode_request(body(tools=[tool()], tool_choice=choice), backend(), "key")["json"]
    if expected:
        assert result["tool_choice"] == {"type": expected}
    else:
        assert "tool_choice" not in result and "tools" not in result


def test_claude_merges_parallel_tool_results_and_systems():
    messages = [{"role": "system", "content": "a"}, {"role": "system", "content": "b"},
                {"role": "assistant", "content": None, "tool_calls": [call()]},
                {"role": "tool", "tool_call_id": "call-1", "content": "one"},
                {"role": "tool", "tool_call_id": "call-2", "content": "two"}]
    result = adapter().encode_request(body(messages=messages), backend(), "key")["json"]
    assert [b["text"] for b in result["system"]] == ["a", "b"]
    assert [b["content"] for b in result["messages"][-1]["content"]] == ["one", "two"]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("extra", [{"user": "id"}, {"base_url": "https://evil.test"}, {"stop": ["x"]}, {"response_format": {}}])
def test_unknown_fields_explicitly_rejected(provider, extra):
    with pytest.raises(AdapterError, match="unsupported_field"):
        adapter(provider).encode_request(body(**extra), backend(provider), "key")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_unknown_routes(provider):
    a = adapter(provider)
    with pytest.raises(AdapterError, match="unknown_model"):
        a.encode_request(body(model="not-configured"), backend(provider), "key")
    with pytest.raises(AdapterError, match="unknown_provider"):
        a.encode_request(body(), backend("other"), "key")
    with pytest.raises(AdapterError, match="unknown_provider"):
        ProtocolAdapter("other", {})
    with pytest.raises(AdapterError, match="unknown_model"):
        ProtocolAdapter(provider, {}).encode_request(body(), backend(provider), "key")


@pytest.mark.parametrize("messages", [[], [{"role": "developer", "content": "x"}], [{"role": "user", "content": [{"type": "image_url"}]}], [{"role": "user", "content": "x", "name": "ignored"}], [{"role": "tool", "content": "x"}], [{"role": "assistant", "content": None, "tool_calls": [{**call(), "function": {"name": "weather", "arguments": "[]"}}]}]])
def test_invalid_or_unsupported_messages(messages):
    with pytest.raises(AdapterError):
        adapter().encode_request(body(messages=messages), backend(), "key")


def test_claude_max_tokens_and_system_position_rejected():
    for value in (None, 0, -1, True):
        with pytest.raises(AdapterError, match="max_tokens_required"):
            adapter().encode_request(body(max_tokens=value), backend(), "key")
    with pytest.raises(AdapterError, match="unsupported_system_position"):
        adapter().encode_request(body(messages=[{"role": "user", "content": "a"}, {"role": "system", "content": "b"}]), backend(), "key")


@pytest.mark.parametrize("base", ["file:///tmp/foo", "https://u:secret@example.test", "https://example.test?q=x", "https://example.test#x"])
def test_unsafe_url_rejected(base):
    with pytest.raises(AdapterError, match="invalid_base_url"):
        adapter().encode_request(body(), backend(base=base), "key")


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("status", [400, 401, 429, 500, 529])
def test_http_error_sanitized(provider, status):
    result = adapter(provider).decode_response(status, {"Authorization": "secret"}, b'{"error":{"message":"secret prompt key"}}')
    assert result["error"]["code"] == "upstream_http_error"
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("is_tool", [False, True])
def test_compatible_json_response(provider, is_tool):
    message = {"role": "assistant", "content": None if is_tool else "hello"}
    if is_tool:
        message["tool_calls"] = [call()]
    raw = {"id": "id", "object": "chat.completion", "model": "upstream", "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if is_tool else "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
    assert adapter(provider).decode_response(200, {}, json.dumps(raw).encode()) == raw


@pytest.mark.parametrize("reason, expected", [("end_turn", "stop"), ("stop_sequence", "stop"), ("max_tokens", "length"), ("tool_use", "tool_calls"), ("refusal", "content_filter")])
def test_claude_response_content_usage_and_stop(reason, expected):
    raw = {"id": "m1", "model": "upstream", "content": [{"type": "text", "text": "hello"}, {"type": "tool_use", "id": "call-1", "name": "weather", "input": {"city": "北京"}}, {"type": "text", "text": "!"}], "stop_reason": reason, "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 2}}
    result = adapter().decode_response(200, {}, raw)
    assert result["choices"][0]["message"] == {"role": "assistant", "content": "hello!", "tool_calls": [call()]}
    assert result["choices"][0]["finish_reason"] == expected
    assert result["usage"] == {"prompt_tokens": 16, "completion_tokens": 3, "total_tokens": 19}


def test_claude_stream_multiple_blocks_and_partial_json():
    a, s = adapter(), adapter().new_state("public", include_usage=True)
    chunks = start(a, s)
    block_start(a, s)
    chunks += event(a, s, "content_block_delta", index=0, delta={"type": "text_delta", "text": "hi"})
    block_stop(a, s)
    for index, call_id in [(1, "call-1"), (3, "call-2")]:
        chunks += block_start(a, s, index, {"type": "tool_use", "id": call_id, "name": "weather", "input": {}})
        for part in ['{"city":', '"北京"}']:
            chunks += event(a, s, "content_block_delta", index=index, delta={"type": "input_json_delta", "partial_json": part})
        block_stop(a, s, index)
    assert event(a, s, "ping") == []
    assert event(a, s, "future_event") == []
    event(a, s, "message_delta", delta={}, usage={"output_tokens": 3})
    chunks += finish(a, s, "tool_use", 7)
    assert s["usage"] == {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17}
    assert chunks[-1] == "[DONE]"
    assert chunks[-3]["choices"][0]["finish_reason"] == "tool_calls"
    assert {c["model"] for c in chunks if isinstance(c, dict)} == {"public"}
    deltas = [c["choices"][0]["delta"]["tool_calls"][0] for c in chunks if isinstance(c, dict) and c["choices"] and c["choices"][0]["delta"].get("tool_calls")]
    for index in (0, 1):
        assert "".join(d["function"]["arguments"] for d in deltas if d["index"] == index) == '{"city":"北京"}'
    assert event(a, s, "message_stop") == []


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_compatible_stream_usage_tools_done(provider):
    a = adapter(provider)
    s = a.new_state("public")
    tool_delta = {"id": "id", "model": "upstream", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, **call()}]}, "finish_reason": None}]}
    assert a.decode_event(None, json.dumps(tool_delta), s)[0]["choices"] == tool_delta["choices"]
    a.decode_event(None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}, s)
    usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    assert a.decode_event(None, {"choices": [], "usage": usage}, s)[0]["usage"] == usage
    assert s["usage"] == usage
    assert a.decode_event(None, "[DONE]", s) == ["[DONE]"]
    assert a.decode_event(None, "[DONE]", s) == []
    assert s["outcome"] == "success"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_stream_error_has_no_normal_finish_or_done(provider):
    a = adapter(provider)
    s = a.new_state()
    result = a.decode_event("error", {"error": {"message": "secret-key prompt"}}, s)
    assert "secret" not in json.dumps(result)
    assert result[0]["error"]["code"] == "upstream_stream_error"
    assert a.decode_event(None, "[DONE]", s) == []
    assert s["outcome"] == "upstream_error"


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("raw", ["not json secret", "[]", b'\xff', {}])
def test_malformed_response_safe(provider, raw):
    with pytest.raises(AdapterError) as caught:
        adapter(provider).decode_response(200, {}, raw)
    assert "secret" not in str(caught.value)


def test_stream_state_isolation_and_usage_missing():
    a = adapter()
    one, two = a.new_state(), a.new_state()
    start(a, one)
    assert two["usage"] is None and two["blocks"] == {}
    with pytest.raises(AdapterError, match="invalid_event_order"):
        event(a, two, "message_stop")
    assert one["terminal"] is False


@pytest.mark.parametrize("provider", PROVIDERS)
def test_truncated_stream_never_success(provider):
    a = adapter(provider)
    s = a.new_state()
    with pytest.raises(AdapterError, match="premature_stream_end"):
        a.decode_event(None, "[DONE]", s)
    assert s["outcome"] == "upstream_error"
    assert a.decode_event(None, "[DONE]", s) == []


def test_claude_empty_tool_input_and_nonempty_initial_input():
    for initial, expected in [({}, "{}"), ({"x": 1}, '{"x":1}')]:
        a, s = adapter(), adapter().new_state()
        start(a, s)
        chunks = block_start(a, s, 0, {"type": "tool_use", "id": "c1", "name": "fn", "input": initial})
        chunks += block_stop(a, s)
        assert "".join(c["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] for c in chunks) == expected
        assert finish(a, s, "tool_use")[-1] == "[DONE]"


@pytest.mark.parametrize("mode", ["unknown_block", "unknown_delta", "open_block_stop", "duplicate_start", "mismatch", "unknown_reason"])
def test_claude_bad_event_sequences(mode):
    a, s = adapter(), adapter().new_state()
    start(a, s)
    with pytest.raises(AdapterError):
        if mode == "unknown_block":
            block_start(a, s, block={"type": "thinking"})
        elif mode == "duplicate_start":
            start(a, s)
        elif mode == "mismatch":
            a.decode_event("ping", {"type": "message_stop"}, s)
        elif mode == "unknown_reason":
            finish(a, s, "pause_turn")
        else:
            block_start(a, s)
            if mode == "unknown_delta":
                event(a, s, "content_block_delta", index=0, delta={"type": "signature_delta"})
            else:
                finish(a, s)
    assert s["terminal"] and s["outcome"] == "upstream_error"


@pytest.mark.parametrize("usage", [{"prompt_tokens": -1, "completion_tokens": 0}, {"prompt_tokens": True, "completion_tokens": 0}, {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 99}])
def test_invalid_usage_rejected(usage):
    with pytest.raises(AdapterError, match="invalid_upstream_usage"):
        adapter("openai").decode_response(200, {}, {"choices": [{}], "usage": usage})


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("choices", [[{}], [1], [{"index": 0, "message": {"role": "assistant", "content": 123}, "finish_reason": "stop"}], [{"index": 1, "message": {"role": "assistant", "content": "text"}, "finish_reason": "stop"}]])
def test_review_malformed_compatible_choices(provider, choices):
    with pytest.raises(AdapterError):
        adapter(provider).decode_response(200, {}, {"choices": choices})


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_review_malformed_stream_choices_terminal_and_content_after_finish(provider):
    a = adapter(provider)
    s = a.new_state()
    with pytest.raises(AdapterError):
        a.decode_event(None, {"choices": [1]}, s)
    assert s["terminal"] and s["outcome"] == "upstream_error"
    s = a.new_state()
    a.decode_event(None, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}, s)
    with pytest.raises(AdapterError, match="content_after_finish"):
        a.decode_event(None, {"choices": [{"index": 0, "delta": {"content": "extra"}, "finish_reason": None}]}, s)
    assert a.decode_event(None, "[DONE]", s) == []


@pytest.mark.parametrize("include_usage", [False, True])
def test_review_claude_stream_options(include_usage):
    a = adapter()
    s = a.new_state(include_usage=include_usage)
    start(a, s)
    result = finish(a, s)
    assert len([c for c in result if isinstance(c, dict) and "usage" in c]) == int(include_usage)
    assert s["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert result.count("[DONE]") == 1


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("options", [{"stream_options": {"include_usage": True}}, {"stream": True, "stream_options": {"other": True}}, {"stream": True, "stream_options": {"include_usage": 1}}, {"tools": "bad"}, {"tools": [{"type": "function", "function": {"name": "fn", "strict": True}}]}, {"tool_choice": {"type": "function", "function": {"name": "fn"}, "extra": True}}])
def test_review_unsupported_nested_fields_and_stream_options(provider, options):
    with pytest.raises(AdapterError):
        adapter(provider).encode_request(body(**options), backend(provider), "key")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_review_null_options_nonmutation_and_header_injection(provider):
    a = adapter(provider)
    b = body(temperature=None, top_p=None, tools=None, tool_choice=None)
    encoded = a.encode_request(b, backend(provider), "key")
    assert "temperature" not in encoded["json"]
    assert b["temperature"] is None
    with pytest.raises(AdapterError, match="invalid_upstream_key"):
        a.encode_request(body(), backend(provider), "key\r\nAuthorization: secret")
    with pytest.raises(AdapterError, match="unsupported_field"):
        a.encode_request(body(unknown=None), backend(provider), "key")


def test_review_text_only_claude_response_and_missing_usage_compatible_stream():
    response = adapter().decode_response(200, {}, {"id": "id", "model": "upstream", "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 2}})
    assert response["choices"][0]["message"] == {"role": "assistant", "content": "hi"}
    a = adapter("openai")
    s = a.new_state()
    a.decode_event(None, {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]}, s)
    assert a.decode_event(None, "[DONE]", s) == ["[DONE]"]
    assert s["usage"] is None


def test_review_claude_text_stream_error_after_stop_reason_no_success():
    a = adapter()
    s = a.new_state()
    start(a, s)
    block_start(a, s)
    assert event(a, s, "content_block_delta", index=0, delta={"type": "text_delta", "text": "hi"})[0]["choices"][0]["delta"] == {"content": "hi"}
    block_stop(a, s)
    event(a, s, "message_delta", delta={"stop_reason": "end_turn"}, usage={"output_tokens": 2})
    result = a.decode_event("error", {"error": {"message": "secret"}}, s)
    assert len(result) == 1 and "error" in result[0]
    assert event(a, s, "message_stop") == []


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_review_compatible_tool_delta_optional_arguments(provider):
    a, s = adapter(provider), adapter(provider).new_state()
    for call_delta in [{"index": 0, "id": "c1", "type": "function", "function": {"name": "fn"}}, {"index": 0, "function": {"arguments": "{}"}}]:
        raw = {"choices": [{"index": 0, "delta": {"tool_calls": [call_delta]}, "finish_reason": None}]}
        assert a.decode_event(None, raw, s) == [raw]


def test_review_claude_context_limit_and_unknown_stop():
    a, s = adapter(), adapter().new_state()
    start(a, s)
    result = finish(a, s, "model_context_window_exceeded")
    assert result[0]["choices"][0]["finish_reason"] == "length"
    with pytest.raises(AdapterError, match="unsupported_stop_reason"):
        a.decode_response(200, {}, {"id": "id", "model": "upstream", "content": [], "stop_reason": "pause_turn", "usage": {"input_tokens": 1, "output_tokens": 0}})


@pytest.mark.parametrize("provider", PROVIDERS)
def test_transport_empty_state_initializes_once_and_finishes(provider):
    a, state = adapter(provider), {}
    state_id = id(state)
    if provider == "claude":
        first = start(a, state)
        created, blocks = state["created"], state["blocks"]
        block_start(a, state)
        event(a, state, "content_block_delta", index=0, delta={"type": "text_delta", "text": "hi"})
        block_stop(a, state)
        final = finish(a, state)
        assert state["created"] == created and state["blocks"] is blocks
        assert first[0]["choices"][0]["delta"]["role"] == "assistant"
    else:
        first = a.decode_event("message", {"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}, state)
        a.decode_event("message", {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}, state)
        final = a.decode_event("message", "[DONE]", state)
        assert first[0]["choices"][0]["delta"] == {"content": "hi"}
    assert id(state) == state_id
    assert final.count("[DONE]") == 1
    assert state["terminal"] is True and state["outcome"] == "success"
    assert a.decode_event(None, "[DONE]", state) == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_transport_empty_state_failure_remains_terminal(provider):
    a, state = adapter(provider), {}
    with pytest.raises(AdapterError):
        a.decode_event(None, "invalid-json", state)
    assert state["terminal"] is True and state["outcome"] == "upstream_error"
    assert a.decode_event(None, "[DONE]", state) == []
    fresh = {}
    assert "error" in a.decode_event("error", {"error": {"message": "private"}}, fresh)[0]
    assert fresh["outcome"] == "upstream_error"
    assert a.decode_event(None, "[DONE]", fresh) == []


def test_transport_preserves_preinitialized_options_and_isolates_empty_states():
    a = adapter()
    explicit = a.new_state("public", include_usage=True)
    created, blocks = explicit["created"], explicit["blocks"]
    start(a, explicit)
    result = finish(a, explicit)
    assert explicit["created"] == created and explicit["blocks"] is blocks
    assert result[0]["model"] == "public"
    assert result[1]["choices"] == [] and result[1]["usage"]["total_tokens"] == 15
    one, two = {}, {}
    start(a, one)
    start(a, two)
    block_start(a, one)
    assert two["blocks"] == {} and two["terminal"] is False
    finish(a, two)
    assert one["terminal"] is False


@pytest.mark.parametrize("state", [None, [], "private", 1, {"usage": None}, {"terminal": False}, {"terminal": "private"}])
def test_transport_invalid_initial_state_is_safe(state):
    a = adapter()
    with pytest.raises(AdapterError, match="^invalid_stream_state$") as caught:
        start(a, state)
    assert caught.value.status == 502
    assert "private" not in json.dumps(caught.value.payload())
    if isinstance(state, dict):
        assert state["terminal"] is True and state["outcome"] == "upstream_error"
        assert a.decode_event(None, "[DONE]", state) == []


def test_transport_existing_terminal_sentinel_remains_inert():
    a, state = adapter(), {"terminal": True}
    assert a.decode_event(None, "not-json", state) == []
    assert state == {"terminal": True}
