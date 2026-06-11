from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


class FallbackResponseBuilder:
    """Builds OpenAI-compatible fallback responses when all backends fail."""

    def __init__(self, message: str) -> None:
        self.message = message

    def build(self, request_body: dict[str, Any]) -> JSONResponse:
        model = request_body.get("model", "fallback")
        payload = {
            "id": "fallback-response",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": self.message,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        return JSONResponse(content=payload, status_code=200)
