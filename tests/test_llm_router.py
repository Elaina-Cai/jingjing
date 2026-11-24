"""WP3 LLMRouter guards: routing / chain / retry / budget / stream (§4.3).

Run directly:
    python tests/test_llm_router.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.adapters.base import (  # noqa: E402
    Capabilities,
    ChatResult,
    ProviderError,
    StreamChunk,
    ToolsResult,
    Usage,
)
from llm.config import AgentRoute, EndpointSpec, LLMConfig  # noqa: E402
from llm.router import LLMRouter, estimate_tokens  # noqa: E402


class _FakeAdapter:
    name = "fake"

    def __init__(self, name, *, outcomes=None, stream_steps=None, capabilities=None):
        self.name = name
        self.outcomes = list(outcomes or [])
        self.stream_steps = list(stream_steps or [])
        self.capabilities_obj = capabilities or Capabilities(
            tools=True, vision=True, streaming=True, json_mode=True, context_window=100000,
        )
        self.calls: list[tuple[str, dict]] = []
        self.refresh_count = 0

    def capabilities(self, model):
        return self.capabilities_obj

    def _next(self, kind, kwargs):
        self.calls.append((kind, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def chat(self, **kwargs):
        return self._next("chat", kwargs)

    async def chat_tools(self, **kwargs):
        return self._next("tools", kwargs)

    async def chat_vision(self, **kwargs):
        return self._next("vision", kwargs)

    async def stream(self, **kwargs):
        self.calls.append(("stream", kwargs))
        for step in self.stream_steps:
            if isinstance(step, Exception):
                raise step
            yield step

    def refresh_credentials(self):
        self.refresh_count += 1


class _Breaker:
    def __init__(self, open_names=()):
        self.open_names = set(open_names)
        self.allowed: list[str] = []
        self.successes: list[str] = []
        self.failures: list[str] = []

    def is_open(self, name):
        return name in self.open_names

    def allow(self, name):
        self.allowed.append(name)
        return name not in self.open_names

    def record_success(self, name):
        self.successes.append(name)

    def record_failure(self, name):
        self.failures.append(name)


class _Budget:
    def __init__(self, exceeded=False):
        self.exceeded = exceeded
        self.used: list[tuple[str, int | None]] = []

    def budget_exceeded(self, agent):
        return self.exceeded

    def record_usage(self, agent, tokens):
        self.used.append((agent, tokens))


class _Capture:
    def __init__(self):
        self.events: list[dict] = []

    def info(self, event=None, **kw):
        self.events.append({"event": event, **kw})

    def warning(self, event=None, **kw):
        self.events.append({"event": event, **kw})

    def error(self, event=None, **kw):
        self.events.append({"event": event, **kw})

    def debug(self, event=None, **kw):
        self.events.append({"event": event, **kw})


def _endpoint(name, *, model=None, api_key="k", capabilities=None, pricing=None):
    return EndpointSpec(
        name=name,
        kind="openai_compat",
        base_url="https://fake.test/v1",
        model=model or name,
        api_key=api_key,
        capabilities=capabilities or Capabilities(
            tools=True, vision=True, streaming=True, json_mode=True, context_window=100000,
        ),
        pricing=pricing,
    )


def _config(endpoints, *, default, fallback=(), agents=None, kind_lead=None,
            max_retries=0, on_missing="downgrade", retry_on_rate_limit=False):
    return LLMConfig(
        endpoints={endpoint.name: endpoint for endpoint in endpoints},
        default_endpoint=default,
        agent_endpoints=agents or {},
        fallback=tuple(fallback),
        on_missing_capability=on_missing,
        max_retries=max_retries,
        retry_on_rate_limit=retry_on_rate_limit,
        kind_lead=kind_lead or {},
        budgets={},
    )


def _chat_ok(text="ok"):
    return ChatResult(
        text=text,
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        raw_meta={},
    )


def _tools_ok():
    return ToolsResult(text="", usage=Usage(), raw_meta={}, tool_calls=())


def _capture():
    import llm.meter as meter_mod

    cap = _Capture()
    old = meter_mod.log
    meter_mod.log = cap
    return cap, (meter_mod, old)


def _restore(state):
    meter_mod, old = state
    meter_mod.log = old

def _router(adapters, config, **kwargs):
    return LLMRouter(config=config, adapters=adapters, **kwargs)


_USER_MSG = [{"role": "user", "content": "hello"}]


def test_resolution_is_deterministic() -> None:
    cfg = _config(
        [_endpoint("a"), _endpoint("b")],
        default="a",
        agents={"conversational": AgentRoute("b")},
    )
    router = _router({"a": _FakeAdapter("a"), "b": _FakeAdapter("b")}, cfg)
    first = router.for_agent("conversational", "chat", "chat")
    second = router.for_agent("conversational", "chat", "chat")
    assert (first.endpoint, first.source) == ("b", "agent")
    assert (second.endpoint, second.source) == ("b", "agent")


def test_agent_override_beats_default() -> None:
    cfg = _config(
        [_endpoint("a"), _endpoint("b")],
        default="a",
        agents={"reply_intent": AgentRoute("b")},
    )
    router = _router({"a": _FakeAdapter("a"), "b": _FakeAdapter("b")}, cfg)
    route = router.for_agent("reply_intent", "chat", "chat")
    assert route.endpoint == "b" and route.source == "agent"


def test_task_override_beats_agent() -> None:
    cfg = _config(
        [_endpoint("a"), _endpoint("b")],
        default="a",
        agents={
            "conversational": AgentRoute("b"),
            "conversational:tools": AgentRoute("a"),
        },
    )
    router = _router({"a": _FakeAdapter("a"), "b": _FakeAdapter("b")}, cfg)
    route = router.for_agent("conversational", "tools", "tools")
    assert route.endpoint == "a" and route.source == "task"


def test_fast_chat_only_chat_and_breaker_semantics() -> None:
    fast = AgentRoute(endpoint="ollama", only_kind="chat", on_circuit_open="fallback")
    cfg = _config(
        [_endpoint("ollama"), _endpoint("a"), _endpoint("b")],
        default="a",
        fallback=("a", "b"),
        agents={"fast_chat": fast, "fast_tool_format": fast},
    )
    breaker = _Breaker()
    adapters = {
        "ollama": _FakeAdapter("ollama", outcomes=[_chat_ok("local")]),
        "a": _FakeAdapter("a", outcomes=[_chat_ok("a")]),
        "b": _FakeAdapter("b"),
    }
    router = _router(adapters, cfg, breaker=breaker)
    assert router.for_agent("fast_chat", "tools", "tools").endpoint == "a"
    route = router.for_agent("fast_chat", "chat", "chat")
    assert router.build_chain(route, "chat", {}) == ["ollama"]
    assert breaker.allowed == []
    result = asyncio.run(router.call("chat", agent="fast_chat", messages=_USER_MSG))
    assert result.text == "local"
    assert breaker.allowed == ["ollama"]

    open_breaker = _Breaker(open_names={"ollama"})
    router2 = _router(
        {
            "ollama": _FakeAdapter("ollama"),
            "a": _FakeAdapter("a", outcomes=[_chat_ok("a")]),
            "b": _FakeAdapter("b"),
        },
        cfg,
        breaker=open_breaker,
    )
    chain = router2.build_chain(router2.for_agent("fast_chat", "chat", "chat"), "chat", {})
    assert chain == ["a", "b"]


def test_vision_lead_reaches_chain() -> None:
    cfg = _config(
        [_endpoint("dash"), _endpoint("a")],
        default="a",
        fallback=("a",),
        kind_lead={"vision": ("dash",)},
    )
    adapters = {"dash": _FakeAdapter("dash", outcomes=[_chat_ok("seen")]), "a": _FakeAdapter("a")}
    router = _router(adapters, cfg)
    chain = router.build_chain(router.for_agent("user", "vision", "vision"), "vision", {"images": []})
    assert chain[0] == "dash"
    result = asyncio.run(router.call(
        "vision", agent="user", messages=_USER_MSG, images=["data:image/png;base64,AAAA"],
    ))
    assert result.text == "seen"


def test_chain_exhaustion_preserves_last_error() -> None:
    cfg = _config([_endpoint("a"), _endpoint("b")], default="a", fallback=("b",))
    router = _router(
        {
            "a": _FakeAdapter("a", outcomes=[ProviderError("auth", "bad key")]),
            "b": _FakeAdapter("b", outcomes=[ProviderError("auth", "bad key")]),
        },
        cfg,
    )
    try:
        asyncio.run(router.call("chat", agent="user", messages=_USER_MSG))
    except ProviderError as exc:
        assert exc.code == "auth", exc.code
    else:
        raise AssertionError("auth exhaustion must raise the real code")

    caps = Capabilities(tools=False, vision=True, streaming=True)
    cfg2 = _config(
        [_endpoint("a", capabilities=caps), _endpoint("b", capabilities=caps)],
        default="a",
        fallback=("b",),
    )
    router2 = _router(
        {"a": _FakeAdapter("a", capabilities=caps), "b": _FakeAdapter("b", capabilities=caps)},
        cfg2,
    )
    try:
        asyncio.run(router2.call("tools", agent="user", messages=_USER_MSG, tools=[{}]))
    except ProviderError as exc:
        assert exc.code == "tool_unsupported", exc.code
    else:
        raise AssertionError("no capable endpoint must raise tool_unsupported")

def test_skip_events_are_emitted() -> None:
    cap, state = _capture()
    try:
        a_caps = Capabilities(tools=False, vision=True, streaming=True)
        a = _FakeAdapter("a", capabilities=a_caps)
        b = _FakeAdapter("b", outcomes=[_tools_ok()])
        cfg = _config(
            [_endpoint("a", capabilities=a_caps), _endpoint("b")],
            default="a",
            fallback=("b",),
        )
        router = _router({"a": a, "b": b}, cfg)
        asyncio.run(router.call("tools", agent="user", messages=_USER_MSG, tools=[{}]))
    finally:
        _restore(state)
    skipped = [
        event for event in cap.events
        if event.get("skipped") and event.get("error") == "tool_unsupported"
    ]
    assert len(skipped) == 1 and skipped[0]["endpoint"] == "a", cap.events
    success = [
        event for event in cap.events
        if event.get("endpoint") == "b" and event.get("error") is None
    ]
    assert success, cap.events


def test_auth_refresh_once() -> None:
    adapter = _FakeAdapter(
        "a", outcomes=[ProviderError("auth", "expired"), _chat_ok("after-refresh")],
    )
    cfg = _config([_endpoint("a")], default="a", max_retries=1)
    router = _router({"a": adapter}, cfg)
    result = asyncio.run(router.call("chat", agent="user", messages=_USER_MSG))
    assert result.text == "after-refresh"
    assert adapter.refresh_count == 1
    assert len(adapter.calls) == 2


def test_stream_budget_check_and_post_chunk_failure() -> None:
    async def _collect(router):
        return [chunk async for chunk in router.stream(agent="user", messages=_USER_MSG)]

    budget_adapter = _FakeAdapter("a", stream_steps=[StreamChunk(text="x")])
    cfg = _config([_endpoint("a")], default="a")
    router = _router({"a": budget_adapter}, cfg, budget_port=_Budget(exceeded=True))
    try:
        asyncio.run(_collect(router))
    except ProviderError as exc:
        assert exc.code == "budget", exc.code
    else:
        raise AssertionError("budget must block stream before the adapter runs")
    assert budget_adapter.calls == []

    a = _FakeAdapter(
        "a", stream_steps=[StreamChunk(text="first"), ProviderError("provider_down", "half")],
    )
    b = _FakeAdapter("b", stream_steps=[StreamChunk(text="nope")])
    cfg2 = _config([_endpoint("a"), _endpoint("b")], default="a", fallback=("b",))
    router2 = _router({"a": a, "b": b}, cfg2)
    out = asyncio.run(_collect(router2))
    assert [chunk.text for chunk in out] == ["first"]
    assert b.calls == []


def test_fallback_is_recorded() -> None:
    cap, state = _capture()
    try:
        a = _FakeAdapter("a", outcomes=[ProviderError("provider_down", "down")])
        b = _FakeAdapter("b", outcomes=[_chat_ok("b")])
        cfg = _config([_endpoint("a"), _endpoint("b")], default="a", fallback=("b",))
        router = _router({"a": a, "b": b}, cfg)
        result = asyncio.run(router.call("chat", agent="user", messages=_USER_MSG))
    finally:
        _restore(state)
    assert result.text == "b"
    successes = [
        event for event in cap.events
        if event.get("event") == "llm_call" and event.get("error") is None
    ]
    assert successes and successes[-1]["endpoint"] == "b"
    assert successes[-1]["fallback_from"] == "a"
    failures = [
        event for event in cap.events
        if event.get("event") == "llm_call" and event.get("error") == "provider_down"
    ]
    assert failures and failures[0]["endpoint"] == "a"


def test_budget_blocks_before_call() -> None:
    cap, state = _capture()
    try:
        adapter = _FakeAdapter("a", outcomes=[_chat_ok()])
        cfg = _config([_endpoint("a")], default="a")
        router = _router({"a": adapter}, cfg, budget_port=_Budget(exceeded=True))
        try:
            asyncio.run(router.call("chat", agent="user", messages=_USER_MSG))
        except ProviderError as exc:
            assert exc.code == "budget", exc.code
        else:
            raise AssertionError("budget must block before any adapter call")
        assert adapter.calls == []
    finally:
        _restore(state)
    assert any(event.get("error") == "budget" and event.get("skipped") for event in cap.events)


def test_circuit_breaker_seams() -> None:
    breaker = _Breaker(open_names={"a"})
    a = _FakeAdapter("a", outcomes=[_chat_ok("a")])
    b = _FakeAdapter("b", outcomes=[_chat_ok("b")])
    cfg = _config([_endpoint("a"), _endpoint("b")], default="a", fallback=("b",))
    router = _router({"a": a, "b": b}, cfg, breaker=breaker)
    result = asyncio.run(router.call("chat", agent="user", messages=_USER_MSG))
    assert result.text == "b" and a.calls == []
    assert breaker.successes == ["b"]

    breaker2 = _Breaker()
    a2 = _FakeAdapter("a", outcomes=[ProviderError("auth", "bad")])
    cfg2 = _config([_endpoint("a")], default="a")
    router2 = _router({"a": a2}, cfg2, breaker=breaker2)
    try:
        asyncio.run(router2.call("chat", agent="user", messages=_USER_MSG))
    except ProviderError:
        pass
    assert breaker2.failures == ["a"]

    cap, state = _capture()
    try:
        router3 = _router(
            {"a": _FakeAdapter("a"), "b": _FakeAdapter("b", outcomes=[_chat_ok("b")])},
            cfg,
            breaker=_Breaker(open_names={"a"}),
        )
        asyncio.run(router3.call("chat", agent="user", messages=_USER_MSG))
    finally:
        _restore(state)
    assert any(event.get("error") == "circuit_open" and event.get("skipped") for event in cap.events)



def test_context_estimate_counts_messages_and_tools() -> None:
    messages = [{"role": "user", "content": "x" * 400}]
    without_tools = estimate_tokens({"messages": messages})
    tools = [{"type": "function", "function": {"name": "search"}, "description": "d" * 80}]
    with_tools = estimate_tokens({"messages": messages, "tools": tools})
    assert without_tools >= 100, without_tools
    assert with_tools > without_tools
    assert estimate_tokens({}) == 0


def test_capability_downgrade_tools_falls_back() -> None:
    a_caps = Capabilities(tools=False, vision=True, streaming=True)
    a = _FakeAdapter("a", capabilities=a_caps)
    b = _FakeAdapter("b", outcomes=[_tools_ok()])
    cfg = _config(
        [_endpoint("a", capabilities=a_caps), _endpoint("b")],
        default="a",
        fallback=("b",),
    )
    router = _router({"a": a, "b": b}, cfg)
    result = asyncio.run(router.call("tools", agent="user", messages=_USER_MSG, tools=[{}]))
    assert isinstance(result, ToolsResult)
    assert a.calls == [] and b.calls and b.calls[0][0] == "tools"


def test_capability_downgrade_vision_uses_placeholder() -> None:
    no_vision = Capabilities(tools=True, vision=False, streaming=True)
    a = _FakeAdapter("a", capabilities=no_vision, outcomes=[_chat_ok("text-only")])
    b = _FakeAdapter("b", capabilities=no_vision)
    cfg = _config(
        [_endpoint("a", capabilities=no_vision), _endpoint("b", capabilities=no_vision)],
        default="a",
        fallback=("b",),
    )
    router = _router({"a": a, "b": b}, cfg)
    cap, state = _capture()
    try:
        result = asyncio.run(router.call(
            "vision", agent="user", messages=_USER_MSG,
            images=["data:image/png;base64,AAAA", "raw-two"],
        ))
    finally:
        _restore(state)

    assert result.text == "text-only"
    assert len(a.calls) == 1
    kind, kwargs = a.calls[0]
    assert kind == "chat", kind
    content = str(kwargs["messages"][-1]["content"])
    assert content.count("[图片]") == 2, content
    skipped = [e for e in cap.events if e.get("skipped")]
    assert len(skipped) == 2, cap.events
    success = [e for e in cap.events if e.get("error") is None]
    assert success and success[0]["call_kind"] == "vision"
    assert success[0]["endpoint"] == "a" and success[0]["fallback_from"] == "b"


def test_capability_downgrade_context_switches_endpoint() -> None:
    small = Capabilities(tools=True, vision=True, streaming=True, context_window=5)
    big = Capabilities(tools=True, vision=True, streaming=True, context_window=100000)
    a = _FakeAdapter("a", capabilities=small)
    b = _FakeAdapter("b", capabilities=big, outcomes=[_chat_ok("big")])
    cfg = _config(
        [_endpoint("a", capabilities=small), _endpoint("b", capabilities=big)],
        default="a",
        fallback=("b",),
    )
    router = _router({"a": a, "b": b}, cfg)
    result = asyncio.run(router.call(
        "chat", agent="user", messages=[{"role": "user", "content": "x" * 400}],
    ))
    assert result.text == "big" and a.calls == []


def test_capability_downgrade_context_overflow_when_all_too_small() -> None:
    small = Capabilities(tools=True, vision=True, streaming=True, context_window=5)
    cfg = _config([_endpoint("a", capabilities=small)], default="a")
    router = _router({"a": _FakeAdapter("a", capabilities=small)}, cfg)
    try:
        asyncio.run(router.call(
            "chat", agent="user", messages=[{"role": "user", "content": "x" * 400}],
        ))
    except ProviderError as exc:
        assert exc.code == "context_overflow", exc.code
    else:
        raise AssertionError("all-too-small context must raise context_overflow")


def test_capability_fail_mode_raises_immediately() -> None:
    cases = [
        ("tools", Capabilities(tools=False, vision=True, streaming=True), "tool_unsupported"),
        ("vision", Capabilities(tools=True, vision=False, streaming=True), "bad_response"),
    ]
    for kind, caps, expected in cases:
        adapter = _FakeAdapter("a", capabilities=caps)
        cfg = _config([_endpoint("a", capabilities=caps)], default="a", on_missing="fail")
        router = _router({"a": adapter}, cfg)
        try:
            asyncio.run(router.call(kind, agent="user", messages=_USER_MSG, tools=[{}], images=[]))
        except ProviderError as exc:
            assert exc.code == expected, (kind, exc.code)
        else:
            raise AssertionError(f"{kind} must fail immediately")
        assert adapter.calls == []

    small = Capabilities(tools=True, vision=True, streaming=True, context_window=5)
    adapter = _FakeAdapter("a", capabilities=small)
    cfg = _config([_endpoint("a", capabilities=small)], default="a", on_missing="fail")
    router = _router({"a": adapter}, cfg)
    try:
        asyncio.run(router.call(
            "chat", agent="user", messages=[{"role": "user", "content": "x" * 400}],
        ))
    except ProviderError as exc:
        assert exc.code == "context_overflow", exc.code
    else:
        raise AssertionError("context fail mode must raise context_overflow")
    assert adapter.calls == []


def test_capability_downgrade_stream_skips_non_streaming() -> None:
    no_stream = Capabilities(tools=True, vision=True, streaming=False)
    a = _FakeAdapter("a", capabilities=no_stream, stream_steps=[StreamChunk(text="nope")])
    b = _FakeAdapter("b", stream_steps=[StreamChunk(text="ok")])
    cfg = _config(
        [_endpoint("a", capabilities=no_stream), _endpoint("b")],
        default="a",
        fallback=("b",),
    )
    router = _router({"a": a, "b": b}, cfg)

    async def _collect():
        return [chunk async for chunk in router.stream(agent="user", messages=_USER_MSG)]

    out = asyncio.run(_collect())
    assert [chunk.text for chunk in out] == ["ok"] and a.calls == []



def test_cost_is_attached_to_event() -> None:
    pricing = {"currency": "CNY", "input_per_mtok": 2.0, "output_per_mtok": 8.0}
    adapter = _FakeAdapter("a", outcomes=[ChatResult(
        text="ok",
        usage=Usage(prompt_tokens=1_000_000, completion_tokens=500_000, total_tokens=1_500_000),
        raw_meta={},
    )])
    cfg = _config([_endpoint("a", pricing=pricing)], default="a")
    router = _router({"a": adapter}, cfg)
    cap, state = _capture()
    try:
        asyncio.run(router.call("chat", agent="user", messages=_USER_MSG))
    finally:
        _restore(state)
    success = [event for event in cap.events if event.get("error") is None]
    assert success, cap.events
    assert success[0]["cost"]["currency"] == "CNY"
    assert success[0]["cost"]["amount"] == 6.0
    assert success[0]["cost"]["missing"] == []


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
