"""WP2 openai_compat adapter contract (FakeTransport, zero network).

Run directly:
    python tests/test_llm_adapter_contract.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import httpx2 as _httpx
except ImportError:  # pragma: no cover - openai<3 vendored plain httpx
    import httpx as _httpx

from openai import (  # noqa: E402
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from llm.adapters.base import Capabilities, ProviderError  # noqa: E402
from llm.adapters.openai_compat import (  # noqa: E402
    OpenAICompatAdapter,
    request_digest,
    tool_schema_digest,
)
from llm.config import EndpointSpec  # noqa: E402

_UNSET = object()


class _FakeCompletions:
    def __init__(self, behaviors) -> None:
        self.behaviors = list(behaviors)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        behavior = self.behaviors.pop(0)
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior


class _FakeChat:
    def __init__(self, completions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, behaviors) -> None:
        self.completions = _FakeCompletions(behaviors)
        self.chat = _FakeChat(self.completions)


class _FakeStream:
    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class _StreamChunk:
    def __init__(self, text=None, usage=None, finish=None) -> None:
        delta = SimpleNamespace(content=text)
        self.choices = (
            [SimpleNamespace(delta=delta, finish_reason=finish)]
            if (text is not None or finish)
            else []
        )
        self.usage = usage


def _usage(prompt=10, completion=5, cached=4, reasoning=2):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


def _tool_call(cid="c1", name="search", arguments='{"q": "x"}'):
    return SimpleNamespace(
        id=cid,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(text="", tool_calls=None, usage=_UNSET, finish="stop",
              resp_id="resp-1", model="m-1", reasoning=None):
    message = SimpleNamespace(
        content=text,
        tool_calls=tool_calls,
        reasoning_content=reasoning,
    )
    choice = SimpleNamespace(message=message, finish_reason=finish)
    return SimpleNamespace(
        choices=[choice],
        usage=_usage() if usage is _UNSET else usage,
        id=resp_id,
        model=model,
    )


def _spec(**caps):
    defaults = dict(
        tools=True, vision=True, json_mode=True, streaming=True,
        stream_usage=True, context_window=8192,
    )
    defaults.update(caps)
    return EndpointSpec(
        name="fake",
        kind="openai_compat",
        base_url="https://fake.test/v1",
        model="m-1",
        api_key="sk-fake",
        timeout=5.0,
        capabilities=Capabilities(**defaults),
    )


def _adapter(behaviors, **caps):
    client = _FakeClient(behaviors)
    adapter = OpenAICompatAdapter(_spec(**caps), client_factory=lambda: client)
    return adapter, client


def _http_response(status: int):
    request = _httpx.Request("POST", "https://fake.test/v1/chat/completions")
    return _httpx.Response(
        status,
        request=request,
        json={"error": {"message": "boom"}},
        headers={"content-type": "application/json"},
    )


def _status_error(cls, status: int, message: str):
    return cls(message, response=_http_response(status), body=None)

def test_chat_maps_text_and_usage() -> None:
    adapter, client = _adapter([_response(text="你好", usage=_usage(10, 5, 4, 2))])
    result = asyncio.run(adapter.chat(
        model="m-1", messages=[{"role": "user", "content": "hi"}],
        temperature=0.3, max_tokens=64,
    ))
    assert result.text == "你好"
    assert result.finish_reason == "stop"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens,
            result.usage.total_tokens) == (10, 5, 15)
    assert result.usage.reasoning_tokens == 2
    assert result.usage.cache_hit_tokens == 4 and result.usage.cache_miss_tokens == 6
    assert result.raw_meta["id"] == "resp-1"
    call = client.completions.calls[0]
    assert call["model"] == "m-1" and call["timeout"] == 5.0

    adapter2, client2 = _adapter([_response(text="x")])
    asyncio.run(adapter2.chat(
        model="m-1", messages=[{"role": "user", "content": "hi"}],
        temperature=0.1, max_tokens=8, reasoning_effort="high",
    ))
    assert client2.completions.calls[0]["reasoning_effort"] == "high"


def test_tools_preserve_raw_arguments() -> None:
    raw = '{"q": "天气", "n": 2}'
    tools = [{"type": "function", "function": {"name": "search"}}]
    adapter, client = _adapter([_response(tool_calls=[_tool_call(arguments=raw)], usage=_usage(1, 1))])
    result = asyncio.run(adapter.chat_tools(
        model="m-1", messages=[{"role": "user", "content": "hi"}], tools=tools,
    ))
    assert result.tool_calls == ({"id": "c1", "name": "search", "arguments": raw},)
    assert result.text == ""
    assert result.raw_meta["tool_schema_digest"] == tool_schema_digest(tools)
    assert client.completions.calls[0]["tools"] == tools


def test_error_mapping() -> None:
    request = _httpx.Request("POST", "https://fake.test/v1/chat/completions")
    cases = [
        (_status_error(RateLimitError, 429, "rate limited"), "rate_limit"),
        (_status_error(AuthenticationError, 401, "bad key"), "auth"),
        (_status_error(PermissionDeniedError, 403, "forbidden"), "auth"),
        (_status_error(BadRequestError, 400, "maximum context length is 8192 tokens"), "context_overflow"),
        (_status_error(BadRequestError, 400, "invalid tool schema"), "bad_response"),
        (_status_error(InternalServerError, 500, "server down"), "provider_down"),
        (APITimeoutError(request=request), "timeout"),
        (APIConnectionError(request=request), "provider_down"),
        (APIError("boom", request=request, body=None), "bad_response"),
    ]
    for exc, expected in cases:
        adapter, _client = _adapter([exc])

        async def _call(adapter=adapter):
            await adapter.chat(
                model="m-1", messages=[{"role": "user", "content": "x"}],
                temperature=0.1, max_tokens=8,
            )

        try:
            asyncio.run(_call())
        except ProviderError as err:
            assert err.code == expected, (type(exc).__name__, err.code, expected)
            assert err.provider == "fake", err.provider
        else:
            raise AssertionError(f"{type(exc).__name__} did not raise")


def test_stream_usage_from_stream_options() -> None:
    chunks = [
        _StreamChunk(text="你"),
        _StreamChunk(text="好"),
        _StreamChunk(usage=_usage(10, 5, 4, 2), finish="stop"),
    ]
    adapter, client = _adapter([_FakeStream(chunks)], stream_usage=True)

    async def _collect(adapter=adapter):
        return [chunk async for chunk in adapter.stream(
            model="m-1", messages=[{"role": "user", "content": "hi"}],
        )]

    out = asyncio.run(_collect())
    assert "".join(chunk.text for chunk in out) == "你好"
    final = out[-1]
    assert final.text == "" and final.usage is not None
    assert final.usage.total_tokens == 15 and final.finish_reason == "stop"
    call = client.completions.calls[0]
    assert call["stream"] is True
    assert call["stream_options"] == {"include_usage": True}

    adapter2, client2 = _adapter(
        [_FakeStream([_StreamChunk(text="x"), _StreamChunk(usage=_usage(), finish="stop")])],
        stream_usage=False,
    )
    asyncio.run(_collect(adapter=adapter2))
    assert "stream_options" not in client2.completions.calls[0]


def test_stream_usage_unknown_param_falls_back() -> None:
    error = _status_error(BadRequestError, 400, "Unknown parameter: 'stream_options'.")
    stream = _FakeStream([_StreamChunk(text="x"), _StreamChunk(usage=_usage(), finish="stop")])
    adapter, client = _adapter([error, stream], stream_usage=True)

    async def _collect():
        return [chunk async for chunk in adapter.stream(
            model="m-1", messages=[{"role": "user", "content": "hi"}],
        )]

    out = asyncio.run(_collect())
    assert "".join(chunk.text for chunk in out) == "x"
    assert len(client.completions.calls) == 2
    assert "stream_options" in client.completions.calls[0]
    assert "stream_options" not in client.completions.calls[1]


def test_response_is_reconstructible() -> None:
    raw_args = '{"q": "replay"}'
    tools = [{"type": "function", "function": {"name": "search"}}]
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    adapter, _client = _adapter([_response(
        text="", tool_calls=[_tool_call(arguments=raw_args)],
        usage=_usage(7, 3, 1, 1), reasoning="thinking...",
        finish="tool_calls", resp_id="r-9",
    )])
    result = asyncio.run(adapter.chat_tools(model="m-1", messages=messages, tools=tools))

    assert result.raw_meta["request_digest"] == request_digest(messages)
    assert result.raw_meta["tool_schema_digest"] == tool_schema_digest(tools)
    assert result.raw_meta["id"] == "r-9"
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls[0]["arguments"] == raw_args
    assert result.reasoning_blocks and result.reasoning_blocks[0].text == "thinking..."
    assert result.usage.total_tokens == 10
    assert request_digest(messages) != request_digest(messages + [{"role": "user", "content": "more"}])


_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_vision_appends_image_parts_and_drops_unknown() -> None:
    adapter, client = _adapter([_response(text="看到了", usage=_usage())])
    result = asyncio.run(adapter.chat_vision(
        model="m-1",
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "看图"},
        ],
        images=["data:image/png;base64,AAAA", "[图片]", _PNG_B64],
    ))
    assert result.text == "看到了"
    sent = client.completions.calls[0]["messages"]
    parts = sent[-1]["content"]
    assert isinstance(parts, list), parts
    texts = [part for part in parts if part.get("type") == "text"]
    urls = [part["image_url"]["url"] for part in parts if part.get("type") == "image_url"]
    assert texts and texts[0]["text"] == "看图"
    assert urls[0] == "data:image/png;base64,AAAA"
    assert urls[1] == f"data:image/png;base64,{_PNG_B64}"
    assert len(urls) == 2
    assert result.raw_meta["image_count"] == 3


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
