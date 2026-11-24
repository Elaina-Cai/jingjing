"""Adapter contract suite (WP6).

``run_adapter_contract(adapter, transport)`` drives the reference checks
(chat / tools / stream / usage / timeout passthrough / error mapping /
cancellation).  ``transport`` is the fake client the adapter was built with
(e.g. ``OpenAICompatAdapter(spec, client_factory=lambda: transport)``);
``FakeTransport`` below is the reference implementation.

Third-party adapters must pass ``assert_adapter_contract`` before shipping.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import structlog
from openai import RateLimitError

from llm.adapters.base import (
    Capabilities,
    ChatResult,
    ProviderError,
    StreamChunk,
    ToolsResult,
    Usage,
)
from safelog import warn

try:  # openai 3.x uses the vendored httpx2 name; older uses httpx
    import httpx2 as _httpx
except ImportError:  # pragma: no cover
    warn("llm/adapters/contract.py:32 ImportError", err=repr(__import__("sys").exc_info()[1]))
    import httpx as _httpx

log = structlog.get_logger()

ADAPTER_CONTRACT_VERSION = 1


class AdapterContractError(RuntimeError):
    """Raised by ``assert_adapter_contract`` with the failed check list."""


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


class _FakeCompletions:
    def __init__(self, owner: "FakeTransport") -> None:
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        if not self._owner.queue:
            raise RuntimeError("FakeTransport queue is empty")
        payload = self._owner.queue.pop(0)
        kind = payload["kind"]
        if kind == "error":
            raise payload["error"]
        if kind == "slow":
            await asyncio.sleep(payload.get("seconds", 10))
            return payload.get("response") or text_response("slow")
        if kind == "stream":
            return _FakeStream(payload["payload"])
        return payload["response"]


class ContractTransport:
    """Programming API shared by fake transports used by the contract runner."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.queue: list[dict[str, Any]] = []

    def queue_response(self, response) -> None:
        self.queue.append({"kind": "response", "response": response})

    def queue_error(self, error: BaseException) -> None:
        self.queue.append({"kind": "error", "error": error})

    def queue_stream(self, payload) -> None:
        self.queue.append({"kind": "stream", "payload": payload})

    def queue_slow(self, seconds: float = 10) -> None:
        self.queue.append({"kind": "slow", "seconds": seconds})

    def make_text(self, text: str = "hello", **kwargs):
        raise NotImplementedError

    def make_tools(self, calls=None, **kwargs):
        raise NotImplementedError

    def make_stream(self, **kwargs):
        raise NotImplementedError

    def make_rate_limit_error(self, message: str = "rate limited"):
        raise NotImplementedError


class FakeTransport(ContractTransport):
    """Programmable OpenAI-compatible transport for contract tests."""

    def __init__(self) -> None:
        super().__init__()
        self.chat = SimpleNamespace(completions=_FakeCompletions(self))

    def make_text(self, text: str = "hello", **kwargs):
        return text_response(text, **kwargs)

    def make_tools(self, calls=None, **kwargs):
        return tool_response(calls or [("c1", "search", '{"q": "x"}')], **kwargs)

    def make_stream(self, **kwargs):
        return [
            stream_chunk("a"),
            stream_chunk("b"),
            stream_chunk(usage=usage_fixture(10, 5), finish="stop"),
        ]

    def make_rate_limit_error(self, message: str = "rate limited"):
        return rate_limit_error(message)


def text_response(text: str = "ok", *, prompt_tokens: int | None = 10,
                  completion_tokens: int | None = 5, total_tokens: int | None = None,
                  finish_reason: str = "stop", response_id: str = "fake-1",
                  model: str = "fake-model"):
    total = total_tokens
    if total is None and prompt_tokens is not None and completion_tokens is not None:
        total = prompt_tokens + completion_tokens
    message = SimpleNamespace(content=text, tool_calls=[], reasoning_content=None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        total_tokens=total,
    )
    return SimpleNamespace(choices=[choice], usage=usage, id=response_id, model=model)


def tool_response(calls: list[tuple[str, str, str]], *, text: str = ""):
    tool_calls = [
        SimpleNamespace(
            id=call_id,
            type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        )
        for call_id, name, arguments in calls
    ]
    message = SimpleNamespace(content=text, tool_calls=tool_calls, reasoning_content=None)
    choice = SimpleNamespace(message=message, finish_reason="tool_calls")
    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        id="fake-tools-1",
        model="fake-model",
    )


def stream_chunk(text: str | None = None, *, usage: Any = None, finish: str | None = None):
    delta = SimpleNamespace(content=text)
    choices = (
        [SimpleNamespace(delta=delta, finish_reason=finish)]
        if (text is not None or finish)
        else []
    )
    return SimpleNamespace(choices=choices, usage=usage)


def usage_fixture(prompt: int = 10, completion: int = 5):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                           total_tokens=prompt + completion)


def rate_limit_error(message: str = "rate limited") -> RateLimitError:
    request = _httpx.Request("POST", "https://contract.test/v1/chat/completions")
    response = _httpx.Response(429, request=request, json={"error": {"message": message}})
    return RateLimitError(message, response=response, body=None)

def _collect_stream(adapter, model: str) -> list[StreamChunk]:
    async def _run():
        return [
            chunk async for chunk in adapter.stream(
                model=model, messages=[{"role": "user", "content": "hi"}],
                temperature=0.1, max_tokens=16, timeout=3,
            )
        ]

    return asyncio.run(_run())


def _cancel_probe(adapter, model: str) -> bool:
    async def _run() -> bool:
        task = asyncio.create_task(adapter.chat(
            model=model, messages=[{"role": "user", "content": "hi"}],
            temperature=0.1, max_tokens=8, timeout=10,
        ))
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            warn("llm/adapters/contract.py:213 asyncio.CancelledError", err=repr(__import__("sys").exc_info()[1]))
            return True
        except BaseException:  # noqa: BLE001
            warn("llm/adapters/contract.py:215 BaseException", err=repr(__import__("sys").exc_info()[1]))
            return False
        return False

    return asyncio.run(_run())


def run_adapter_contract(adapter, transport, *, model: str = "contract-model") -> dict[str, Any]:
    """Run the mandatory checks; returns ``{ok, version, checks}`` (never raises)."""
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})

    try:
        caps = adapter.capabilities(model)
        check("capabilities", isinstance(caps, Capabilities), repr(caps)[:120])
    except Exception as exc:  # noqa: BLE001
        warn("llm/adapters/contract.py:232 Exception", err=repr(exc))
        check("capabilities", False, f"{type(exc).__name__}: {exc}")

    try:
        transport.queue_response(transport.make_text("hello"))
        result = asyncio.run(adapter.chat(
            model=model, messages=[{"role": "user", "content": "hi"}],
            temperature=0.1, max_tokens=16, timeout=3,
        ))
        check("chat_returns_chat_result", isinstance(result, ChatResult), type(result).__name__)
        check("chat_text", getattr(result, "text", None) == "hello", getattr(result, "text", None))
        usage = getattr(result, "usage", None)
        check("usage_reported", isinstance(usage, Usage) and usage.resolved_total() == 15, usage)
        check("timeout_passed",
              bool(transport.calls) and transport.calls[-1].get("timeout") == 3,
              transport.calls[-1].get("timeout") if transport.calls else "no call")
    except Exception as exc:  # noqa: BLE001
        warn("llm/adapters/contract.py:248 Exception", err=repr(exc))
        check("chat_returns_chat_result", False, f"{type(exc).__name__}: {exc}")

    try:
        transport.queue_response(transport.make_tools([("c1", "search", '{"q": "x"}')]))
        tools = [{"type": "function", "function": {"name": "search"}}]
        result = asyncio.run(adapter.chat_tools(
            model=model, messages=[{"role": "user", "content": "hi"}],
            tools=tools, timeout=3,
        ))
        calls = getattr(result, "tool_calls", ())
        check("tools_returns_tools_result", isinstance(result, ToolsResult), type(result).__name__)
        check("tools_preserve_raw_arguments",
              bool(calls) and calls[0].get("arguments") == '{"q": "x"}', calls[:1])
    except Exception as exc:  # noqa: BLE001
        warn("llm/adapters/contract.py:262 Exception", err=repr(exc))
        check("tools_returns_tools_result", False, f"{type(exc).__name__}: {exc}")

    try:
        transport.queue_stream(transport.make_stream())
        chunks = _collect_stream(adapter, model)
        text = "".join(chunk.text for chunk in chunks)
        final = chunks[-1] if chunks else None
        check("stream_yields_chunks", isinstance(final, StreamChunk), len(chunks))
        check("stream_text", text == "ab", text)
        check("stream_usage_reported",
              final is not None and final.usage is not None
              and final.usage.resolved_total() == 15,
              getattr(final, "usage", None))
    except Exception as exc:  # noqa: BLE001
        warn("llm/adapters/contract.py:276 Exception", err=repr(exc))
        check("stream_yields_chunks", False, f"{type(exc).__name__}: {exc}")

    try:
        transport.queue_error(transport.make_rate_limit_error())
        try:
            asyncio.run(adapter.chat(
                model=model, messages=[{"role": "user", "content": "hi"}],
                temperature=0.1, max_tokens=8, timeout=3,
            ))
            check("error_mapping", False, "no exception raised")
        except ProviderError as exc:
            warn("llm/adapters/contract.py:287 ProviderError", err=repr(exc))
            check("error_mapping", exc.code == "rate_limit", exc.code)
    except Exception as exc:  # noqa: BLE001
        warn("llm/adapters/contract.py:289 Exception", err=repr(exc))
        check("error_mapping", False, f"{type(exc).__name__}: {exc}")

    try:
        transport.queue_slow(10)
        check("cancellation_propagates", _cancel_probe(adapter, model), "CancelledError")
    except Exception as exc:  # noqa: BLE001
        warn("llm/adapters/contract.py:295 Exception", err=repr(exc))
        check("cancellation_propagates", False, f"{type(exc).__name__}: {exc}")

    return {
        "ok": all(item["ok"] for item in checks),
        "version": ADAPTER_CONTRACT_VERSION,
        "checks": checks,
    }


def assert_adapter_contract(adapter, transport, *, model: str = "contract-model") -> dict[str, Any]:
    """Raise :class:`AdapterContractError` unless every contract check passed."""
    report = run_adapter_contract(adapter, transport, model=model)
    if not report["ok"]:
        failed = [item["name"] for item in report["checks"] if not item["ok"]]
        name = getattr(adapter, "name", type(adapter).__name__)
        raise AdapterContractError(
            f"适配器 {name!r} 未通过契约 v{ADAPTER_CONTRACT_VERSION}；失败项: {failed}"
        )
    return report


__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "AdapterContractError",
    "FakeTransport",
    "assert_adapter_contract",
    "rate_limit_error",
    "run_adapter_contract",
    "stream_chunk",
    "text_response",
    "tool_response",
    "usage_fixture",
]
