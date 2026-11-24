"""Generic OpenAI-compatible adapter (WP2).

One adapter covers deepseek / openai / ollama / dashscope and any endpoint that
speaks the Chat Completions shape. Protocol fields stay in here; core/ and
agents/ never see them (施工单 §3.1 / I2).

Error mapping follows §4.3. Streaming asks for
``stream_options={"include_usage": true}`` only when the endpoint declares
``capabilities.stream_usage`` and retries once without the option if the
endpoint rejects it.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

import structlog
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from core.config.settings import config as _app_config
from core.conversation.media import image_mime
from llm.adapters.base import (
    Capabilities,
    ChatResult,
    ProviderError,
    ReasoningBlock,
    StreamChunk,
    ToolsResult,
    Usage,
)
from safelog import warn

if TYPE_CHECKING:
    from llm.config import EndpointSpec

log = structlog.get_logger()

# 思考档下 max_tokens 的地板：e2e 实测 low 档一条群聊短稿要烧 130~670 个思考 token，
# 若沿用出稿侧的 300 会被思考吃光、正文返回空字符串（finish_reason=length）。
_MIN_REASONING_MAX_TOKENS = 1200

_CONTEXT_HINTS = (
    "context length", "context_length", "maximum context", "max context",
    "too many tokens", "maximum number of tokens", "reduce the length",
)
_STREAM_OPTION_HINTS = (
    "stream_options", "include_usage", "unknown parameter",
    "unrecognized", "unsupported parameter", "extra inputs",
)


def _digest(payload: Any) -> str:
    try:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - digest must never break a request
        warn("llm/adapters/openai_compat.py:71 Exception", err=repr(__import__("sys").exc_info()[1]))
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _pad_reasoning_content(messages: list[dict]) -> list[dict]:
    """思考档下回灌的 assistant(tool_calls) 必须带 reasoning_content。

    2026-10-24 定位：DeepSeek thinking 模式要求把上一轮 assistant 的
    reasoning_content 带回去，否则 400：The reasoning_content in the thinking
    mode must be passed back to the API. 实测（deepseek-flash + effort=low）：
    缺字段的 assistant(tool_calls) -> 400；空串 -> 200；真思考文本 -> 200。
    2026-10-24 追加实证（最小复现）：**有 tools 时，任何 assistant 消息**缺该键都会 400，
    不只是 tool_calls 那条——窗口摘要（role=assistant 的 [早前工具轮·摘要]）缺键同样 400。
    因此这里对所有缺键的 assistant 消息补空串（只补键，不动 content）。
    """
    out: list[dict] = []
    for m in messages or []:
        if (
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and not m.get("reasoning_content")
        ):
            m = dict(m)
            m["reasoning_content"] = ""
        out.append(m)
    return out


def request_digest(messages: list[dict]) -> str:
    """Stable digest of the request messages (I5 replay; no raw content kept)."""
    return _digest(messages)


def tool_schema_digest(tools: list[dict] | None) -> str | None:
    return _digest(tools) if tools else None


def bare_base64_data_uri(img: str) -> str:
    """``data:`` URI for a bare-base64 image, typed from its own bytes.

    The media type comes from ``core.conversation.media.image_mime`` -- the one
    owner of "what is this file", which reads the magic bytes first.  This used
    to be the literal ``image/jpeg``, so a PNG/GIF/WEBP payload went out declared
    as a JPEG: the bytes and the type disagreed, which the provider is entitled
    to reject.  ``image_mime`` answers about a path, so the decoded bytes are
    handed to it through a temporary file; **nothing is sniffed again here**.

    ``""`` means "no type could be established" -- not base64, empty, or a format
    the owner refuses to name.  The caller drops that image and keeps the text,
    the same honest fallback ``media_path_to_data_uri`` performs, instead of
    announcing a JPEG that was never there.
    """
    payload = "".join(str(img or "").split())
    if not payload:
        return ""
    try:
        raw = base64.b64decode(payload)
    except Exception:  # noqa: BLE001 - not base64 (a placeholder, a stray path)
        log.warning("llm_vision_image_not_base64", size=len(payload))
        return ""
    if not raw:
        return ""
    path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(raw)
            path = handle.name
        mime = image_mime(path)
    except OSError as exc:
        log.warning("llm_vision_image_type_unreadable", error=str(exc)[:120])
        return ""
    finally:
        if path:
            with contextlib.suppress(OSError):
                Path(path).unlink()
    if not mime:
        log.warning("llm_vision_image_type_unknown", size=len(raw))
        return ""
    return f"data:{mime};base64,{payload}"


def _looks_like_path(text: str) -> bool:
    """是不是"一个本地文件路径"（而不是裸 base64）。Windows 盘符、UNC、正/反斜杠都算。"""
    if not text or text.startswith("data:"):
        return False
    if len(text) > 260:  # 路径长度上限，超了基本是 base64
        return False
    low = text.lower()
    if low.startswith(("\\", "//", "./", "../", "~")):
        return True
    if len(text) > 2 and text[1] == ":" and text[0].isalpha():
        return True
    return ("/" in text or "\\" in text) and "." in text.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _image_url(img: str) -> str:
    text = str(img or "")
    if text.startswith("data:") or text.startswith(("http://", "https://")):
        return text
    # 2026-10-25：这里原来无条件走 bare_base64_data_uri()，它把入参当**裸 base64** 去
    # b64decode；传本地路径时解码失败 → 返回 ""，图被静默丢弃（日志只有一条
    # llm_vision_image_not_base64，完全看不出是"路径没被支持"）。所以先判路径，
    # 路径交给 media_path_to_data_uri（它会读文件、按 magic bytes 定 mime）。
    if _looks_like_path(text):
        try:
            from core.conversation.media import media_path_to_data_uri

            uri = media_path_to_data_uri(text)
            if uri:
                return uri
            log.warning("llm_vision_image_path_unreadable", path=text[:120])
            return ""
        except Exception as exc:  # noqa: BLE001 - 路径分支失败就退回裸 base64
            log.warning("llm_vision_image_path_failed", path=text[:120], error=str(exc)[:100])
            return ""
    return bare_base64_data_uri(text)


def messages_with_images(messages: list[dict], images: list[str]) -> list[dict]:
    """Append image parts to the last user message; unknown images are dropped."""
    parts: list[dict] = []
    dropped: list[str] = []
    for img in images:
        url = _image_url(img)
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
        else:
            # 2026-10-25：以前只打一条无参数的 warning，图被静默丢掉，排查时完全看不出来
            # 到底是"没传进来"还是"传进来读不出"。现在把内容与长度一起记下来。
            dropped.append(str(img or "")[:60] + "…(len=%d)" % len(str(img or "")))
            log.warning("llm_vision_image_dropped", img=str(img or "")[:60], n=len(images))
    out = [dict(m) for m in messages]
    index = next(
        (i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"),
        None,
    )
    if index is None:
        if parts:
            out.append({"role": "user", "content": parts})
        return out
    content = out[index].get("content")
    if isinstance(content, list):
        out[index]["content"] = list(content) + parts
    else:
        # 与旧行为一致：user 内容永远转成 parts 列表（即使一张图都没留下）。
        out[index]["content"] = [{"type": "text", "text": str(content or "")}, *parts]
    if images and not parts:
        # 一张都没送进去：与其让模型回"我看不到图片"，不如在这里说清楚。
        raise ValueError(
            "vision 请求里一张图都没能送进去（%d 张全部读不出）：%s"
            % (len(images), "; ".join(dropped)[:300])
        )
    return out


def _usage_from_sdk(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    total = getattr(usage, "total_tokens", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    cache_hit = getattr(prompt_details, "cached_tokens", None) if prompt_details is not None else None
    reasoning = getattr(completion_details, "reasoning_tokens", None) if completion_details is not None else None
    cache_miss = None
    if isinstance(prompt, int) and isinstance(cache_hit, int):
        cache_miss = max(prompt - cache_hit, 0)
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        reasoning_tokens=reasoning,
        cache_hit_tokens=cache_hit,
        cache_miss_tokens=cache_miss,
    )


def _reasoning_blocks(message: Any) -> tuple[ReasoningBlock, ...]:
    text = str(getattr(message, "reasoning_content", "") or "").strip()
    if not text:
        return ()
    return (ReasoningBlock(text=text),)


def map_sdk_error(exc: Exception, *, provider: str = "") -> ProviderError:
    """SDK / HTTP exception -> ErrorCode (施工单 §4.3)."""
    if isinstance(exc, ProviderError):
        return exc
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = None
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, (APITimeoutError, TimeoutError)) or status == 408:
        code = "timeout"
    elif isinstance(exc, (AuthenticationError, PermissionDeniedError)) or status in (401, 403):
        code = "auth"
    elif isinstance(exc, RateLimitError) or status == 429:
        code = "rate_limit"
    elif isinstance(exc, BadRequestError):
        code = "context_overflow" if any(hint in lowered for hint in _CONTEXT_HINTS) else "bad_response"
    elif isinstance(exc, (APIConnectionError, InternalServerError, ConnectionError, OSError)):
        code = "provider_down"
    elif status is not None and 500 <= status < 600:
        code = "provider_down"
    elif isinstance(exc, APIError):
        code = "bad_response"
    else:
        code = "bad_response"
    return ProviderError(code, message[:200], provider=provider, status=status)


def _mentions_stream_options(exc: ProviderError) -> bool:
    lowered = str(exc).lower()
    return any(hint in lowered for hint in _STREAM_OPTION_HINTS)

class OpenAICompatAdapter:
    """Adapter for any Chat-Completions-shaped endpoint."""

    name = "openai_compat"

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
            if not value:
                settings = self._settings if self._settings is not None else _app_config.llm
                value = str(getattr(settings, self.spec.api_key_env.lower(), "") or "")
        # Router 的 auth_ok 会先过滤未配 key 的端点；这里只兜底，避免 SDK
        # 构造期报一个比 401 更难读的错。
        return value or "missing-api-key"

    def _get_client(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                self._client = AsyncOpenAI(
                    api_key=self._api_key(),
                    base_url=self.spec.base_url,
                )
        return self._client

    def refresh_credentials(self) -> None:
        """Drop the cached SDK client so the next call re-resolves the key (§4.3)."""
        self._client = None

    def _request_kwargs(
        self, *, model, messages, temperature, max_tokens, timeout,
        tools=None, tool_choice=None, reasoning_effort=None, thinking_budget=None,
        thinking=None, stream=False,
    ) -> dict[str, Any]:
        effort = reasoning_effort if reasoning_effort is not None else (self.spec.reasoning_effort or None)
        think = thinking if thinking is not None else (self.spec.thinking or None)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        # 思考档：官方文档说 thinking 模式不支持 temperature / penalty（会削思考质量）。
        # 所以只要这一档在思考，就不传 temperature；max_tokens 也要抬到思考烧得起的量。
        if think == "disabled":
            kwargs["temperature"] = temperature
        elif effort or think:
            kwargs["max_tokens"] = max(int(max_tokens or 0), _MIN_REASONING_MAX_TOKENS)
        else:
            kwargs["temperature"] = temperature
            kwargs["max_tokens"] = max_tokens
        if "max_tokens" not in kwargs:
            kwargs["max_tokens"] = max_tokens
        kwargs["timeout"] = self._timeout() if timeout is None else timeout
        if tools is not None:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto" if tool_choice is None else tool_choice
        if effort:
            kwargs["reasoning_effort"] = effort
        if think in ("enabled", "disabled"):
            kwargs["extra_body"] = {"thinking": {"type": think}}
        if think != "disabled" and (effort or think):
            kwargs["messages"] = _pad_reasoning_content(messages)
        # thinking_budget 是 Anthropic 语义；OpenAI 兼容端点暂不映射（TODO(owner)）。
        if stream:
            kwargs["stream"] = True
        return kwargs

    async def _create(self, **kwargs):
        client = self._get_client()
        try:
            return await client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            try:  # 2026-10-24 诊断：失败时把真实 payload 落盘，供离线比对
                import json as _json
                from pathlib import Path as _Path
                _body = {k: v for k, v in kwargs.items() if k != "messages"}
                _body["messages"] = [
                    {**{kk: vv for kk, vv in _m.items() if kk != "content"},
                     "content": (str(_m.get("content"))[:300] if _m.get("content") else _m.get("content"))}
                    for _m in (kwargs.get("messages") or [])
                ]
                _p = _Path("logs") / ("llm_payload_dump.json")
                _p.parent.mkdir(parents=True, exist_ok=True)
                _p.write_text(_json.dumps({"error": str(exc)[:400], "endpoint": self.spec.name, "body": _body}, ensure_ascii=False, indent=1, default=str)[:400000], encoding="utf-8")
            except Exception:
                warn("llm/adapters/openai_compat.py:351 Exception", err=repr(__import__("sys").exc_info()[1]))
            raise map_sdk_error(exc, provider=self.spec.name) from exc

    def _raw_meta(self, resp, *, messages, tools=None, images=None) -> dict[str, Any]:
        return {
            "id": str(getattr(resp, "id", "") or ""),
            "model": str(getattr(resp, "model", "") or ""),
            "request_digest": request_digest(messages),
            "tool_schema_digest": tool_schema_digest(tools),
            "image_count": len(images or []),
        }

    def _chat_result(self, resp, *, messages, tools=None, images=None) -> ChatResult:
        choices = getattr(resp, "choices", None) or []
        if not choices:
            raise ProviderError("bad_response", "provider 返回空 choices", provider=self.spec.name)
        choice = choices[0]
        message = getattr(choice, "message", None)
        return ChatResult(
            text=str(getattr(message, "content", "") or ""),
            usage=_usage_from_sdk(getattr(resp, "usage", None)),
            finish_reason=str(getattr(choice, "finish_reason", "") or "stop"),
            reasoning_blocks=_reasoning_blocks(message),
            raw_meta=self._raw_meta(resp, messages=messages, tools=tools, images=images),
        )

    async def chat(
        self, *, model: str, messages: list[dict], temperature: float, max_tokens: int,
        reasoning_effort: str | None = None, thinking_budget: int | None = None,
        thinking: str | None = None, timeout: float | None = None,
    ) -> ChatResult:
        kwargs = self._request_kwargs(
            model=model, messages=messages, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, thinking=thinking,
            reasoning_effort=reasoning_effort, thinking_budget=thinking_budget,
        )
        resp = await self._create(**kwargs)
        return await self._chat_result_or_retry(resp, kwargs, messages=messages)

    async def _chat_result_or_retry(self, resp, kwargs, *, messages) -> ChatResult:
        """思考把 output 上限吃光（finish_reason=length 且正文空）时重发一次。

        2026-10-16 实测：flash 的 low 档偶发烧到 900 上限、正文返回空串；
        不重发的话出稿层会拿空草稿当回复。重发用 max_completion_tokens 抬到
        _MIN_REASONING_MAX_TOKENS（官方文档：它是输出上限，思考也算在里面）；
        再吃光就原样返回，由调用方的空回复路径兜底。
        """
        result = self._chat_result(resp, messages=messages)
        thinking_on = bool(kwargs.get("reasoning_effort")) or bool(
            (kwargs.get("extra_body") or {}).get("thinking", {}).get("type") == "enabled"
        )
        if not thinking_on or result.text.strip() or result.finish_reason != "length":
            return result
        used = int(kwargs.get("max_tokens") or kwargs.get("max_completion_tokens") or 0)
        if used >= _MIN_REASONING_MAX_TOKENS:
            return result
        bumped = dict(kwargs)
        bumped.pop("max_tokens", None)
        bumped["max_completion_tokens"] = _MIN_REASONING_MAX_TOKENS
        log.warning("llm_reasoning_ate_budget", endpoint=self.spec.name,
                    used=used, bumped=_MIN_REASONING_MAX_TOKENS)
        try:
            retry = await self._create(**bumped)
        except Exception as exc:  # noqa: BLE001 - 重发失败就用首答（空串由调用方兜底）
            log.warning("llm_reasoning_retry_failed", endpoint=self.spec.name,
                        error=str(exc)[:160])
            return result
        return self._chat_result(retry, messages=messages)

    async def chat_tools(
        self, *, model: str, messages: list[dict], tools: list[dict], thinking: str | None = None,
        tool_choice: str | dict = "auto", temperature: float = 0.1,
        max_tokens: int = 1200,
        reasoning_effort: str | None = None, thinking_budget: int | None = None,
        timeout: float | None = None,
    ) -> ToolsResult:
        resp = await self._create(**self._request_kwargs(
            model=model, messages=messages, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout, tools=tools,
            tool_choice=tool_choice, reasoning_effort=reasoning_effort,
            thinking_budget=thinking_budget,
        ))
        base = self._chat_result(resp, messages=messages, tools=tools)
        choices = getattr(resp, "choices", None) or []
        message = getattr(choices[0], "message", None)
        calls: list[dict[str, Any]] = []
        for call in (getattr(message, "tool_calls", None) or []):
            function = getattr(call, "function", None)
            calls.append({
                "id": str(getattr(call, "id", "") or ""),
                "name": str(getattr(function, "name", "") or ""),
                # 参数保持原始 JSON 字符串（解析/修复留在兼容壳，§4.6）
                "arguments": str(getattr(function, "arguments", "") or "{}"),
            })
        return ToolsResult(
            text=base.text,
            usage=base.usage,
            finish_reason=base.finish_reason,
            reasoning_blocks=base.reasoning_blocks,
            raw_meta=base.raw_meta,
            tool_calls=tuple(calls),
        )

    async def chat_vision(
        self, *, model: str, messages: list[dict], images: list[str], thinking: str | None = None,
        temperature: float = 0.3, max_tokens: int = 2000,
        timeout: float | None = None,
    ) -> ChatResult:
        request_messages = messages_with_images(messages, images)
        resp = await self._create(**self._request_kwargs(
            model=model, messages=request_messages, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout,
        ))
        return self._chat_result(resp, messages=request_messages, images=images)

    async def stream(
        self, *, model: str, messages: list[dict], temperature: float = 0.7,
        max_tokens: int = 800,
        reasoning_effort: str | None = None, thinking_budget: int | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[StreamChunk]:
        kwargs = self._request_kwargs(
            model=model, messages=messages, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout,
            reasoning_effort=reasoning_effort, thinking_budget=thinking_budget,
            stream=True,
        )
        wants_usage = bool(self.spec.capabilities.stream_usage)
        if wants_usage:
            kwargs["stream_options"] = {"include_usage": True}
        try:
            stream = await self._create(**kwargs)
        except ProviderError as exc:
            warn("llm/adapters/openai_compat.py:484 ProviderError", err=repr(exc))
            if not (wants_usage and exc.code == "bad_response" and _mentions_stream_options(exc)):
                raise
            # 端点不认识 stream_options：去掉参数重试一次（§4.3 流式降级）。
            kwargs.pop("stream_options", None)
            stream = await self._create(**kwargs)

        usage: Usage | None = None
        finish: str | None = None
        async for chunk in stream:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = _usage_from_sdk(chunk_usage)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason:
                finish = str(finish_reason)
            delta = getattr(choice, "delta", None)
            text = getattr(delta, "content", None) if delta is not None else None
            if text:
                yield StreamChunk(text=str(text))
        if usage is not None or finish is not None:
            yield StreamChunk(text="", usage=usage, finish_reason=finish)

    async def list_models(self) -> list[dict]:
        client = self._get_client()
        try:
            resp = await client.models.list()
        except Exception as exc:  # noqa: BLE001
            raise map_sdk_error(exc, provider=self.spec.name) from exc
        return [
            {"id": str(getattr(model, "id", "") or "")}
            for model in (getattr(resp, "data", None) or [])
        ]
