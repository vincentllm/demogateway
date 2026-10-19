from __future__ import annotations

import asyncio
import codecs
import inspect
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

import httpx


class TransportError(Exception):
    """Safe pre-commit error; never includes upstream text, URLs or credentials."""

    def __init__(self, status_code: int = 502, code: str = "upstream_error"):
        self.status_code = status_code
        self.code = code
        self.body = {"error": {"message": "Upstream request failed", "type": code,
                               "param": None, "code": code}}
        super().__init__(code)


async def parse_sse(chunks: AsyncIterator[bytes], *, max_event_bytes: int = 1048576):
    """Yield (event, data), honoring UTF-8, CR/LF/CRLF and multiline data.

    EOF does not dispatch an unterminated event. Event size is bounded, including
    ignored fields and comments, so an upstream cannot grow the buffer forever.
    """
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    line: list[str] = []
    data: list[str] = []
    event = "message"
    skip_lf = False
    size = 0
    async for chunk in chunks:
        for char in decoder.decode(chunk):
            if skip_lf:
                skip_lf = False
                if char == "\n":
                    continue
            size += len(char.encode("utf-8"))
            if size > max_event_bytes:
                raise TransportError(code="invalid_stream")
            if char not in "\r\n":
                line.append(char)
                continue
            skip_lf = char == "\r"
            value = "".join(line)
            line.clear()
            if not value:
                if data:
                    yield event, "\n".join(data)
                data.clear()
                event = "message"
                size = 0
            elif not value.startswith(":"):
                field, sep, content = value.partition(":")
                if content.startswith(" "):
                    content = content[1:]
                if field == "data":
                    data.append(content if sep else "")
                elif field == "event":
                    event = content or "message"
    decoder.decode(b"", final=True)


def _usage(value):
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    if isinstance(value, dict) and all(type(value.get(k)) is int and value[k] >= 0 for k in keys):
        if value["total_tokens"] == value["prompt_tokens"] + value["completion_tokens"]:
            return {k: value[k] for k in keys}
    return None


def _frame(payload: Any) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")) + "\n\n").encode()


class StreamingTransport:
    def __init__(self, client: httpx.AsyncClient, *, timeout: httpx.Timeout | float = 30.0,
                 max_retries: int = 0, max_event_bytes: int = 1048576):
        if type(max_retries) is not int or not 0 <= max_retries <= 3:
            raise ValueError("max_retries must be an integer between 0 and 3")
        if max_event_bytes <= 0:
            raise ValueError("max_event_bytes must be positive")
        self.client = client
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_event_bytes = max_event_bytes

    @asynccontextmanager
    async def stream(self, encoded: dict, decode_event: Callable,
                     on_terminal: Callable) -> AsyncIterator[AsyncIterator[bytes]]:
        """Open/prefetch before headers; own this context until downstream closes.

        decode_event(event, data, state) returns a list of dict chunks or [DONE].
        It may instead set state['done']=True after its final event. It must raise
        on provider errors (or return an error dict). Cumulative canonical usage
        is read from state['usage'] or emitted chunk['usage'].
        """
        response = None
        payloads = None
        outcome = "cancelled"
        usage = None

        async def decoded():
            nonlocal usage
            state: dict[str, Any] = {}
            async for event, data in parse_sse(response.aiter_bytes(), max_event_bytes=self.max_event_bytes):
                if event == "error":
                    raise TransportError()
                items = decode_event(event, data, state)
                if not isinstance(items, list):
                    raise TransportError(code="invalid_stream")
                if _usage(state.get("usage")) is not None:
                    usage = _usage(state["usage"])
                done = False
                for item in items:
                    if item == "[DONE]":
                        done = True
                        continue
                    if done or not isinstance(item, dict) or "error" in item:
                        raise TransportError(code="invalid_stream")
                    if _usage(item.get("usage")) is not None:
                        usage = _usage(item["usage"])
                    yield _frame(item)
                if done or state.get("done") is True:
                    return
            raise TransportError(code="incomplete_stream")

        async def close_response():
            if response is not None:
                await response.aclose()

        async def body(first):
            nonlocal outcome
            try:
                if first is not None:
                    yield first
                    async for chunk in payloads:
                        yield chunk
                outcome = "success"
                yield b"data: [DONE]\n\n"
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except GeneratorExit:
                outcome = "cancelled"
                raise
            except Exception as exc:
                outcome = "timeout" if isinstance(exc, httpx.TimeoutException) else "upstream_error"
                error = TransportError(code=outcome)
                yield b"event: error\n" + _frame(error.body)

        iterator = None
        try:
            for attempt in range(self.max_retries + 1):
                try:
                    usage = None
                    request = self.client.build_request(encoded["method"], encoded["url"],
                                                        headers=encoded["headers"], json=encoded["json"],
                                                        timeout=self.timeout)
                    response = await self.client.send(request, stream=True, follow_redirects=False)
                    if not 200 <= response.status_code < 300:
                        raise TransportError(response.status_code)
                    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                        raise TransportError(code="invalid_stream")
                    payloads = decoded()
                    first = await anext(payloads, None)
                    break
                except (httpx.TransportError, TransportError) as exc:
                    await close_response()
                    retryable = isinstance(exc, httpx.TransportError) or (
                        isinstance(exc, TransportError) and response is not None
                        and response.status_code in (429, 502, 503, 504)
                        and exc.code == "upstream_error")
                    if attempt == self.max_retries or not retryable:
                        raise
            iterator = body(first)
            yield iterator
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except GeneratorExit:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome = "timeout" if isinstance(exc, httpx.TimeoutException) else "upstream_error"
            if isinstance(exc, TransportError):
                raise
            raise TransportError(504 if outcome == "timeout" else 502, outcome) from None
        finally:
            async def cleanup():
                try:
                    if iterator is not None:
                        await iterator.aclose()
                    if payloads is not None:
                        await payloads.aclose()
                finally:
                    try:
                        await close_response()
                    finally:
                        result = on_terminal(usage, outcome)
                        if inspect.isawaitable(result):
                            await result

            # Cleanup must finish even if downstream cancellation interrupts it.
            task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
