"""OpenAI Responses 形状的适配器（2026-11-22）。

为什么加：本机 15721 是 CC Switch 的本地代理，它只讲 `/v1/responses`（chat/completions 回 400）。
鲸鲸原来的回退链是 deepseek → openai（没 key）→ ollama（缺 llama-server），主路一抖就没有
第二条腿。这个适配器让那台代理能当回退。

职责边界与 openai_compat 相同：协议字段只活在本文件，core/ 与 agents/ 看不到。
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, AsyncIterator

import structlog
from openai import AsyncOpenAI

from core.config.settings import config as _app_config
from llm.adapters.base import (
    Capabilities,
    ChatResult,
    ProviderError,
    StreamChunk,
    ToolsResult,
    Usage,
)
from llm.adapters.openai_compat import map_sdk_error

if TYPE_CHECKING:
    from llm.config import EndpointSpec

log = structlog.get_logger()


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if isinstance(part, dict):
            parts.append(str(part.get("text") or ""))
    return "".join(parts)


def _usage_from_responses(usage: Any) -> Usage:
    """Responses 回包给的是 input_tokens/output_tokens，chat 那边叫 prompt/completion。"""
    if usage is None:
        return Usage()
    prompt = getattr(usage, "input_tokens", None)
    completion = getattr(usage, "output_tokens", None)
    total = getattr(usage, "total_tokens", None)
    if total is None and isinstance(prompt, int) and isinstance(completion, int):
        total = prompt + completion
    details = getattr(usage, "input_tokens_details", None)
    cache_hit = getattr(details, "cached_tokens", None) if details is not None else None
    cache_miss = None
    if isinstance(prompt, int) and isinstance(cache_hit, int):
        cache_miss = max(prompt - cache_hit, 0)
    return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
                 cache_hit_tokens=cache_hit, cache_miss_tokens=cache_miss)


def _input_parts(content: Any) -> list[dict]:
    """user/system 的内容既可能是字符串，也可能是带 image_url 的 chat parts。"""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    parts: list[dict] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            parts.append({"type": "input_image", "image_url": str(url or "")})
        else:
            parts.append({"type": "input_text", "text": str(part.get("text") or "")})
    return parts


def to_responses_input(messages: list[dict]) -> list[dict]:
    """把 chat 形状的 messages 翻成 Responses 的 input items。"""
    items: list[dict] = []
    for message in messages or []:
        role = str(message.get("role") or "user")
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or ""),
                "output": _text_of(message.get("content")),
            })
            continue
        text = _text_of(message.get("content"))
        if role == "assistant":
            if text:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": text}]})
            for call in message.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                fn = fn if isinstance(fn, dict) else {}
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args or {}, ensure_ascii=False)
                items.append({"type": "function_call",
                              "name": str(fn.get("name") or ""),
                              "arguments": args,
                              "call_id": str(call.get("id") or "")})
            continue
        items.append({"type": "message", "role": role,
                      "content": _input_parts(message.get("content"))})
    return items


def to_responses_tools(tools: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            out.append(tool)
            continue
        out.append({
            "type": "function",
            "name": str(fn.get("name") or ""),
            "description": str(fn.get("description") or ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def to_responses_tool_choice(tool_choice: str | dict | None) -> str | dict:
    """强制工具时项目传 chat 形状 {"type":"function","function":{"name":...}}。"""
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return {"type": "function", "name": str(fn["name"])}
    return tool_choice if tool_choice is not None else "auto"


def parse_responses_output(resp: Any) -> tuple[str, list[dict]]:
    """从 Responses 回包里取出正文与函数调用。"""
    texts: list[str] = []
    calls: list[dict] = []
    for item in getattr(resp, "output", None) or []:
        kind = str(getattr(item, "type", "") or "")
        if kind == "message":
            for part in getattr(item, "content", None) or []:
                if str(getattr(part, "type", "") or "") == "output_text":
                    texts.append(str(getattr(part, "text", "") or ""))
        elif kind == "function_call":
            calls.append({
                "id": str(getattr(item, "call_id", "") or getattr(item, "id", "") or ""),
                "name": str(getattr(item, "name", "") or ""),
                "arguments": str(getattr(item, "arguments", "") or "{}"),
            })
    return "".join(texts), calls


class OpenAIResponsesAdapter:
    """`/v1/responses` 形状的端点（CC Switch 本地代理）。"""

    name = "openai_responses"
    KIND = "openai_responses"
    ADAPTER_CONTRACT_VERSION = 1

    def __init__(self, spec: "EndpointSpec", *, settings=None, client_factory=None) -> None:
        self.spec = spec
        self._settings = settings
        self._client_factory = client_factory
        self._client = None

    def capabilities(self, model: str) -> Capabilities:
        return self.spec.capabilities

    def _timeout(self) -> float:
        if self.spec.timeout is not None:
            return float(self.spec.timeout)
        settings = self._settings if self._settings is not None else _app_config.llm
        return float(getattr(settings, "timeout_seconds", 30) or 30)

    def _api_key(self) -> str:
        if self.spec.api_key:
            return str(self.spec.api_key)
        value = ""
        if self.spec.api_key_env:
            value = str(os.environ.get(self.spec.api_key_env, "") or "")
        return value or "local-proxy"

    def _get_client(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                self._client = AsyncOpenAI(api_key=self._api_key(), base_url=self.spec.base_url)
        return self._client

    def refresh_credentials(self) -> None:
        self._client = None

    async def _create(self, **kwargs):
        client = self._get_client()
        try:
            return await client.responses.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise map_sdk_error(exc, provider=self.spec.name) from exc

    def _meta(self, resp, messages) -> dict[str, Any]:
        return {"id": str(getattr(resp, "id", "") or ""),
                "model": str(getattr(resp, "model", "") or ""),
                "request_messages": len(messages or [])}

    async def chat(self, *, model: str, messages: list[dict], temperature: float,
                   max_tokens: int, reasoning_effort: str | None = None,
                   thinking_budget: int | None = None,
                   timeout: float | None = None) -> ChatResult:
        resp = await self._create(
            model=model, input=to_responses_input(messages),
            temperature=temperature, max_output_tokens=int(max_tokens or 1200),
            timeout=self._timeout() if timeout is None else timeout,
        )
        text, _calls = parse_responses_output(resp)
        return ChatResult(text=text, usage=_usage_from_responses(getattr(resp, "usage", None)),
                          finish_reason=str(getattr(resp, "status", "") or "stop"),
                          raw_meta=self._meta(resp, messages))

    async def chat_tools(self, *, model: str, messages: list[dict], tools: list[dict],
                         tool_choice: str | dict = "auto", temperature: float = 0.1,
                         max_tokens: int = 1200, reasoning_effort: str | None = None,
                         thinking_budget: int | None = None,
                         timeout: float | None = None) -> ToolsResult:
        resp = await self._create(
            model=model, input=to_responses_input(messages),
            tools=to_responses_tools(tools),
            tool_choice=to_responses_tool_choice(tool_choice),
            temperature=temperature, max_output_tokens=int(max_tokens or 1200),
            timeout=self._timeout() if timeout is None else timeout,
        )
        text, calls = parse_responses_output(resp)
        return ToolsResult(
            text=text, tool_calls=tuple(calls),
            usage=_usage_from_responses(getattr(resp, "usage", None)),
            finish_reason="tool_calls" if calls else "stop",
            raw_meta=self._meta(resp, messages),
        )

    async def chat_vision(self, *, model: str, messages: list[dict], images: list[str],
                          temperature: float = 0.3, max_tokens: int = 2000,
                          timeout: float | None = None) -> ChatResult:
        from llm.adapters.openai_compat import messages_with_images

        request_messages = messages_with_images(messages, images)
        resp = await self._create(
            model=model, input=to_responses_input(request_messages),
            temperature=temperature, max_output_tokens=int(max_tokens or 2000),
            timeout=self._timeout() if timeout is None else timeout,
        )
        text, _calls = parse_responses_output(resp)
        return ChatResult(text=text, usage=_usage_from_responses(getattr(resp, "usage", None)),
                          finish_reason=str(getattr(resp, "status", "") or "stop"),
                          raw_meta=self._meta(resp, request_messages))

    async def stream(self, *, model: str, messages: list[dict], temperature: float = 0.7,
                     max_tokens: int = 800, reasoning_effort: str | None = None,
                     thinking_budget: int | None = None,
                     timeout: float | None = None) -> AsyncIterator[StreamChunk]:
        result = await self.chat(model=model, messages=messages, temperature=temperature,
                                 max_tokens=max_tokens, timeout=timeout)
        yield StreamChunk(text=result.text, usage=result.usage, finish_reason="stop")

    async def list_models(self) -> list[dict]:
        client = self._get_client()
        resp = await client.models.list()
        return [{"id": str(getattr(item, "id", "") or "")} for item in getattr(resp, "data", None) or []]
