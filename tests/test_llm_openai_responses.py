"""OpenAI Responses 适配器（CC Switch 本地代理）的协议翻译与解析（无网络）。

2026-11-22：DeepSeek 官方 key 402 之后，这条适配器是唯一的第二腿；
真机已验证 chat / tools / function_call_output / system+强制工具 / 图片五条路径。
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.adapters.base import Capabilities, ProviderError  # noqa: E402
from llm.adapters.contract import rate_limit_error  # noqa: E402
from llm.adapters.openai_responses import (  # noqa: E402
    OpenAIResponsesAdapter,
    parse_responses_output,
    to_responses_input,
    to_responses_tool_choice,
    to_responses_tools,
)
from llm.config import EndpointSpec  # noqa: E402


class _FakeResponses:
    def __init__(self, owner: "_FakeClient") -> None:
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        if not self._owner.queue:
            raise RuntimeError("fake queue is empty")
        item = self._owner.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeClient:
    def __init__(self, *queue) -> None:
        self.calls: list[dict] = []
        self.queue = list(queue)
        self.responses = _FakeResponses(self)


def _text_response(text: str = "hello", *, total: int = 15):
    message = SimpleNamespace(
        type="message", content=[SimpleNamespace(type="output_text", text=text)],
    )
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5, total_tokens=total,
        input_tokens_details=SimpleNamespace(cached_tokens=4),
    )
    return SimpleNamespace(id="resp_1", model="ccs-model", status="completed",
                           output=[message], usage=usage)


def _tool_response(call_id: str = "call_1", name: str = "say",
                   arguments: str = '{"text": "hi"}'):
    item = SimpleNamespace(type="function_call", call_id=call_id, id="fc_1",
                           name=name, arguments=arguments)
    usage = SimpleNamespace(input_tokens=3, output_tokens=2, total_tokens=5,
                            input_tokens_details=None)
    return SimpleNamespace(id="resp_2", model="ccs-model", status="completed",
                           output=[item], usage=usage)


def _adapter(client: _FakeClient) -> OpenAIResponsesAdapter:
    spec = EndpointSpec(
        name="qwen_proxy", kind="openai_responses",
        base_url="http://127.0.0.1:15721/v1", model="ccs-ai/deepseek-v4.1-flash-1017",
        api_key="local-proxy", capabilities=Capabilities(tools=True, vision=True),
    )
    return OpenAIResponsesAdapter(spec, client_factory=lambda: client)


_USER = [{"role": "user", "content": "hi"}]


def test_translates_messages_to_responses_items() -> None:
    items = to_responses_input([
        {"role": "system", "content": "短话"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在",
         "tool_calls": [{"id": "call_9", "function": {"name": "say", "arguments": '{"text": "嗯"}'}}]},
        {"role": "tool", "tool_call_id": "call_9", "content": "ok"},
    ])
    assert items[0] == {"type": "message", "role": "system",
                        "content": [{"type": "input_text", "text": "短话"}]}
    assert items[1]["role"] == "user"
    assert items[2] == {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "在"}]}
    assert items[3] == {"type": "function_call", "name": "say",
                        "arguments": '{"text": "嗯"}', "call_id": "call_9"}
    assert items[4] == {"type": "function_call_output", "call_id": "call_9", "output": "ok"}


def test_translates_tools_and_tool_choice() -> None:
    tools = [{"type": "function", "function": {
        "name": "say", "description": "说一句", "parameters": {"type": "object", "properties": {}},
    }}]
    assert to_responses_tools(tools) == [{
        "type": "function", "name": "say", "description": "说一句",
        "parameters": {"type": "object", "properties": {}},
    }]
    forced = {"type": "function", "function": {"name": "say"}}
    assert to_responses_tool_choice(forced) == {"type": "function", "name": "say"}
    assert to_responses_tool_choice("auto") == "auto"
    assert to_responses_tool_choice(None) == "auto"


def test_chat_returns_text_and_maps_usage() -> None:
    client = _FakeClient(_text_response("hello"))
    result = asyncio.run(_adapter(client).chat(
        model="ccs-model", messages=_USER, temperature=0.2, max_tokens=64, timeout=3,
    ))
    assert result.text == "hello"
    assert result.finish_reason == "completed"
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 5
    assert result.usage.total_tokens == 15
    assert result.usage.cache_hit_tokens == 4
    assert result.usage.cache_miss_tokens == 6
    sent = client.calls[0]
    assert sent["model"] == "ccs-model"
    assert sent["max_output_tokens"] == 64
    assert sent["timeout"] == 3
    assert sent["input"][0]["content"][0]["text"] == "hi"


def test_chat_tools_parses_function_call_and_forces_say() -> None:
    client = _FakeClient(_tool_response(arguments='{"text": "收到了"}'))
    result = asyncio.run(_adapter(client).chat_tools(
        model="ccs-model", messages=_USER,
        tools=[{"type": "function", "function": {"name": "say"}}],
        tool_choice={"type": "function", "function": {"name": "say"}}, timeout=3,
    ))
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == ({"id": "call_1", "name": "say",
                                  "arguments": '{"text": "收到了"}'},)
    assert client.calls[0]["tool_choice"] == {"type": "function", "name": "say"}
    assert client.calls[0]["tools"][0]["name"] == "say"


def test_error_maps_to_provider_error() -> None:
    client = _FakeClient(rate_limit_error())
    try:
        asyncio.run(_adapter(client).chat(
            model="m", messages=_USER, temperature=0, max_tokens=8, timeout=3,
        ))
    except ProviderError as exc:
        assert exc.code == "rate_limit"
    else:
        raise AssertionError("rate limit must raise ProviderError")


def test_vision_sends_input_image() -> None:
    client = _FakeClient(_text_response("蓝色"))
    url = "data:image/png;base64,AAAA"
    result = asyncio.run(_adapter(client).chat_vision(
        model="ccs-model", messages=_USER, images=[url], max_tokens=64, timeout=3,
    ))
    assert result.text == "蓝色"
    parts = client.calls[0]["input"][0]["content"]
    assert parts[0] == {"type": "input_text", "text": "hi"}
    assert parts[1] == {"type": "input_image", "image_url": url}


def test_stream_falls_back_to_single_chunk() -> None:
    client = _FakeClient(_text_response("双"))
    async def _run():
        return [chunk async for chunk in _adapter(client).stream(
            model="m", messages=_USER, temperature=0.1, max_tokens=16,
        )]
    chunks = asyncio.run(_run())
    assert len(chunks) == 1
    assert chunks[0].text == "双"
    assert chunks[0].finish_reason == "stop"


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