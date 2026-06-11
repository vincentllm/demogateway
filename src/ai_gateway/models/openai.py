from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def model_dump_for_provider(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"extra"})
        data.update(self.extra)
        return data
