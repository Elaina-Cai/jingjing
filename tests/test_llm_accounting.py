"""Accounting guards: unified llm_call (legacy event names removed in WP4).

Run directly:
    python tests/test_llm_accounting.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = Path(__file__).resolve().parent.parent

_TEST_LOG_DIR = str(Path("data") / "_test_logs_llm_accounting")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_DIR", _TEST_LOG_DIR)
_ISO_ROOT = (ROOT / "data" / "_llm_accounting_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))

from llm.adapters.base import Usage  # noqa: E402
from llm.meter import (  # noqa: E402
    Charge,
    PriceRule,
    PriceSchedule,
    PriceSnapshot,
    charge_for_usage,
    compute_cost,
    price_snapshot_from_dict,
)


class _CapturedLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def _emit(self, level: str, event, **kw) -> None:
        self.events.append({"level": level, "event": event, **kw})

    def info(self, event=None, **kw):
        self._emit("info", event, **kw)

    def warning(self, event=None, **kw):
        self._emit("warning", event, **kw)

    def error(self, event=None, **kw):
        self._emit("error", event, **kw)

    def debug(self, event=None, **kw):
        self._emit("debug", event, **kw)


def _patch_logs():
    import llm.client as client_mod
    import llm.meter as meter_mod

    cap = _CapturedLog()
    state = (client_mod, meter_mod, client_mod.log, meter_mod.log)
    client_mod.log = cap
    meter_mod.log = cap
    return cap, state


def _restore_logs(state) -> None:
    client_mod, meter_mod, old_client_log, old_meter_log = state
    client_mod.log = old_client_log
    meter_mod.log = old_meter_log


class _Usage:
    def __init__(self, prompt=3, completion=5):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion


class _Function:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _Call:
    def __init__(self, cid, name, arguments):
        self.id, self.function = cid, _Function(name, arguments)


class _Message:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _Choice:
    def __init__(self, message):
        self.message = message


class _Response:
    def __init__(self, message, usage=None):
        self.choices = [_Choice(message)]
        self.usage = usage


class _Delta:
    def __init__(self, content):
        self.content = content


class _StreamChoice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, content):
        self.choices = [_StreamChoice(_Delta(content))] if content is not None else []


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._i]
        self._i += 1
        return chunk


class _FakeCompletions:
    def __init__(self, responses=None, stream_chunks=None):
        self._responses = list(responses or [])
        self._stream_chunks = list(stream_chunks or [])
        self.requests = 0
        self.calls = []

    async def create(self, **kwargs):
        self.requests += 1
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return _FakeStream(self._stream_chunks)
        return self._responses.pop(0)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeOpenAI:
    def __init__(self, responses=None, stream_chunks=None):
        self.completions = _FakeCompletions(responses, stream_chunks)
        self.chat = _FakeChat(self.completions)

def _client_with(fake):
    from llm.client import LLMClient

    client = LLMClient()
    client._get_client = lambda provider: fake
    client._get_model = lambda provider: "fake-model"
    client._filter_usable_providers = lambda chain, vision_mode=False: (list(chain), [])
    client._circuit.allow = lambda name: True
    client._budget_used = 0
    client._daily_budget = 0
    client._per_agent_budget = 0
    client._agent_usage = {}
    return client


def _unified(cap):
    return [e for e in cap.events if e.get("event") == "llm_call"]


def test_tools_call_logs_duration_and_unified_event() -> None:
    good = json.dumps({"q": "weather"})
    fake = _FakeOpenAI([
        _Response(_Message(tool_calls=[_Call("c1", "search", good)]), _Usage()),
    ])
    cap, state = _patch_logs()
    try:
        client = _client_with(fake)
        result = asyncio.run(client.chat_tools(
            system="s", user="u", tools=[{"type": "function"}], agent_name="t",
        ))
    finally:
        _restore_logs(state)

    assert result.get("error") is None, result
    assert result["tool_calls"][0]["arguments"] == {"q": "weather"}, result
    unified = [e for e in _unified(cap) if e.get("call_kind") == "tools"]
    assert len(unified) == 1, cap.events
    ev = unified[0]
    assert isinstance(ev["duration_ms"], int) and ev["duration_ms"] >= 0, ev
    assert {
        key: ev["tokens"].get(key) for key in ("prompt", "completion", "total")
    } == {"prompt": 3, "completion": 5, "total": 8}, ev
    assert ev["attempt"] == 0 and ev["provider"], ev


def test_stream_without_usage_logs_none_not_estimate() -> None:
    fake = _FakeOpenAI(stream_chunks=[_Chunk("你"), _Chunk("好")])
    cap, state = _patch_logs()
    try:
        client = _client_with(fake)

        async def _collect():
            out = []
            async for delta in client.stream_chat(system="s", user="u", agent_name="t"):
                out.append(delta)
            return out

        out = asyncio.run(_collect())
    finally:
        _restore_logs(state)

    assert "".join(out) == "你好", out
    unified = [e for e in _unified(cap) if e.get("call_kind") == "stream"]
    assert len(unified) == 1, cap.events
    ev = unified[0]
    assert ev["tokens"] is None, ev
    assert isinstance(ev["duration_ms"], int) and ev["duration_ms"] >= 0, ev
    assert client._budget_used == 0, client._budget_used
    assert client._agent_usage.get("t", 0) == 0, client._agent_usage


def test_budget_exceeded_emits_skipped_event() -> None:
    fake = _FakeOpenAI([_Response(_Message(content="hi"), _Usage())])
    cap, state = _patch_logs()
    try:
        client = _client_with(fake)
        client._daily_budget = 1
        client._budget_used = 1
        out = asyncio.run(client.chat(system="s", user="u", agent_name="t"))
    finally:
        _restore_logs(state)

    assert out == ""
    skipped = [e for e in _unified(cap) if e.get("error") == "budget"]
    assert len(skipped) == 1, cap.events
    assert skipped[0]["skipped"] is True and skipped[0]["tokens"] is None, skipped


def test_secrets_never_logged() -> None:
    import llm.client as client_mod

    entry = client_mod._PROVIDER_REGISTRY.get("deepseek")
    old_key_fn = entry.get("api_key_fn") if entry else None
    if entry is not None:
        entry["api_key_fn"] = lambda: "sk-test-secret"

    fake = _FakeOpenAI([_Response(_Message(content="hi"), _Usage())])
    cap, state = _patch_logs()
    try:
        client = _client_with(fake)
        asyncio.run(client.chat(system="s", user="u", agent_name="t"))
    finally:
        _restore_logs(state)
        if entry is not None and old_key_fn is not None:
            entry["api_key_fn"] = old_key_fn

    blob = json.dumps(cap.events, ensure_ascii=False, default=str)
    assert "sk-test" not in blob, blob
    assert "Bearer " not in blob, blob
    assert "data:image" not in blob, blob



def test_cost_none_without_pricing() -> None:
    usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    assert compute_cost(usage, None) is None
    assert compute_cost(usage, {}) is None
    assert price_snapshot_from_dict(None) is None


def test_cost_input_output_basic() -> None:
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=500_000, total_tokens=1_500_000)
    pricing = {"currency": "CNY", "input_per_mtok": 2.0, "output_per_mtok": 8.0}
    cost = compute_cost(usage, pricing)
    assert cost is not None
    assert cost["currency"] == "CNY"
    assert cost["amount"] == 6.0 and cost["missing"] == [], cost
    meters = {line["meter"]: line["amount"] for line in cost["lines"]}
    assert meters["input"] == 2.0 and meters["output"] == 4.0


def test_cost_cache_discount() -> None:
    usage = Usage(
        prompt_tokens=1000, completion_tokens=0, total_tokens=1000,
        cache_hit_tokens=400, cache_miss_tokens=600,
    )
    pricing = {
        "currency": "USD", "input_per_mtok": 2.0, "output_per_mtok": 8.0,
        "cache_discount": 0.25,
    }
    cost = compute_cost(usage, pricing)
    assert cost is not None and cost["missing"] == [], cost
    expected = (600 * 2.0 + 400 * 0.5) / 1_000_000
    assert abs(cost["amount"] - expected) < 1e-12, cost
    meters = {line["meter"]: line for line in cost["lines"]}
    assert meters["cache_hit"]["tokens"] == 400
    assert meters["cache_hit"]["per_mtok"] == 0.5
    assert meters["input"]["tokens"] == 600


def test_cost_missing_usage_or_price_keeps_null() -> None:
    pricing = {"currency": "CNY", "input_per_mtok": 2.0, "output_per_mtok": 8.0}
    cost = compute_cost(Usage(total_tokens=10), pricing)
    assert cost is not None and cost["amount"] is None, cost
    assert "input_tokens" in cost["missing"] and "output_tokens" in cost["missing"], cost
    cost2 = compute_cost(
        Usage(prompt_tokens=10, completion_tokens=5),
        {"currency": "CNY", "input_per_mtok": 2.0},
    )
    assert cost2 is not None and cost2["amount"] is None, cost2
    assert "output_price" in cost2["missing"], cost2


def test_cost_charge_snapshot_shapes() -> None:
    snapshot = price_snapshot_from_dict({"currency": "CNY", "input_per_mtok": 2.0})
    assert isinstance(snapshot, PriceSnapshot) and snapshot.currency == "CNY"
    schedule = snapshot.table.schedule()
    assert isinstance(schedule, PriceSchedule)
    assert schedule.rule("input") == PriceRule("input", 2.0)
    charge = charge_for_usage(Usage(prompt_tokens=1_000_000, completion_tokens=0), snapshot)
    assert isinstance(charge, Charge)
    assert charge.known_amount is True and charge.amount == 2.0


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
