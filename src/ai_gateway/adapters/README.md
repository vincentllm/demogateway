# GW-003 protocol boundary

```python
from ai_gateway.adapters import ProtocolAdapter
adapter = ProtocolAdapter("claude", {"public-chat": "configured-upstream-model"})
encoded = adapter.encode_request(canonical_body, backend, upstream_key)
response = adapter.decode_response(status, headers, response_body)
state = adapter.new_state(canonical_body["model"], include_usage=bool(
    (canonical_body.get("stream_options") or {}).get("include_usage", False)))
payloads = adapter.decode_event(event, data, state)
```

Provider names: `openai`, `deepseek`, `claude`. Aliases must be explicitly configured, including identity aliases for direct model names. Backend is a server-owned mapping or object with provider/base_url. Base URL is an API root, optionally ending in `/v1`, with no query, fragment or userinfo. Encoded request has only method/url/headers/json; no client headers are accepted. No HTTP, quota, config loading or key resolution occurs here. Upstream secrets are provided by the caller.

The three methods are bound callbacks, matching the public contract signatures. JSON response bodies accept dict/UTF-8 bytes/JSON string. HTTP error bodies and headers are deliberately not reflected; the caller retains the actual upstream status. `AdapterError` exposes safe code/status/param and payload() for local input or malformed upstream failures. Never expose the original body or exception context.

SSE framing belongs to GW-004. `decode_event` accepts an already framed event name (or None), JSON data string or dict, and a fresh empty dictionary or `new_state()` dictionary per request. An empty dictionary is initialized in place on the first callback, so `adapter.decode_event` can be passed directly to GW-004. Complete caller-created state is retained; incomplete nonempty or non-dictionary states fail with safe `AdapterError("invalid_stream_state")`. Existing terminal state remains inert. It returns canonical JSON dicts plus the literal string `[DONE]`, not serialized SSE frames. Transport serializes dicts to `data: <json>\n\n`, handles error dictionaries as SSE error events and closes the connection, and catches AdapterError as a terminal upstream failure. Do not append another DONE: the adapter emits it once; any later event returns []. EOF without adapter success must be treated as failure. On cancellation/timeout, transport owns the terminal outcome and closes the connection.

`state['usage']` is None before usage arrives and otherwise normalized cumulative accounting usage; Claude cache read/creation input tokens count toward prompt_tokens. `state['outcome']` is set only at success/error. Streaming Claude finish is deferred to message_stop to avoid manufacturing success on a truncated stream. Its usage chunk is emitted only with include_usage=True, while accounting usage is always kept in state. OpenAI/DeepSeek chunks preserve upstream usage selection; caller forwards stream_options upstream. Missing usage remains None for conservative quota handling. No token estimator or reservation logic is provided.

Claude tool argument fragments are forwarded unchanged as function.arguments deltas with dense tool indices, independent of content-block indices. Empty tool input yields `{}` on block stop if no fragments arrived. No prompt or complete accumulated tool arguments are retained in stream state. Leading system messages are separate text blocks; later system messages explicitly fail. Text-only string message contents and function tools are supported. Multimodal blocks, thinking blocks, server tools, unknown nested fields, strict tools, stop and response_format are explicitly unsupported. Claude requires an explicit positive max_tokens; the integrator must supply its configured default if omitted by the client. tool_choice=none removes Claude tools to prevent invocation.

JSON responses retain the upstream model name. For streamed public alias names pass the alias to new_state; there is no reverse alias guessing (multiple public aliases can map to one upstream model). Integration may normalize the JSON model name using its selected route.

Official protocol references checked during development:
- https://platform.claude.com/docs/en/api/messages/create
- https://platform.claude.com/docs/en/build-with-claude/streaming
- https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events
- https://api-docs.deepseek.com/api/create-chat-completion/

See acceptance.md for criteria/matrix and HANDOFF.md for actual evidence and remaining limits. This module does not extend existing BackendConfig/provider registries/router models; those are integration-owned.
