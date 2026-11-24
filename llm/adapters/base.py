"""Adapter contract vocabulary (pre-landed in WP1; WP2 implements adapters).

Copied from the 施工单 §4.1. Shared by ``llm/config.py`` (WP1), the Router
(WP3) and every provider adapter. Do not invent extra fields here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol

ErrorCode = Literal["auth", "rate_limit", "timeout", "context_overflow",
                    "tool_unsupported", "bad_response", "provider_down", "budget"]


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    #: provider 只给 total 时用这个；三者都缺 → None（不许估算）
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None

    def resolved_total(self) -> int | None:
        if self.total_tokens is not None:
            return self.total_tokens
        parts = [p for p in (self.prompt_tokens, self.completion_tokens) if p is not None]
        return sum(parts) if parts else None


@dataclass(frozen=True)
class ReasoningBlock:
    type: Literal["reasoning"] = "reasoning"
    text: str = ""
    #: Anthropic thinking 签名 / OpenAI 加密推理，按端点原样回放（见 WP7）
    signature: str | None = None
    encrypted: str | None = None


@dataclass(frozen=True)
class Capabilities:
    tools: bool = False
    vision: bool = False
    json_mode: bool = False
    streaming: bool = False
    reasoning: bool = False
    context_window: int | None = None
    #: 是否支持 stream_options={"include_usage": true}（拿不到 usage 的端点置 False）
    stream_usage: bool = False
@dataclass(frozen=True)
class ChatResult:
    text: str
    usage: Usage = Usage()
    finish_reason: str = "stop"
    reasoning_blocks: tuple[ReasoningBlock, ...] = ()
    raw_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolsResult(ChatResult):
    tool_calls: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class StreamChunk:
    text: str = ""
    usage: Usage | None = None        # 最后一块带；拿不到 → None
    finish_reason: str | None = None


class ProviderError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, provider: str = "", status: int | None = None):
        super().__init__(message)
        self.code, self.provider, self.status = code, provider, status


class BaseAdapter(Protocol):
    name: str

    def capabilities(self, model: str) -> Capabilities: ...

    async def chat(self, *, model: str, messages: list[dict], temperature: float, max_tokens: int,
                   reasoning_effort: str | None = None, thinking_budget: int | None = None,
                   timeout: float | None = None) -> ChatResult: ...

    async def chat_tools(self, *, model: str, messages: list[dict], tools: list[dict],
                         tool_choice: str | dict = "auto", temperature: float = 0.1,
                         max_tokens: int = 1200,
                         reasoning_effort: str | None = None, thinking_budget: int | None = None,
                         timeout: float | None = None) -> ToolsResult: ...

    async def chat_vision(self, *, model: str, messages: list[dict], images: list[str],
                          temperature: float = 0.3, max_tokens: int = 2000,
                          timeout: float | None = None) -> ChatResult: ...

    def stream(self, *, model: str, messages: list[dict], temperature: float = 0.7,
               max_tokens: int = 800,
               reasoning_effort: str | None = None, thinking_budget: int | None = None,
               timeout: float | None = None) -> AsyncIterator[StreamChunk]: ...

    async def list_models(self) -> list[dict]: ...
