from dataclasses import replace
from types import SimpleNamespace

import pytest

from agents.tools.memory_index import ReadMemoryTool
from core.context import situation, situation_store as store
from core.context.cognitive_commit import Scope
from core.memory.beliefs import BeliefMemoryProvider, effective_beliefs
from core.memory.hub import MemoryHub
from core.memory.index_scope import bind_index
from core.terminal.memory_gateway import index_entries
from core.terminal.principal import PrincipalContext, ChatType, RelationLevel

SCOPE = Scope("wechat", "a", "same")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    store.reset_world_models()
    yield
    store.reset_world_models()


def principal():
    return PrincipalContext("wechat", "a", "same", ChatType.PRIVATE, "person-a", "A", RelationLevel.UNKNOWN)


def context(at=""):
    return SimpleNamespace(principal=principal(), extra={"platform": "wechat", "account_id": "a", "chat_id": "same", "belief_as_of": at})


def model():
    return store.get_world_model("same", scope=SCOPE)


def fact(key="address", value="深圳", **kwargs):
    refs = kwargs.pop("evidence_refs", ("event",))
    return situation.assert_fact(model(), key, value=value, at=kwargs.pop("at", "2026-01-01"), subject_key="person-a", predicate="lives_in", evidence_refs=refs, **kwargs)


def recall(query="", at="", provider=None):
    return MemoryHub([provider or BeliefMemoryProvider()]).recall(principal(), query, kinds=["fact"], limit=20, context=context(at))


def execute(tool, **args):
    coroutine = tool.execute(**args)
    try:
        with pytest.raises(StopIteration) as result:
            coroutine.send(None)
        return result.value.value
    finally:
        coroutine.close()


def test_correction_hides_wrong_value_before_keyword_search():
    fact()
    fact("address-v2", "上海", at="2026-02-01", supersedes="address")
    assert not recall("深圳")
    assert len(recall("上海")) == 1
    assert "深圳" not in situation.render_situation(model())
    assert len(model().events) == 2


def test_correction_does_not_revive_old_error_for_historical_question():
    fact()
    fact("address-v2", "上海", at="2026-02-01", supersedes="address")
    assert not recall("深圳", at="2026-01-15")
    assert len(recall("上海", at="2026-01-15")) == 1


def test_time_change_preserves_historical_validity_and_boundary():
    fact()
    fact("address-v2", "上海", at="2026-03-01", valid_from="2026-02-01", supersedes="address", change_kind="time_change")
    assert [b.value for b in effective_beliefs(model(), at="2026-01-31")] == ["深圳"]
    assert [b.value for b in effective_beliefs(model(), at="2026-02-01")] == ["上海"]
    assert recall("深圳", at="2026-01-15")
    assert not recall("深圳", at="2026-02-01")


def test_conflicting_sources_remain_visible_instead_of_last_write_winning():
    fact(who="source-a")
    fact(value="北京", who="source-b")
    hits = recall()
    assert len(hits) == 2
    assert all(hit.metadata["conflict"] for hit in hits)
    assert all("冲突待核对" in hit.content for hit in hits)
    assert "冲突待核对" in situation.render_situation(model())


def test_invisible_fact_cannot_supersede_a_visible_fact():
    fact()
    fact("foreign", "秘密", supersedes="address", scope="wechat:b:same")
    assert [hit.content for hit in recall()] == ["address = 深圳"]


def test_no_principal_and_other_account_cannot_read_same_named_conversation():
    fact()
    provider = BeliefMemoryProvider()
    assert provider.recall(None) == []
    assert provider.recall(replace(principal(), bot_account_id="b")) == []
    assert provider.recall(principal(), context=SimpleNamespace(extra={"platform": "qq"})) == []


def test_future_and_expired_facts_do_not_appear_as_current():
    fact(valid_to="2026-02-01")
    fact("future", "上海", valid_from="2099-01-01")
    assert not recall()


def test_revoked_fact_is_not_reused_as_valid_historical_fact():
    fact()
    model().add("fact_revoked", {"key": "address"}, at="2026-02-01")
    assert not recall(at="2026-01-15")


@pytest.mark.parametrize("at", ["", "2026-01-15"])
def test_fact_with_only_dead_source_evidence_is_not_recalled(monkeypatch, at):
    from core.memory import belief_commit

    fact()
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "dead",
    )
    provider = BeliefMemoryProvider(verify_evidence=True)
    assert not recall(at=at, provider=provider)


def test_unknown_source_evidence_keeps_fact_but_marks_it_unverified(monkeypatch):
    from core.memory import belief_commit

    fact()
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "unknown",
    )
    hits = recall(provider=BeliefMemoryProvider(verify_evidence=True))
    assert len(hits) == 1
    assert "[未核实旧记录]" in hits[0].content
    assert hits[0].metadata["evidence_state"] == "unknown"


def test_one_live_source_evidence_rescues_fact(monkeypatch):
    from core.memory import belief_commit

    fact(evidence_refs=("dead", "live"))
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "alive" if ref == "live" else "dead",
    )
    hits = recall(provider=BeliefMemoryProvider(verify_evidence=True))
    assert len(hits) == 1
    assert "[未核实旧记录]" not in hits[0].content
    assert hits[0].metadata["evidence_state"] == "alive"


def test_world_update_write_path_keeps_version_metadata():
    fact()
    situation.apply_world_updates(model(), {"observe": [{"key": "address-v2", "value": "上海", "subject_key": "person-a", "predicate": "lives_in", "supersedes": "address", "change_kind": "time_change", "valid_from": "2026-02-01", "evidence_refs": ["message-2"]}]}, at="2026-03-01")
    assert recall("深圳", at="2026-01-15")
    assert recall("上海")[0].source_event_ids == ("message-2",)


def test_invalid_correction_target_is_rejected_before_write():
    fact()
    before = list(model().events)
    with pytest.raises(ValueError, match="belief_supersedes_missing"):
        fact("bad", supersedes="missing")
    assert model().events == before


def test_stale_index_cannot_expand_invalidated_current_belief():
    fact()
    ctx = context()
    bind_index(ctx, index_entries(recall()), ctx.principal)
    tool = ReadMemoryTool()
    tool.set_context(ctx)
    assert "深圳" in execute(tool, keys="m1").output
    fact("address-v2", "上海", at="2026-02-01", supersedes="address")
    result = execute(tool, keys="m1")
    assert "深圳" not in result.output
    assert result.error == "no_memory_index"


def test_new_conflict_invalidates_previous_unqualified_index():
    fact()
    ctx = context()
    bind_index(ctx, index_entries(recall()), ctx.principal)
    tool = ReadMemoryTool()
    tool.set_context(ctx)
    fact("another-source", "北京")
    assert execute(tool, keys="m1").error == "no_memory_index"


@pytest.mark.parametrize("kwargs", [{"change_kind": "unknown"}, {"valid_from": "invalid"}, {"change_kind": "time_change", "valid_from": "2026-01-01T00:00:00+08:00"}])
def test_invalid_version_fields_do_not_mutate_events(kwargs):
    fact()
    before = list(model().events)
    with pytest.raises(ValueError):
        fact("bad", supersedes="address", **kwargs)
    assert model().events == before


def test_historical_index_stays_usable_for_correct_time_change():
    fact()
    ctx = context("2026-01-15")
    fact("address-v2", "上海", at="2026-02-01", supersedes="address", change_kind="time_change")
    # 旧版本的区间元数据已变化，旧快照拒绝；重新召回的历史索引仍可展开。
    bind_index(ctx, index_entries(recall(at="2026-01-15")), ctx.principal)
    tool = ReadMemoryTool()
    tool.set_context(ctx)
    assert "深圳" in execute(tool, keys="m1").output


def test_reloading_preserves_both_time_intervals():
    fact()
    fact("address-v2", "上海", supersedes="address", at="2026-02-01", change_kind="time_change")
    assert store.persist_world_model("same", scope=SCOPE)
    store.reset_world_models()
    assert recall("深圳", at="2026-01-15")
    assert recall("上海")
