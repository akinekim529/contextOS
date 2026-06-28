"""The one abstraction every inference backend hides behind.

ContextOS forwards an assembled prompt; it does not run models. vLLM, TGI, Ollama, and the
OpenAI API all sit behind this protocol, so the rest of the system is backend-agnostic and a
backend swap is a config change, not a code change.

C8 (client abort): commit write-back only when a server-side terminal event (``finish_reason``)
is observed. A client disconnect before that terminal event discards the partial generation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(BaseModel):
    role: Role
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    max_tokens: int = 512
    temperature: float = 0.7
    stream: bool = False
    extra: dict[str, str] = Field(default_factory=dict)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class ChatResponse(BaseModel):
    text: str
    model: str
    finish_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)


class StreamEventType(str, Enum):
    TOKEN = "token"  # noqa: S105 - SSE event name, not a credential

    USAGE = "usage"
    DONE = "done"
    ERROR = "error"


class StreamEvent(BaseModel):
    type: StreamEventType
    data: str = ""
    finish_reason: str | None = None


class Capabilities(BaseModel):
    streaming: bool = True
    max_context_tokens: int = 8192
    supports_system_prompt: bool = True
    prefix_cache: bool = False  # vLLM sets this True — drives stable-prefix prompt construction


@runtime_checkable
class BackendAdapter(Protocol):
    name: str

    def capabilities(self) -> Capabilities: ...

    async def health_check(self) -> bool: ...

    async def generate(self, req: ChatRequest) -> ChatResponse: ...

    def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]: ...
