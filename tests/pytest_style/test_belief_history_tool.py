from dataclasses import replace
from types import SimpleNamespace

import pytest

from agents.tools.memory import MemoryRecallTool
from core.context import situation, situation_store as store
from core.context.cognitive_commit import Scope
from core.memory.beliefs import BeliefMemoryProvider
from core.memory import hub as hub_module
from core.terminal.principal import PrincipalContext, ChatType, RelationLevel

SCOPE = Scope("wechat", "a", "same")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("YINYUE_WORLD_MODEL_DIR", "DATA_DIR", "LOG_DIR", "PLATFORMS_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("META_DB_PATH", str(tmp_path / "meta.db"))
    store.reset_world_models()
    configured = hub_module.MemoryHub([BeliefMemoryProvider()])
    monkeypatch.setattr(hub_module, "_hub", configured)
    yield configured
    store.reset_world_models()


def context():
    principal = PrincipalContext("wechat", "a", "same", ChatType.PRIVATE, "person-a", "A", RelationLevel.UNKNOWN)
    return SimpleNamespace(principal=principal, extra={"platform": "wechat", "account_id": "a", "chat_id": "same"})


def seed(kind="time_change"):
    world = store.get_world_model("same", scope=SCOPE)
    situation.assert_fact(world, "address", value="深圳", at="2026-01-01", evidence_refs=("old",))
    situation.assert_fact(world, "address2", value="上海", at="2026-03-01", valid_from="2026-02-01" if kind == "time_change" else "", supersedes="address", change_kind=kind, evidence_refs=("new",))


def execute(tool, **args):
    coroutine = tool.execute(**args)
    try:
        with pytest.raises(StopIteration) as result:
            coroutine.send(None)
        return result.value.value
    finally:
        coroutine.close()


def tool(ctx=None):
    instance = MemoryRecallTool()
    instance.set_context(ctx or context())
    return instance


def test_real_tool_queries_fact_validity_not_record_time():
    seed()
    result = execute(tool(), query="address", valid_at="2026-01-15")
    assert "深圳" in result.output and "上海" not in result.output
    assert "事实有效时间：2026-01-15" in result.output
    assert "记录时间" in result.output
    result = execute(tool(), query="address", kinds="fact", valid_at="2026-02-01")
    assert "上海" in result.output and "深圳" not in result.output
    assert result.data[0]["occurred_at"].startswith("2026-03-01")


def test_historical_query_does_not_restore_corrected_error():
    seed("correction")
    result = execute(tool(), query="address", valid_at="2026-01-15")
    assert "上海" in result.output and "深圳" not in result.output


def test_historical_context_is_local_and_next_call_returns_current():
    seed()
    ctx = context()
    original = dict(ctx.extra)
    instance = tool(ctx)
    assert "深圳" in execute(instance, query="address", valid_at="2026-01-15").output
    assert ctx.extra == original
    result = execute(instance, query="address", kinds="fact")
    assert "上海" in result.output and "深圳" not in result.output
    assert ctx.extra == original


def test_default_query_clears_inherited_history_without_mutating_context():
    seed()
    ctx = context()
    ctx.extra["belief_as_of"] = "2026-01-15"
    assert "上海" in execute(tool(ctx), query="address", kinds="fact").output
    assert ctx.extra["belief_as_of"] == "2026-01-15"


@pytest.mark.parametrize("value", ["bad", "2026-02-30", "2026-01-01T00:00:00+08:00"])
def test_bad_time_is_an_explicit_error(value):
    instance = tool()
    result = execute(instance, query="address", valid_at=value)
    assert result.is_error and "valid_at" in result.error
    assert instance.calls == 0


@pytest.mark.parametrize("kinds", ["episode", "fact,person", "personal", "knowledge", "notes", "fact,fact,fact,fact,fact,episode"])
def test_non_versioned_kinds_are_not_silently_used_as_historical_facts(kinds):
    result = execute(tool(), query="address", valid_at="2026-01-15", kinds=kinds)
    assert result.is_error and "只支持" in result.error


def test_other_stores_are_never_queried_for_valid_at(isolated):
    calls = []
    class Legacy:
        kind = "personal"
        def recall(self, *args, **kwargs):
            calls.append(True)
            return []
    isolated.register(Legacy())
    seed()
    assert "深圳" in execute(tool(), query="address", kinds="fact", valid_at="2026-01-15").output
    assert calls == []


def test_history_query_does_not_widen_scope():
    seed()
    ctx = context()
    ctx.extra["account_id"] = "b"
    result = execute(tool(ctx), query="address", valid_at="2026-01-15")
    assert "深圳" not in result.output and "上海" not in result.output
    ctx.principal = replace(ctx.principal, bot_account_id="b")
    result = execute(tool(ctx), query="address", valid_at="2026-01-15")
    assert "深圳" not in result.output and "上海" not in result.output


def test_schema_and_runtime_action_catalog_expose_valid_at():
    from loop import ports
    assert "valid_at" in MemoryRecallTool().schema()["function"]["parameters"]["properties"]
    assert "memory_recall" in ports.ALLOWED_TASKS
    assert "memory_recall" in ports.TASK_FAMILIES["翻旧账"]
    assert "valid_at" in ports.TASK_ARGS["memory_recall"]


def test_unknown_validity_start_is_not_guessed_for_history():
    world = store.get_world_model("same", scope=SCOPE)
    situation.assert_fact(world, "address", value="深圳", at="08:10")
    assert "深圳" in execute(tool(), query="address", kinds="fact").output
    assert "深圳" not in execute(tool(), query="address", valid_at="2026-01-15").output


def test_formal_extra_contract_accepts_time_only_on_query_copy():
    from agents.base import AgentContext, _extra_contract_keys
    from core.memory.beliefs import query_context
    keys = _extra_contract_keys()
    assert keys is not None and "belief_as_of" in keys
    original = AgentContext(messages=[], extra={"platform": "wechat", "account_id": "a", "chat_id": "same"})
    clone = query_context(original, "2026-01-15")
    assert clone.extra["belief_as_of"] == "2026-01-15 00:00:00"
    assert "belief_as_of" not in original.extra
