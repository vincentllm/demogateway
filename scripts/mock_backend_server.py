#!/usr/bin/env python3
"""A tiny standalone mock LLM backend server.

This server exposes ``POST /v1/chat/completions`` and uses
``MockLLMBackendSender`` to produce OpenAI-compatible responses. It is meant
for local demos and manual testing of the gateway without needing a real API
key.

Usage:
    python scripts/mock_backend_server.py --port 9999
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ai_gateway.testing import MockLLMBackendSender, SenderBehavior


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.sender = MockLLMBackendSender(SenderBehavior(latency_ms=100))
    yield


app = FastAPI(title="Mock LLM Backend", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin/request-log")
async def request_log(request: Request) -> dict[str, Any]:
    sender: MockLLMBackendSender = request.app.state.sender
    entries = [
        {
            "method": ctx.method,
            "url": str(ctx.url),
            "body": ctx.parsed_body,
        }
        for ctx in sender.request_log
    ]
    return {"requests": entries}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    sender: MockLLMBackendSender = request.app.state.sender
    import httpx

    mock_request = httpx.Request(
        "POST",
        str(request.url),
        headers=dict(request.headers),
        content=await request.body(),
    )
    response = await sender.send(mock_request)

    if response.headers.get("content-type") == "text/event-stream":
        # The httpx response may have its stream already consumed; stream the
        # buffered content in that case to avoid StreamConsumed errors.
        if response.is_stream_consumed:
            body_iter = iter([response.content])
        else:
            body_iter = response.aiter_raw()
        return StreamingResponse(
            content=body_iter,
            status_code=response.status_code,
            headers={"content-type": "text/event-stream"},
        )

    return JSONResponse(
        status_code=response.status_code,
        content=response.json(),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
