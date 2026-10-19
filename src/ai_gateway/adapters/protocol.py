from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class AdapterError(ValueError):
    """Safe local protocol failure; never includes request or upstream body."""

    def __init__(self, code: str, *, status: int = 400, param: str | None = None):
        self.code, self.status, self.param = code, status, param
        super().__init__(code)

    def payload(self) -> dict:
        return {"error": {"message": self.code, "type": "invalid_request_error"
                          if self.status < 500 else "upstream_error",
                          "param": self.param, "code": self.code}}


def _fail(code: str = "invalid_upstream_payload") -> None:
    raise AdapterError(code, status=502)


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        _fail()
    return value


def _parse(value: Any) -> dict:
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except (ValueError, UnicodeError):
            _fail()
    return _object(value)


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        _fail("invalid_upstream_usage")
    return value


def _usage(raw: dict, claude: bool = False) -> dict:
    if claude:
        prompt = _count(raw["input_tokens"])
        prompt += sum(_count(raw.get(k, 0)) for k in
                      ("cache_creation_input_tokens", "cache_read_input_tokens"))
        completion = _count(raw["output_tokens"])
    else:
        prompt, completion = _count(raw["prompt_tokens"]), _count(raw["completion_tokens"])
        if "total_tokens" in raw and _count(raw["total_tokens"]) != prompt + completion:
            _fail("invalid_upstream_usage")
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def _stop(reason: str) -> str:
    mapping = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
               "tool_use": "tool_calls", "refusal": "content_filter",
               "model_context_window_exceeded": "length"}
    if reason not in mapping:
        _fail("unsupported_stop_reason")
    return mapping[reason]


def _keys(value: Mapping, allowed: set[str], param: str) -> None:
    if set(value) - allowed:
        raise AdapterError("unsupported_field", param=param)


def _arguments(value: str) -> dict:
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass
    raise AdapterError("invalid_tool_arguments", param="messages")


def _compatible_choices(raw: dict, *, streaming: bool) -> list:
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) > 1 or (not streaming and not choices):
        _fail()
    if not choices and raw.get("usage") is None:
        _fail()
    for value in choices:
        choice = _object(value)
        if type(choice.get("index")) is not int or choice["index"] != 0:
            _fail()
        reason = choice.get("finish_reason")
        if reason is not None and reason not in {"stop", "length", "tool_calls", "content_filter"}:
            _fail("unsupported_stop_reason")
        if not streaming and reason is None:
            _fail()
        message = _object(choice["delta" if streaming else "message"])
        if not streaming and message.get("role") != "assistant":
            _fail()
        if message.get("content") is not None and not isinstance(message["content"], str):
            _fail()
        if "tool_calls" in message:
            if not isinstance(message["tool_calls"], list):
                _fail()
            for value in message["tool_calls"]:
                call = _object(value)
                fn = _object(call.get("function", {}) if streaming else call["function"])
                if (not streaming or "arguments" in fn) and not isinstance(fn.get("arguments"), str):
                    _fail()
                if streaming:
                    if type(call.get("index")) is not int or call["index"] < 0:
                        _fail()
                elif call.get("type") != "function" or not isinstance(call.get("id"), str) or not isinstance(fn.get("name"), str):
                    _fail()
    return choices


class ProtocolAdapter:
    """Bind provider and explicit public-model -> upstream-model aliases.

    No HTTP, key lookup, config loading, quota mutation or shared stream state.
    Backend may be a mapping or a configuration object with provider/base_url.
    """

    def __init__(self, provider: str, model_aliases: Mapping[str, str]):
        if provider not in {"openai", "deepseek", "claude"}:
            raise AdapterError("unknown_provider", param="provider")
        if not isinstance(model_aliases, Mapping) or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or not v
            for k, v in model_aliases.items()
        ):
            raise AdapterError("invalid_model_aliases", param="model")
        self.provider = provider
        self._aliases = dict(model_aliases)

    def new_state(self, model: str | None = None, *, include_usage: bool = False) -> dict:
        if model is not None and model not in self._aliases:
            raise AdapterError("unknown_model", param="model")
        if type(include_usage) is not bool:
            raise AdapterError("invalid_stream_options")
        return {"model_alias": model, "include_usage": include_usage, "started": False, "terminal": False,
                "usage": None, "blocks": {}, "tool_count": 0,
                "finish_reason": None, "created": int(time.time())}

    def encode_request(self, canonical_body: dict, backend: Any, upstream_key: str) -> dict:
        if not isinstance(canonical_body, dict):
            raise AdapterError("invalid_body")
        body = {k: deepcopy(v) for k, v in canonical_body.items() if v is not None}
        _keys(canonical_body, {"model", "messages", "stream", "max_tokens", "temperature", "top_p",
                     "tools", "tool_choice", "stream_options"}, "body")
        model = body.get("model")
        if not isinstance(model, str) or model not in self._aliases:
            raise AdapterError("unknown_model", param="model")
        get = backend.get if isinstance(backend, Mapping) else lambda k: getattr(backend, k, None)
        if get("provider") != self.provider:
            raise AdapterError("unknown_provider", param="provider")
        base = get("base_url")
        if not isinstance(base, str):
            raise AdapterError("invalid_base_url")
        url = urlsplit(base)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise AdapterError("invalid_base_url")
        path = url.path.rstrip("/")
        if not path.endswith("/v1"):
            path += "/v1"
        path += "/messages" if self.provider == "claude" else "/chat/completions"
        if not isinstance(upstream_key, str) or not upstream_key or any(c in upstream_key for c in "\r\n"):
            raise AdapterError("invalid_upstream_key")
        body["model"] = self._aliases[model]
        self._validate_messages(body)
        opts = body.get("stream_options")
        if opts is not None:
            if not isinstance(opts, dict):
                raise AdapterError("invalid_stream_options")
            _keys(opts, {"include_usage"}, "stream_options")
            if not body.get("stream") or type(opts.get("include_usage", False)) is not bool:
                raise AdapterError("invalid_stream_options")
        headers = {"content-type": "application/json"}
        if self.provider == "claude":
            body = self._claude_request(body)
            headers.update({"x-api-key": upstream_key, "anthropic-version": "2023-06-01"})
        else:
            headers["Authorization"] = f"Bearer {upstream_key}"
        return {"method": "POST", "url": urlunsplit((url.scheme, url.netloc, path, "", "")),
                "headers": headers, "json": body}

    @staticmethod
    def _validate_messages(body: dict) -> None:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise AdapterError("invalid_messages", param="messages")
        for m in messages:
            if not isinstance(m, dict):
                raise AdapterError("invalid_messages", param="messages")
            _keys(m, {"role", "content", "tool_calls", "tool_call_id"}, "messages")
            role = m.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                raise AdapterError("unsupported_role", param="messages")
            content = m.get("content")
            if not isinstance(content, str) and not (role == "assistant" and content is None and m.get("tool_calls")):
                raise AdapterError("unsupported_content", param="messages")
            if "tool_call_id" in m and (role != "tool" or not isinstance(m["tool_call_id"], str) or not m["tool_call_id"]):
                raise AdapterError("invalid_tool_result", param="messages")
            if role == "tool" and not m.get("tool_call_id"):
                raise AdapterError("invalid_tool_result", param="messages")
            if "tool_calls" in m:
                if role != "assistant" or not isinstance(m["tool_calls"], list) or not m["tool_calls"]:
                    raise AdapterError("invalid_tool_calls", param="messages")
                for call in m["tool_calls"]:
                    if not isinstance(call, dict):
                        raise AdapterError("invalid_tool_calls")
                    _keys(call, {"id", "type", "function"}, "tool_calls")
                    fn = call.get("function")
                    if call.get("type") != "function" or not isinstance(call.get("id"), str) or not call["id"] or not isinstance(fn, dict):
                        raise AdapterError("invalid_tool_calls")
                    _keys(fn, {"name", "arguments"}, "tool_calls.function")
                    if not isinstance(fn.get("name"), str) or not fn["name"]:
                        raise AdapterError("invalid_tool_calls")
                    _arguments(fn.get("arguments"))
        if "tools" in body and not isinstance(body["tools"], list):
            raise AdapterError("invalid_tools", param="tools")
        for tool in body.get("tools", []):
            if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
                raise AdapterError("unsupported_tool", param="tools")
            _keys(tool, {"type", "function"}, "tools")
            fn = tool["function"]
            _keys(fn, {"name", "description", "parameters"}, "tools.function")
            if not isinstance(fn.get("name"), str) or not fn["name"] or not isinstance(fn.get("parameters", {}), dict):
                raise AdapterError("invalid_tool", param="tools")
        choice = body.get("tool_choice")
        if choice is not None and choice not in ("auto", "none", "required"):
            if not isinstance(choice, dict):
                raise AdapterError("unsupported_tool_choice")
            _keys(choice, {"type", "function"}, "tool_choice")
            fn = choice.get("function")
            if choice.get("type") != "function" or not isinstance(fn, dict) or set(fn) != {"name"} or not isinstance(fn["name"], str):
                raise AdapterError("unsupported_tool_choice")

    def _claude_request(self, body: dict) -> dict:
        if type(body.get("max_tokens")) is not int or body["max_tokens"] <= 0:
            raise AdapterError("max_tokens_required", param="max_tokens")
        system, messages = [], []
        seen_non_system = False
        for m in body["messages"]:
            role, content = m["role"], m.get("content")
            if role == "system":
                if seen_non_system:
                    raise AdapterError("unsupported_system_position", param="messages")
                system.append({"type": "text", "text": content})
                continue
            seen_non_system = True
            blocks = []
            if role == "tool":
                role = "user"
                blocks.append({"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": content})
            else:
                if content:
                    blocks.append({"type": "text", "text": content})
                for call in m.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": call["id"],
                                   "name": call["function"]["name"],
                                   "input": _arguments(call["function"]["arguments"])})
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": blocks})
        if not messages:
            raise AdapterError("invalid_messages", param="messages")
        result = {k: v for k, v in body.items() if k not in {"messages", "tools", "tool_choice", "stream_options"} and v is not None}
        result["messages"] = messages
        if system:
            result["system"] = system
        tools = body.get("tools")
        choice = body.get("tool_choice")
        if tools and choice != "none":
            result["tools"] = [{"name": t["function"]["name"],
                                "input_schema": t["function"].get("parameters", {"type": "object"}),
                                **({"description": t["function"]["description"]} if "description" in t["function"] else {})} for t in tools]
        if choice is not None and choice != "none":
            if not tools:
                raise AdapterError("tool_choice_requires_tools")
            result["tool_choice"] = {"type": {"auto": "auto", "required": "any"}[choice]} if isinstance(choice, str) else {"type": "tool", "name": choice["function"]["name"]}
        return result

    def decode_response(self, status: int, headers: Mapping, body: Any) -> dict:
        # Status is preserved by the caller. Do not echo upstream error messages/headers.
        if not 200 <= status < 300:
            return AdapterError("upstream_http_error", status=status).payload()
        try:
            raw = deepcopy(_parse(body))
            if "error" in raw:
                return AdapterError("upstream_error", status=502).payload()
            if self.provider != "claude":
                if raw.get("usage") is not None:
                    raw["usage"] = _usage(_object(raw["usage"]))
                _compatible_choices(raw, streaming=False)
                return raw
            text, calls = [], []
            for block in raw["content"]:
                if block["type"] == "text":
                    text.append(block["text"])
                elif block["type"] == "tool_use":
                    calls.append({"id": block["id"], "type": "function", "function": {
                        "name": block["name"], "arguments": json.dumps(_object(block["input"]), ensure_ascii=False, separators=(",", ":"))}})
                else:
                    _fail("unsupported_content_block")
            message = {"role": "assistant", "content": "".join(text) if text else None}
            if calls:
                message["tool_calls"] = calls
            return {"id": raw["id"], "object": "chat.completion", "created": int(time.time()),
                    "model": raw["model"], "choices": [{"index": 0, "message": message,
                    "finish_reason": _stop(raw["stop_reason"])}], "usage": _usage(raw["usage"], True)}
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError("invalid_upstream_payload", status=502) from None

    def decode_event(self, event: str | None, data: Any, state: dict) -> list:
        """Return dict payloads or '[DONE]'; error payload is terminal, never success.

        data is a parsed dict or an SSE data string (framing belongs to transport).
        state['usage'] is cumulative normalized usage or None, for terminal hooks.
        """
        if state["terminal"]:
            return []
        try:
            return self._decode_event(event, data, state)
        except (KeyError, TypeError, ValueError) as exc:
            state["terminal"] = True
            state["outcome"] = "upstream_error"
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError("invalid_upstream_event", status=502) from None

    def _decode_event(self, event: str | None, data: Any, s: dict) -> list:
        if data == "[DONE]":
            if self.provider == "claude" or not s.get("finished"):
                _fail("premature_stream_end")
            s.update(terminal=True, outcome="success")
            return ["[DONE]"]
        raw = _parse(data)
        if event == "error" or "error" in raw:
            s.update(terminal=True, outcome="upstream_error")
            return [AdapterError("upstream_stream_error", status=502).payload()]
        if self.provider != "claude":
            choices = _compatible_choices(raw, streaming=True)
            if s.get("finished") and choices:
                _fail("content_after_finish")
            if raw.get("usage") is not None:
                s["usage"] = _usage(raw["usage"])
                raw = {**raw, "usage": s["usage"]}
            if any(c.get("finish_reason") is not None for c in choices):
                s["finished"] = True
            if s.get("model_alias"):
                raw = {**raw, "model": s["model_alias"]}
            return [deepcopy(raw)]
        kind = event or raw.get("type")
        if raw.get("type") != kind:
            _fail("event_type_mismatch")
        if kind == "ping":
            return []
        known = {"message_start", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"}
        if kind not in known:
            return []  # Forward-compatible unknown event names, not unknown content.
        if kind == "message_start":
            if s["started"]:
                _fail("duplicate_message_start")
            msg = raw["message"]
            s.update(started=True, id=msg["id"], model=s["model_alias"] or msg["model"], raw_usage=deepcopy(msg["usage"]))
            s["usage"] = _usage(s["raw_usage"], True)
            return [self._chunk(s, {"role": "assistant", "content": ""})]
        if not s["started"] or s.get("finished"):
            _fail("invalid_event_order")
        if kind == "content_block_start":
            index, block = raw["index"], raw["content_block"]
            if type(index) is not int or index < 0 or index in s["blocks"] or s["finish_reason"]:
                _fail("invalid_content_block")
            record = {"type": block["type"], "closed": False}
            s["blocks"][index] = record
            if block["type"] == "text":
                return [self._chunk(s, {"content": block["text"]})] if block["text"] else []
            if block["type"] != "tool_use":
                _fail("unsupported_content_block")
            record["tool_index"] = s["tool_count"]
            s["tool_count"] += 1
            initial = _object(block["input"])
            record["has_initial"] = bool(initial)
            return [self._chunk(s, {"tool_calls": [{"index": record["tool_index"], "id": block["id"], "type": "function", "function": {"name": block["name"], "arguments": json.dumps(initial, ensure_ascii=False, separators=(",", ":")) if initial else ""}}]})]
        if kind in {"content_block_delta", "content_block_stop"}:
            block = s["blocks"][raw["index"]]
            if block["closed"]:
                _fail("invalid_event_order")
            if kind == "content_block_stop":
                block["closed"] = True
                if block["type"] == "tool_use" and not block.get("has_delta") and not block["has_initial"]:
                    return [self._chunk(s, {"tool_calls": [{"index": block["tool_index"], "function": {"arguments": "{}"}}]})]
                return []
            delta = raw["delta"]
            if delta["type"] == "text_delta" and block["type"] == "text" and isinstance(delta.get("text"), str):
                return [self._chunk(s, {"content": delta["text"]})]
            if delta["type"] == "input_json_delta" and block["type"] == "tool_use" and not block["has_initial"] and isinstance(delta.get("partial_json"), str):
                block["has_delta"] = True
                return [self._chunk(s, {"tool_calls": [{"index": block["tool_index"], "function": {"arguments": delta["partial_json"]}}]})]
            _fail("unsupported_content_delta")
        if kind == "message_delta":
            if any(not b["closed"] for b in s["blocks"].values()):
                _fail("invalid_event_order")
            s["raw_usage"].update(raw.get("usage", {}))
            s["usage"] = _usage(s["raw_usage"], True)
            reason = raw.get("delta", {}).get("stop_reason")
            if reason:
                if s["finish_reason"]:
                    _fail("duplicate_stop_reason")
                s["finish_reason"] = _stop(reason)
            return []
        if not s["finish_reason"] or any(not b["closed"] for b in s["blocks"].values()):
            _fail("premature_stream_end")
        s.update(terminal=True, outcome="success")
        result = [self._chunk(s, {}, s["finish_reason"])]
        if s["include_usage"]:
            result.append({**self._chunk(s, {}), "choices": [], "usage": deepcopy(s["usage"])})
        return result + ["[DONE]"]

    @staticmethod
    def _chunk(s: dict, delta: dict, finish: str | None = None) -> dict:
        return {"id": s["id"], "object": "chat.completion.chunk", "created": s["created"],
                "model": s["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
