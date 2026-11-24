"""WP7 Anthropic adapter contract + thinking/replay/tool roundtrip (no network).

Run directly:
    python tests/test_llm_anthropic.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402

from llm.adapters import (  # noqa: E402
    ADAPTER_CONTRACT_VERSION,
    ContractTransport,
    get_adapter_class,
    known_kinds,
    run_adapter_contract,
)
from llm.adapters.anthropic import AnthropicAdapter  # noqa: E402
from llm.adapters.base import Capabilities  # noqa: E402
from llm.config import EndpointSpec  # noqa: E402

_MSG = [{"role": "user", "content": "hi"}]


class _AnthropicTransport(ContractTransport):
    """Programmable Anthropic Messages transport (records payloads)."""

    def make_text(self, text: str = "hello", *, prompt: int = 10, completion: int = 5,
                  stop: str = "end_turn", cache_read: int | None = None,
                  cache_write: int | None = None):
        usage = {"input_tokens": prompt, "output_tokens": completion}
        if cache_read is not None:
            usage["cache_read_input_tokens"] = cache_read
        if cache_write is not None:
            usage["cache_creation_input_tokens"] = cache_write
        return {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop, "usage": usage,
        }

    def make_tools(self, calls=None, **kwargs):
        calls = calls or [("toolu_1", "search", '{"q": "x"}')]
        return {
            "id": "msg_tools", "type": "message", "role": "assistant", "model": "claude-x",
            "content": [
                {"type": "tool_use", "id": cid, "name": name, "input": json.loads(args)}
                for cid, name, args in calls
            ],
            "stop_reason": "tool_use", "usage": {"input_tokens": 3, "output_tokens": 2},
        }

    def make_stream(self, **kwargs):
        return [
            {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "a"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "b"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]

    def make_rate_limit_error(self, message: str = "rate limited"):
        request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
        response = httpx.Response(429, request=request, json={"error": {"message": message}})
        return httpx.HTTPStatusError(message, request=request, response=response)

    async def send(self, payload, *, stream: bool = False, timeout: float | None = None):
        self.calls.append({"payload": payload, "stream": stream, "timeout": timeout})
        if not self.queue:
            raise RuntimeError("transport queue is empty")
        item = self.queue.pop(0)
        kind = item["kind"]
        if kind == "error":
            raise item["error"]
        if kind == "slow":
            await asyncio.sleep(item.get("seconds", 10))
            return self.make_text("slow")
        if kind == "stream":
            return _aiter(item["payload"])
        return item["response"]


async def _aiter(items):
    for item in items:
        yield item


def _spec(**caps) -> EndpointSpec:
    defaults = dict(
        tools=True, vision=True, json_mode=True, streaming=True,
        stream_usage=True, context_window=200000,
    )
    defaults.update(caps)
    return EndpointSpec(
        name="claude",
        kind="anthropic",
        base_url="https://api.anthropic.test/v1",
        model="claude-x",
        api_key="k",
        capabilities=Capabilities(**defaults),
    )


def _adapter(transport, **kwargs) -> AnthropicAdapter:
    return AnthropicAdapter(_spec(), transport=transport, **kwargs)

def _chat(adapter, transport, **kwargs):
    transport.queue_response(transport.make_text())
    return asyncio.run(adapter.chat(
        model="claude-x", messages=_MSG, temperature=0.1, max_tokens=64, **kwargs,
    ))


def test_anthropic_passes_contract() -> None:
    transport = _AnthropicTransport()
    adapter = _adapter(transport)
    report = run_adapter_contract(adapter, transport, model="claude-x")
    failed = [item for item in report["checks"] if not item["ok"]]
    assert report["ok"], failed
    assert report["version"] == ADAPTER_CONTRACT_VERSION


def test_anthropic_kind_registered() -> None:
    assert "anthropic" in known_kinds()
    assert get_adapter_class("anthropic") is AnthropicAdapter


def test_thinking_toggle() -> None:
    transport = _AnthropicTransport()
    adapter = _adapter(transport)

    _chat(adapter, transport, thinking_budget=2048)
    assert transport.calls[-1]["payload"]["thinking"] == {"type": "enabled", "budget_tokens": 2048}

    _chat(adapter, transport, reasoning_effort="high")
    assert transport.calls[-1]["payload"]["thinking"]["budget_tokens"] == 16384

    _chat(adapter, transport, reasoning_effort="low")
    assert transport.calls[-1]["payload"]["thinking"]["budget_tokens"] == 1024

    _chat(adapter, transport, thinking_budget=100)
    assert transport.calls[-1]["payload"]["thinking"]["budget_tokens"] == 1024

    _chat(adapter, transport)
    assert "thinking" not in transport.calls[-1]["payload"]


def test_system_top_level_and_max_tokens() -> None:
    transport = _AnthropicTransport()
    adapter = _adapter(transport)
    transport.queue_response(transport.make_text())
    asyncio.run(adapter.chat(
        model="claude-x",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        temperature=0.1, max_tokens=7,
    ))
    payload = transport.calls[-1]["payload"]
    assert payload["system"] == "sys"
    assert payload["max_tokens"] == 7
    assert [message["role"] for message in payload["messages"]] == ["user"]


def test_reasoning_replay_encrypted_source_match() -> None:
    transport = _AnthropicTransport()
    adapter = _adapter(transport)
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "reasoning_blocks": [
            {"text": "t1", "signature": "sig-1", "source": "claude-x"},
            {"text": "t2", "encrypted": "enc-1", "source": "claude-x"},
            {"text": "t3", "signature": "sig-3", "source": "other-model"},
        ]},
        {"role": "user", "content": "next"},
    ]
    transport.queue_response(transport.make_text())
    asyncio.run(adapter.chat(model="claude-x", messages=messages, temperature=0.1, max_tokens=64))
    payload = transport.calls[-1]["payload"]
    assistant = [message for message in payload["messages"] if message["role"] == "assistant"][0]
    blocks = assistant["content"]
    assert [block["type"] for block in blocks] == ["text", "thinking", "redacted_thinking"]
    assert blocks[1]["signature"] == "sig-1" and blocks[1]["thinking"] == "t1"
    assert blocks[2]["data"] == "enc-1"


def test_reasoning_replay_plaintext() -> None:
    transport = _AnthropicTransport()
    adapter = _adapter(transport, reasoning_replay="plaintext")
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "reasoning_blocks": [
            {"text": "t1", "signature": "sig-1", "source": "other-model"},
        ]},
        {"role": "user", "content": "next"},
    ]
    transport.queue_response(transport.make_text())
    asyncio.run(adapter.chat(model="claude-x", messages=messages, temperature=0.1, max_tokens=64))
    assistant = [
        message for message in transport.calls[-1]["payload"]["messages"]
        if message["role"] == "assistant"
    ][0]
    assert any(block.get("type") == "thinking" and block.get("thinking") == "t1"
               for block in assistant["content"])


def test_tool_roundtrip_reconstructible() -> None:
    transport = _AnthropicTransport()
    adapter = _adapter(transport)
    tools = [{
        "type": "function",
        "function": {
            "name": "search",
            "description": "s",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }]
    transport.queue_response(transport.make_tools([("toolu_1", "search", '{"q": "x"}')]))
    result = asyncio.run(adapter.chat_tools(
        model="claude-x", messages=_MSG, tools=tools, temperature=0.1, max_tokens=64,
    ))
    assert result.tool_calls == ({
        "id": "toolu_1",
        "name": "search",
        "arguments": json.dumps({"q": "x"}, ensure_ascii=False),
    },)
    assert result.finish_reason == "tool_calls"
    payload = transport.calls[-1]["payload"]
    assert payload["tools"] == [{
        "name": "search",
        "description": "s",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]
    assert payload["tool_choice"] == {"type": "auto"}

    replay_messages = [
        {"role": "user", "content": "find"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "toolu_1", "name": "search", "arguments": '{"q": "x"}'},
        ]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "result text"},
    ]
    transport.queue_response(transport.make_text("done"))
    asyncio.run(adapter.chat(
        model="claude-x", messages=replay_messages, temperature=0.1, max_tokens=64,
    ))
    replay = transport.calls[-1]["payload"]
    assert [message["role"] for message in replay["messages"]] == ["user", "assistant", "user"]
    assistant = replay["messages"][1]
    tool_use = [block for block in assistant["content"] if block.get("type") == "tool_use"][0]
    assert tool_use["id"] == "toolu_1" and tool_use["input"] == {"q": "x"}
    tool_result = replay["messages"][2]["content"][0]
    assert tool_result == {
        "type": "tool_result", "tool_use_id": "toolu_1", "content": "result text",
    }


def test_stream_maps_sse_and_usage() -> None:
    transport = _AnthropicTransport()
    adapter = _adapter(transport)
    transport.queue_stream(transport.make_stream())

    async def _collect():
        return [chunk async for chunk in adapter.stream(model="claude-x", messages=_MSG)]

    chunks = asyncio.run(_collect())
    assert "".join(chunk.text for chunk in chunks) == "ab"
    final = chunks[-1]
    assert final.usage is not None and final.usage.resolved_total() == 15
    assert final.finish_reason == "stop"
    assert transport.calls[-1]["stream"] is True


def test_cache_control_and_cache_usage() -> None:
    transport = _AnthropicTransport()
    adapter = _adapter(transport, cache_control=True)
    transport.queue_response(transport.make_text(cache_read=4, cache_write=2))
    result = asyncio.run(adapter.chat(
        model="claude-x",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        temperature=0.1, max_tokens=64,
    ))
    payload = transport.calls[-1]["payload"]
    assert payload["system"] == [{
        "type": "text", "text": "sys", "cache_control": {"type": "ephemeral"},
    }]
    assert result.usage.prompt_tokens == 16
    assert result.usage.cache_hit_tokens == 4
    assert result.usage.cache_miss_tokens == 12
    assert result.usage.total_tokens == 21


def test_finish_reason_mapping() -> None:
    transport = _AnthropicTransport()
    adapter = _adapter(transport)
    for raw, expected in (("end_turn", "stop"), ("max_tokens", "length"), ("tool_use", "tool_calls")):
        transport.queue_response(transport.make_text(text="x", stop=raw))
        result = asyncio.run(adapter.chat(
            model="claude-x", messages=_MSG, temperature=0.1, max_tokens=8,
        ))
        assert result.finish_reason == expected, raw


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
