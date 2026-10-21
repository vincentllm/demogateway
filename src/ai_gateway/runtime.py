"""Authenticated canonical API integrating protocol, quota and transport lifecycles."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

from ai_gateway.adapters import AdapterError, ProtocolAdapter
from ai_gateway.auth import AuthConfig, AuthError, AuthManager, UpstreamKeyUnavailable
from ai_gateway.quota import MemoryQuotaStore, QuotaLimits, QuotaManager, QuotaRejected, QuotaUnavailable, RedisQuotaStore
from ai_gateway.transport import StreamingTransport, TransportError


def checked_usage(usage):
    if usage is not None:
        if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or not 0 <= usage[k] <= 2**53 - 1 for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
            raise AdapterError("invalid_upstream_usage", status=502)
        if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
            raise AdapterError("invalid_upstream_usage", status=502)
    return usage


def error(code, status=400, headers=None):
    return JSONResponse({"error": {"message": code, "type": "server_error" if status >= 500 else "invalid_request_error", "param": None, "code": code}}, status_code=status, headers=headers)


async def protected(awaitable):
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class GatewayStream(Response):
    """The transport context spans ASGI sends, including initial prefetch.

    A single receive consumer watches disconnect from before the upstream opens.
    This avoids StreamingResponse's late disconnect listener during prefetch.
    """
    media_type = "text/event-stream"

    def __init__(self, transport, encoded, decoder, terminal):
        super().__init__(content=b"", media_type=self.media_type)
        self.transport, self.encoded, self.decoder, self.terminal = transport, encoded, decoder, terminal

    async def __call__(self, scope, receive, send):
        committed = False

        async def safe_send(message):
            try:
                await send(message)
            except OSError:
                raise asyncio.CancelledError() from None

        async def run():
            nonlocal committed
            try:
                pending_done = None
                async with self.transport.stream(self.encoded, self.decoder, self.terminal) as chunks:
                    await safe_send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream"), (b"cache-control", b"no-cache"), (b"x-accel-buffering", b"no")]})
                    committed = True
                    async for chunk in chunks:
                        if chunk == b"data: [DONE]\n\n":
                            pending_done = chunk
                        else:
                            await safe_send({"type": "http.response.body", "body": chunk, "more_body": True})
                # Success is committed only once quota settlement also completed.
                if pending_done is not None:
                    await safe_send({"type": "http.response.body", "body": pending_done, "more_body": True})
                await safe_send({"type": "http.response.body", "body": b"", "more_body": False})
            except TransportError as exc:
                if not committed:
                    await JSONResponse(exc.body, status_code=exc.status_code if 400 <= exc.status_code <= 599 else 502)(scope, receive, safe_send)
            except QuotaRejected:
                if committed:
                    await safe_send({"type": "http.response.body", "body": b'event: error\ndata: {"error":{"code":"quota_unavailable","message":"Quota settlement unavailable"}}\n\n', "more_body": False})
                else:
                    await error("quota_unavailable", 503)(scope, receive, safe_send)
            except OSError:
                # An ASGI send failure is a disconnected client.
                raise asyncio.CancelledError() from None

        async def watch():
            while True:
                if (await receive())["type"] == "http.disconnect":
                    return

        worker, disconnect = asyncio.create_task(run()), asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait({worker, disconnect}, return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done and not worker.done():
                worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        finally:
            for task in (worker, disconnect):
                if not task.done():
                    task.cancel()
            for task in (worker, disconnect):
                with suppress(asyncio.CancelledError):
                    await task


def create_platform_app(config, http_client=None):
    from ai_gateway.observability import Observability

    cfg = config.platform
    auth = AuthManager(AuthConfig.from_mapping(cfg.auth))
    redis_client = None
    if cfg.redis_url:
        import redis.asyncio as redis
        redis_client = redis.from_url(cfg.redis_url.get_secret_value(), decode_responses=True, protocol=2,
                                     socket_connect_timeout=cfg.timeout, socket_timeout=cfg.timeout)
        store = RedisQuotaStore(redis_client, key_prefix=cfg.quota_namespace)
    else:
        store = MemoryQuotaStore()
    quota = QuotaManager({k: QuotaLimits(**v.model_dump()) for k, v in cfg.quotas.items()}, store)
    owned_client = http_client is None
    client = http_client if http_client is not None else httpx.AsyncClient(
        timeout=cfg.timeout, follow_redirects=False, trust_env=False,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
    obs = Observability(configured_routes=[(alias, route.provider) for alias, route in cfg.routes.items()])
    # Dedicated JSON sink; never forward request bodies, URLs, keys or exceptions.
    obs.logger.propagate = False
    obs.logger.setLevel(logging.INFO)
    if not obs.logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        obs.logger.addHandler(handler)

    @asynccontextmanager
    async def lifespan(app):
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            if owned_client:
                await client.aclose()
            if redis_client is not None:
                await redis_client.aclose()

    app = FastAPI(title="LLM Gateway", lifespan=lifespan)
    app.state.ready = True
    app.state.config, app.state.auth, app.state.quota = config, auth, quota
    app.state.observability, app.state.http_client = obs, client

    @app.get("/health")
    async def health():
        ready = app.state.ready
        if ready and redis_client is not None:
            try:
                ready = bool(await redis_client.ping())
            except Exception:
                ready = False
        return JSONResponse({"status": "ok" if ready else "unavailable", "mode": "platform"}, status_code=200 if ready else 503)

    @app.get("/metrics")
    async def metrics():
        return Response(obs.render_metrics(), headers={"content-type": obs.metrics_content_type})

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        request_id = uuid.uuid4().hex
        handle = None
        lease = None
        delegated = False
        dispatched = False
        outcome = "client_error"
        usage = None
        try:
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > cfg.max_request_bytes:
                    return error("request_too_large", 413)
                chunks.append(chunk)
            try:
                body = json.loads(b"".join(chunks), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                return error("invalid_json")
            if not isinstance(body, dict) or not isinstance(body.get("model"), str):
                return error("invalid_request")
            model = body["model"]
            route = cfg.routes.get(model)
            streaming = body.get("stream", False)
            handle = obs.start(request_id, "unknown", {"route": model if route else "unknown", "provider": route.provider if route else "unknown"}, streaming is True)
            headers = request.headers.getlist("authorization")
            identity = auth.authenticate(headers[0] if len(headers) == 1 else None, model)
            if route is None:
                return error("model_not_found", 404)
            if type(streaming) is not bool:
                return error("invalid_stream")
            output = body.get("max_tokens", cfg.default_max_tokens)
            if type(output) is not int or not 1 <= output <= cfg.max_output_tokens:
                return error("invalid_max_tokens")
            for name, upper in (("temperature", 2), ("top_p", 1)):
                if name in body and (type(body[name]) not in (int, float) or not 0 <= body[name] <= upper or (type(body[name]) is float and not math.isfinite(body[name]))):
                    return error("invalid_" + name)
            body["max_tokens"] = output
            body["stream"] = streaming
            adapter = ProtocolAdapter(route.provider, {model: route.upstream_model})
            upstream_key = auth.resolve_upstream_key(route.key_ref)
            try:
                encoded = adapter.encode_request(body, {"provider": route.provider, "base_url": route.base_url}, upstream_key)
            except (TypeError, OverflowError):
                # Malformed nested JSON must not escape the public validation
                # boundary. Keep this catch before admission and dispatch only.
                return error("invalid_request")
            lease = await quota.admit(identity.client_id, request_id, route.input_token_bound, output)

            if streaming:
                initial = adapter.new_state(model, include_usage=bool((body.get("stream_options") or {}).get("include_usage", False)))

                def decode(event, data, state):
                    if not state:
                        state.update(initial)
                    result = adapter.decode_event(event, data, state)
                    checked_usage(state.get("usage"))
                    for item in result:
                        if isinstance(item, dict):
                            for choice in item.get("choices", []):
                                delta = choice.get("delta", {})
                                if delta.get("content"):
                                    obs.first_token(handle)
                                elif delta.get("tool_calls"):
                                    obs.first_token(handle, "tool_calls")
                    return result

                async def terminal(actual, terminal_outcome):
                    final_outcome = terminal_outcome
                    try:
                        if terminal_outcome == "success":
                            await quota.settle(lease, actual)
                        else:
                            await quota.release(lease, terminal_outcome if terminal_outcome in {"cancelled", "timeout", "upstream_error"} else "unknown")
                    except (QuotaRejected, ValueError):
                        final_outcome = "error"
                        raise QuotaUnavailable() from None
                    finally:
                        obs.finish(handle, final_outcome, usage=actual if terminal_outcome == "success" else None)

                delegated = True
                return GatewayStream(StreamingTransport(client, timeout=cfg.timeout, max_retries=0, max_event_bytes=cfg.max_response_bytes), encoded, decode, terminal)

            dispatched = True
            async with client.stream(encoded["method"], encoded["url"], headers=encoded["headers"], json=encoded["json"], timeout=cfg.timeout, follow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    outcome = "upstream_error"
                    return error("upstream_error", response.status_code if 400 <= response.status_code <= 599 else 502)
                data, count = [], 0
                async for part in response.aiter_bytes():
                    count += len(part)
                    if count > cfg.max_response_bytes:
                        raise AdapterError("response_too_large", status=502)
                    data.append(part)
                result = adapter.decode_response(response.status_code, response.headers, b"".join(data))
            if "error" in result:
                raise AdapterError("upstream_error", status=502)
            result["model"] = model
            usage = checked_usage(result.get("usage"))
            await quota.settle(lease, usage)
            lease = None
            outcome = "success"
            return JSONResponse(result)
        except AuthError as exc:
            return exc.to_response()
        except UpstreamKeyUnavailable as exc:
            outcome = "error"
            return exc.to_response()
        except QuotaRejected as exc:
            outcome = "error" if isinstance(exc, QuotaUnavailable) else "rate_limited"
            return error("quota_unavailable" if isinstance(exc, QuotaUnavailable) else "quota_exceeded", 503 if isinstance(exc, QuotaUnavailable) else 429,
                         {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None)
        except AdapterError as exc:
            outcome = "upstream_error" if exc.status >= 500 else "client_error"
            return JSONResponse(exc.payload(), status_code=exc.status)
        except ClientDisconnect:
            outcome = "cancelled"
            raise asyncio.CancelledError() from None
        except RecursionError:
            outcome = "upstream_error" if dispatched else "client_error"
            return error("invalid_upstream_payload" if dispatched else "invalid_request", 502 if dispatched else 400)
        except ValueError:
            outcome = "upstream_error"
            return error("invalid_upstream_payload", 502)
        except httpx.TimeoutException:
            outcome = "timeout"
            return error("upstream_timeout", 504)
        except httpx.HTTPError:
            outcome = "upstream_error"
            return error("upstream_error", 502)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            if not delegated:
                async def cleanup():
                    try:
                        if lease is not None:
                            await quota.release(lease, "unknown" if dispatched else "not_sent")
                    except QuotaRejected:
                        pass  # Reservation remains charged, never re-dispatched.
                    finally:
                        if handle is None:
                            missing = obs.start(request_id, "unknown", {}, False)
                            obs.finish(missing, outcome)
                        else:
                            obs.finish(handle, outcome, usage=usage if outcome == "success" else None)
                await protected(cleanup())

    return app
