"""Anthropic Messages API adapter (WP7).

Raw httpx (no anthropic SDK).  Protocol fields stay here: top-level ``system``,
content blocks, ``max_tokens`` required, ``x-api-key`` + ``anthropic-version``,
SSE -> StreamChunk, thinking/replay/cache_control.

Transport seam: ``transport.send(payload, stream=False) -> dict`` and
``transport.send(payload, stream=True) -> AsyncIterator[event dict]``.  Tests
inject a fake transport; production uses ``AnthropicHTTPTransport``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx
import structlog

from llm.adapters.base import (
    Capabilities,
    ChatResult,
    ProviderError,
    ReasoningBlock,
    StreamChunk,
    ToolsResult,
    Usage,
)
from llm.adapters.openai_compat import bare_base64_data_uri, request_digest, tool_schema_digest
from safelog import warn

if TYPE_CHECKING:
    from llm.config import EndpointSpec

log = structlog.get_logger()

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 2000
MIN_THINKING_BUDGET = 1024
EFFORT_BUDGETS = {"low": 1024, "medium": 4096, "high": 16384}
REPLAY_MODES = ("encrypted", "plaintext")
_FINISH_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "stop",
}
_CONTEXT_HINTS = (
    "context length", "context window", "too many tokens",
    "maximum number of tokens", "prompt is too long", "input length",
)


from core.infra import egress


def map_transport_error(exc: Exception, *, provider: str = "") -> ProviderError:
    """httpx / generic exception -> ErrorCode (施工单 §4.3)."""
    if isinstance(exc, ProviderError):
        return exc
    status = None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        status = int(exc.response.status_code)
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        code = "timeout"
    elif isinstance(exc, (httpx.ConnectError, httpx.NetworkError, ConnectionError, OSError)):
        code = "provider_down"
    elif status in (401, 403):
        code = "auth"
    elif status == 429:
        code = "rate_limit"
    elif status == 408:
        code = "timeout"
    elif status == 400:
        code = "context_overflow" if any(hint in lowered for hint in _CONTEXT_HINTS) else "bad_response"
    elif status is not None and 500 <= status < 600:
        code = "provider_down"
    else:
        code = "bad_response"
    return ProviderError(code, message[:200], provider=provider, status=status)


class AnthropicHTTPTransport:
    """Default httpx transport for the Messages API."""

    def __init__(self, spec: "EndpointSpec", *, settings=None) -> None:
        self.spec = spec
        self._settings = settings
        self._client: httpx.AsyncClient | None = None

    def refresh_credentials(self) -> None:
        self._client = None

    def _timeout(self) -> float:
        if self.spec.timeout is not None:
            return float(self.spec.timeout)
        settings = self._settings
        if settings is None:
            from core.config.settings import config as app_config
            settings = app_config.llm
        return float(getattr(settings, "timeout_seconds", 30) or 30)

    def _api_key(self) -> str:
        if self.spec.api_key:
            return str(self.spec.api_key)
        value = ""
        if self.spec.api_key_env:
            import os

            value = str(os.environ.get(self.spec.api_key_env, "") or "")
            if not value:
                settings = self._settings
                if settings is None:
                    from core.config.settings import config as app_config
                    settings = app_config.llm
                value = str(getattr(settings, self.spec.api_key_env.lower(), "") or "")
        return value or "missing-api-key"

    def _url(self) -> str:
        base = str(self.spec.base_url or "").rstrip("/")
        return base + "/messages" if base.endswith("/v1") else base + "/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key(),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = egress.async_client()
        return self._client

    async def send(self, payload: dict[str, Any], *, stream: bool = False,
                   timeout: float | None = None):
        client = self._get_client()
        effective = self._timeout() if timeout is None else float(timeout)
        if stream:
            return self._stream(client, payload, effective)
        response = await client.post(
            self._url(), json=payload, headers=self._headers(), timeout=effective,
        )
        if response.status_code >= 400:
            response.raise_for_status()
        return response.json()

    async def _stream(self, client: httpx.AsyncClient, payload: dict[str, Any],
                      timeout: float) -> AsyncIterator[dict[str, Any]]:
        async with client.stream(
            "POST", self._url(), json=payload, headers=self._headers(), timeout=timeout,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                response.raise_for_status()
            async for line in response.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    warn("llm/adapters/anthropic.py:165 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
                    continue

class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, spec, *, settings=None, transport=None,
                 reasoning_replay="encrypted", cache_control=False):
        self.spec = spec
        self._settings = settings
        self._transport = transport
        self.reasoning_replay = reasoning_replay if reasoning_replay in REPLAY_MODES else "encrypted"
        self.cache_control = bool(cache_control)

    def capabilities(self, model):
        return self.spec.capabilities

    def refresh_credentials(self):
        if self._transport is not None:
            refresher = getattr(self._transport, "refresh_credentials", None)
            if callable(refresher):
                refresher()
        else:
            self._transport = None

    def _get_transport(self):
        if self._transport is None:
            self._transport = AnthropicHTTPTransport(self.spec, settings=self._settings)
        return self._transport

    def _max_tokens(self, max_tokens):
        value = max_tokens or self.spec.max_tokens or DEFAULT_MAX_TOKENS
        return max(int(value), 1)

    def _thinking(self, reasoning_effort, thinking_budget):
        if thinking_budget is not None:
            return {"type": "enabled", "budget_tokens": max(int(thinking_budget), MIN_THINKING_BUDGET)}
        if reasoning_effort:
            budget = EFFORT_BUDGETS.get(str(reasoning_effort).lower())
            if budget:
                return {"type": "enabled", "budget_tokens": budget}
        return None

    def _system(self, system_text):
        if not system_text:
            return None
        if self.cache_control:
            return [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
        return system_text

    def _image_block(self, url):
        text = str(url or "")
        if not text:
            return None
        if text.startswith("data:"):
            header, _, data = text.partition(",")
            if not data or "base64" not in header:
                return None
            media_type = header[5:].split(";")[0] or "application/octet-stream"
            return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
        if text.startswith(("http://", "https://")):
            return {"type": "image", "source": {"type": "url", "url": text}}
        data_uri = bare_base64_data_uri(text)
        if not data_uri:
            return None
        return self._image_block(data_uri)

    def _user_content(self, content):
        if isinstance(content, list):
            blocks = []
            for part in content:
                if not isinstance(part, dict):
                    blocks.append({"type": "text", "text": str(part)})
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    blocks.append({"type": "text", "text": str(part.get("text") or "")})
                elif ptype == "image_url":
                    raw_url = part.get("image_url")
                    url = raw_url.get("url") if isinstance(raw_url, dict) else raw_url
                    block = self._image_block(str(url or ""))
                    if block:
                        blocks.append(block)
                elif ptype in ("image", "tool_result", "thinking", "redacted_thinking", "tool_use"):
                    blocks.append(part)
                else:
                    blocks.append({"type": "text", "text": json.dumps(part, ensure_ascii=False, default=str)})
            return blocks or [{"type": "text", "text": ""}]
        return [{"type": "text", "text": str(content or "")}]

    def _assistant_blocks(self, message):
        blocks = []
        content = message.get("content")
        if isinstance(content, list):
            blocks.extend(self._user_content(content))
        elif content:
            blocks.append({"type": "text", "text": str(content)})
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            arguments = call.get("arguments")
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                except json.JSONDecodeError:
                    warn("llm/adapters/anthropic.py:269 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
                    parsed = {}
            else:
                parsed = arguments or {}
            blocks.append({
                "type": "tool_use",
                "id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "input": parsed,
            })
        blocks.extend(self._replay_blocks(message.get("reasoning_blocks")))
        return blocks

    def _replay_blocks(self, blocks):
        result = []
        for block in blocks or []:
            if not isinstance(block, dict):
                continue
            source = str(block.get("source") or "")
            if self.reasoning_replay == "plaintext":
                text = str(block.get("text") or "")
                if text:
                    entry = {"type": "thinking", "thinking": text}
                    if block.get("signature"):
                        entry["signature"] = str(block["signature"])
                    result.append(entry)
                continue
            if source and source != self.spec.model:
                continue
            if block.get("encrypted"):
                result.append({"type": "redacted_thinking", "data": str(block["encrypted"])})
            elif block.get("signature"):
                result.append({
                    "type": "thinking",
                    "thinking": str(block.get("text") or ""),
                    "signature": str(block["signature"]),
                })
        return result
    def _convert_messages(self, messages):
        system_chunks = []
        out = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            if role == "system":
                system_chunks.append(str(message.get("content") or ""))
                continue
            if role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id") or message.get("tool_use_id") or ""),
                    "content": str(message.get("content") or ""),
                }
                if out and out[-1]["role"] == "user" and any(
                    isinstance(part, dict) and part.get("type") == "tool_result"
                    for part in out[-1]["content"]
                ):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue
            if role == "assistant":
                blocks = self._assistant_blocks(message)
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
                continue
            out.append({"role": "user", "content": self._user_content(message.get("content"))})
        system_text = "\n".join(chunk for chunk in system_chunks if chunk).strip()
        return system_text, out

    def _messages_with_images(self, messages, images):
        blocks = [block for block in (self._image_block(img) for img in images) if block]
        out = [dict(message) for message in messages]
        index = next((i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"), None)
        if index is None:
            out.append({"role": "user", "content": blocks})
            return out
        content = out[index].get("content")
        if isinstance(content, list):
            out[index]["content"] = list(content) + blocks
        else:
            head = [{"type": "text", "text": str(content)}] if content else []
            out[index]["content"] = head + blocks
        return out

    def _convert_tools(self, tools):
        out = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function":
                function = tool.get("function") or {}
                out.append({
                    "name": str(function.get("name") or ""),
                    "description": str(function.get("description") or ""),
                    "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
                })
            elif tool.get("name"):
                out.append(tool)
        return out

    def _convert_tool_choice(self, tool_choice):
        if isinstance(tool_choice, dict):
            if tool_choice.get("type") == "function":
                name = (tool_choice.get("function") or {}).get("name") or ""
                return {"type": "tool", "name": str(name)}
            return tool_choice
        if tool_choice == "required":
            return {"type": "any"}
        if tool_choice == "none":
            return {"type": "auto"}
        return {"type": "auto"}

    def _usage(self, raw_usage):
        if not isinstance(raw_usage, dict):
            return Usage()
        base_input = raw_usage.get("input_tokens")
        cache_read = raw_usage.get("cache_read_input_tokens")
        cache_write = raw_usage.get("cache_creation_input_tokens")
        parts = [value for value in (base_input, cache_read, cache_write) if isinstance(value, int)]
        prompt = sum(parts) if parts else None
        completion = raw_usage.get("output_tokens")
        total = prompt + completion if prompt is not None and isinstance(completion, int) else None
        cache_miss = None
        if isinstance(base_input, int) or isinstance(cache_write, int):
            cache_miss = sum(value for value in (base_input, cache_write) if isinstance(value, int))
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cache_hit_tokens=cache_read if isinstance(cache_read, int) else None,
            cache_miss_tokens=cache_miss,
        )

    def _base_meta(self, raw, *, messages, tools=None, images=None):
        return {
            "id": str(raw.get("id") or ""),
            "model": str(raw.get("model") or ""),
            "request_digest": request_digest(messages),
            "tool_schema_digest": tool_schema_digest(tools),
            "image_count": len(images or []),
        }

    def _chat_result(self, raw, *, messages, tools=None, images=None):
        blocks = raw.get("content") or []
        text = "".join(
            str(block.get("text") or "") for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        reasoning = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                reasoning.append(ReasoningBlock(
                    text=str(block.get("thinking") or ""),
                    signature=block.get("signature"),
                ))
            elif block.get("type") == "redacted_thinking":
                reasoning.append(ReasoningBlock(text="", encrypted=str(block.get("data") or "")))
        stop_reason = raw.get("stop_reason")
        return ChatResult(
            text=text,
            usage=self._usage(raw.get("usage")),
            finish_reason=_FINISH_MAP.get(stop_reason, str(stop_reason or "stop")),
            reasoning_blocks=tuple(reasoning),
            raw_meta=self._base_meta(raw, messages=messages, tools=tools, images=images),
        )

    def _tools_result(self, raw, *, messages, tools=None):
        base = self._chat_result(raw, messages=messages, tools=tools)
        calls = []
        for block in raw.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append({
                    "id": str(block.get("id") or ""),
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                })
        return ToolsResult(
            text=base.text,
            usage=base.usage,
            finish_reason=base.finish_reason,
            reasoning_blocks=base.reasoning_blocks,
            raw_meta=base.raw_meta,
            tool_calls=tuple(calls),
        )

    def _payload(self, *, model, messages, temperature, max_tokens, tools=None,
                 tool_choice=None, reasoning_effort=None, thinking_budget=None,
                 stream=False, images=None):
        if images:
            messages = self._messages_with_images(messages, images)
        system_text, converted = self._convert_messages(messages)
        payload = {
            "model": model or self.spec.model,
            "max_tokens": self._max_tokens(max_tokens),
            "messages": converted,
            "temperature": temperature,
        }
        system = self._system(system_text)
        if system is not None:
            payload["system"] = system
        thinking = self._thinking(reasoning_effort, thinking_budget)
        if thinking is not None:
            payload["thinking"] = thinking
        if tools:
            payload["tools"] = self._convert_tools(tools)
            payload["tool_choice"] = self._convert_tool_choice(tool_choice)
        if stream:
            payload["stream"] = True
        return payload

    async def _call(self, payload, *, timeout=None):
        transport = self._get_transport()
        try:
            return await transport.send(payload, stream=False, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise map_transport_error(exc, provider=self.spec.name) from exc
    async def chat(self, *, model, messages, temperature, max_tokens,
                   reasoning_effort=None, thinking_budget=None, timeout=None):
        payload = self._payload(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
            reasoning_effort=reasoning_effort, thinking_budget=thinking_budget,
        )
        raw = await self._call(payload, timeout=timeout)
        return self._chat_result(raw, messages=messages)

    async def chat_tools(self, *, model, messages, tools, tool_choice="auto",
                         temperature=0.1, max_tokens=1200,
                         reasoning_effort=None, thinking_budget=None, timeout=None):
        payload = self._payload(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
            tools=tools, tool_choice=tool_choice,
            reasoning_effort=reasoning_effort, thinking_budget=thinking_budget,
        )
        raw = await self._call(payload, timeout=timeout)
        return self._tools_result(raw, messages=messages, tools=tools)

    async def chat_vision(self, *, model, messages, images, temperature=0.3,
                          max_tokens=2000, timeout=None):
        payload = self._payload(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
            images=images,
        )
        raw = await self._call(payload, timeout=timeout)
        return self._chat_result(raw, messages=messages, images=images)

    async def stream(self, *, model, messages, temperature=0.7, max_tokens=800,
                     reasoning_effort=None, thinking_budget=None, timeout=None):
        payload = self._payload(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
            reasoning_effort=reasoning_effort, thinking_budget=thinking_budget, stream=True,
        )
        transport = self._get_transport()
        try:
            events = await transport.send(payload, stream=True, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise map_transport_error(exc, provider=self.spec.name) from exc

        usage_input: dict[str, Any] = {}
        usage_output: dict[str, Any] = {}
        finish = None
        try:
            async for event in events:
                event_type = event.get("type")
                if event_type == "message_start":
                    usage_input = (event.get("message") or {}).get("usage") or usage_input
                elif event_type == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield StreamChunk(text=str(delta["text"]))
                elif event_type == "message_delta":
                    finish = (event.get("delta") or {}).get("stop_reason") or finish
                    usage_output = event.get("usage") or usage_output
                elif event_type == "message_stop":
                    break
        except Exception as exc:  # noqa: BLE001
            raise map_transport_error(exc, provider=self.spec.name) from exc

        merged_usage = dict(usage_input or {})
        merged_usage.update(usage_output or {})
        usage = self._usage(merged_usage) if merged_usage else None
        if usage is not None or finish:
            yield StreamChunk(
                text="",
                usage=usage,
                finish_reason=_FINISH_MAP.get(finish, str(finish)) if finish else None,
            )

    async def list_models(self):
        # TODO(owner): Anthropic /v1/models 需要单独的 HTTP 面；契约不依赖它。
        return []
