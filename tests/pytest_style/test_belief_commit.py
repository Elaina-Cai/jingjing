from dataclasses import replace
from datetime import datetime
import threading

import pytest

from core.context import cognitive_commit as cc, situation, situation_store as store
from core.memory import belief_commit as service
from core.memory.beliefs import effective_beliefs
from core.terminal.event_model import Event, EventType

SCOPE = cc.Scope("wechat", "a", "same")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from core.terminal import shadow
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    store.reset_world_models()
    proofs = {key: Event(key, datetime(2026, 2, 1), platform="wechat", bot_account_id="a", chat_id="same", actor_person_id="person-a", content="之前地址记错了，我住在上海") for key in ("msg-1", "msg-2")}
    from types import SimpleNamespace
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=lambda key: proofs.get(key)))
    yield proofs
    store.reset_world_models()


def model():
    return store.get_world_model("same", scope=SCOPE)


def offer():
    situation.assert_fact(model(), "address", value="深圳", at="2026-01-01", subject_key="person-a", predicate="lives_in", evidence_refs=("original",))
    assert store.persist_world_model("same", scope=SCOPE)
    return service.offers(SCOPE)["b1"]


def submit(target, **kwargs):
    return service.submit(target, scope=kwargs.pop("scope", SCOPE), value=kwargs.pop("value", "上海"), change_kind=kwargs.pop("change_kind", "correction"), evidence_ref=kwargs.pop("evidence_ref", "msg-1"), visible=kwargs.pop("visible", frozenset({"msg-1", "msg-2"})), **kwargs)


def count():
    conn = cc.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM cognitive_events WHERE source='belief_tool'").fetchone()[0]
    finally:
        conn.close()


def test_commit_is_idempotent_and_only_log_is_written():
    target = offer()
    path = store._model_path("same", scope=SCOPE)
    original = path.read_bytes()
    assert submit(target)["result"] == "committed"
    assert submit(target)["duplicates"]
    assert count() == 1 and path.read_bytes() == original
    assert [b.value for b in effective_beliefs(model())] == ["上海"]
    store.reset_world_models()
    assert [b.value for b in effective_beliefs(model())] == ["上海"]


def test_same_idempotency_key_with_changed_value_is_rejected():
    target = offer()
    assert submit(target)["result"] == "committed"
    assert submit(target, value="北京")["result"] == "conflict"
    assert count() == 1


def test_replayed_message_after_restart_does_not_create_another_revision():
    original = offer()
    assert submit(original)["result"] == "committed"
    store.reset_world_models()
    refreshed = next(iter(service.offers(SCOPE).values()))
    assert refreshed.revision == 1
    assert submit(refreshed)["duplicates"]
    assert count() == 1


def test_legacy_belief_without_subject_preserves_logical_identity_on_replay():
    situation.assert_fact(model(), "address", value="深圳", at="2026-01-01")
    store.persist_world_model("same", scope=SCOPE)
    original = service.offers(SCOPE)["b1"]
    assert submit(original)["result"] == "committed"
    store.reset_world_models()
    refreshed = service.offers(SCOPE)["b1"]
    assert refreshed.subject == original.subject and refreshed.revision == 1
    assert submit(refreshed)["duplicates"]
    assert count() == 1


def test_old_snapshot_cannot_overwrite_new_version():
    target = offer()
    assert submit(target)["result"] == "committed"
    assert submit(target, evidence_ref="msg-2", value="北京")["result"] == "conflict"
    assert count() == 1


def test_stale_expected_database_revision_is_rejected():
    target = offer()
    assert submit(target)["result"] == "committed"
    current = next(iter(service.offers(SCOPE).values()))
    result = submit(replace(current, revision=0), evidence_ref="msg-2", value="北京")
    assert result["result"] == "conflict"
    assert "revision_conflict" in str(result)


def test_cross_scope_offer_cannot_be_rebound():
    target = offer()
    assert submit(target, scope=cc.Scope("wechat", "b", "same"))["reason"] == "scope_mismatch"
    assert count() == 0


@pytest.mark.parametrize("subject_key", ["person-a", "room-topic"])
def test_group_correction_keeps_verified_owner_after_source_ages_out(isolated, subject_key):
    scope = cc.Scope("wechat", "a", "same", chat_type="group")
    isolated["msg-1"].actor_person_id = "linked-person-a"
    isolated["msg-1"].actor_account_id = "person-a"
    isolated["msg-2"].actor_person_id = "person-b"
    situation.assert_fact(model(), "address", value="深圳", at="2026-01-01",
                          subject_key=subject_key, predicate="lives_in")
    assert submit(service.offers(scope)["b1"], scope=scope)["result"] == "committed"
    del isolated["msg-1"]

    result = submit(service.offers(scope)["b1"], scope=scope, evidence_ref="msg-2", value="北京")
    if subject_key == "person-a":
        assert result == {"result": "rejected", "reason": "evidence_subject_mismatch"}
    else:
        assert result["result"] == "committed"


@pytest.mark.parametrize("mode", ["missing", "other_chat", "other_account", "self", "thought", "invisible"])
def test_invalid_evidence_cannot_write(isolated, mode):
    target = offer()
    proof = isolated["msg-1"]
    if mode == "missing":
        isolated.pop("msg-1")
    elif mode == "other_chat":
        proof.chat_id = "elsewhere"
    elif mode == "other_account":
        proof.bot_account_id = "b"
    elif mode == "self":
        proof.metadata["is_self"] = 1
    elif mode == "thought":
        proof.event_type = EventType.THOUGHT
    kwargs = {"visible": frozenset()} if mode == "invisible" else {}
    assert submit(target, **kwargs)["result"] == "rejected"
    assert count() == 0


def test_dry_does_not_commit():
    assert submit(offer(), dry_run=True)["result"] == "dry"
    assert count() == 0


def test_offers_and_dry_do_not_create_a_database(tmp_path):
    target = offer()
    path = tmp_path / "cognitive.db"
    assert not path.exists()
    assert submit(target, dry_run=True)["result"] == "dry"
    assert not path.exists()


def test_committed_correction_invalidates_old_index_and_new_recall_expands():
    from types import SimpleNamespace
    from agents.tools.memory_index import ReadMemoryTool
    from core.memory.beliefs import BeliefMemoryProvider
    from core.memory.index_scope import bind_index
    from core.terminal.memory_gateway import index_entries
    from core.terminal.principal import PrincipalContext, ChatType, RelationLevel
    principal = PrincipalContext("wechat", "a", "same", ChatType.PRIVATE, "person-a", "A", RelationLevel.UNKNOWN)
    ctx = SimpleNamespace(principal=principal, extra={"platform": "wechat", "account_id": "a", "chat_id": "same"})
    target = offer()
    provider = BeliefMemoryProvider()
    bind_index(ctx, index_entries(provider.recall(principal)), principal)
    assert submit(target)["result"] == "committed"
    tool = ReadMemoryTool()
    tool.set_context(ctx)
    def read():
        coroutine = tool.execute(keys="m1")
        try:
            with pytest.raises(StopIteration) as done:
                coroutine.send(None)
            return done.value.value
        finally:
            coroutine.close()
    assert read().error == "no_memory_index"
    assert not provider.recall(principal, "深圳")
    bind_index(ctx, index_entries(provider.recall(principal, "上海")), principal)
    assert "上海" in read().output


def test_time_change_keeps_history():
    assert submit(offer(), change_kind="time_change", valid_from="2026-02-01")["result"] == "committed"
    assert [b.value for b in effective_beliefs(model(), at="2026-01-15")] == ["深圳"]
    assert [b.value for b in effective_beliefs(model(), at="2026-02-01")] == ["上海"]


def test_before_commit_failure_keeps_original_and_retry_succeeds(monkeypatch):
    target = offer()
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("storage unavailable")
        patch.setattr(cc, "commit_updates", fail)
        assert submit(target)["result"] == "error"
    assert count() == 0
    assert [b.value for b in effective_beliefs(model())] == ["深圳"]
    assert submit(target)["result"] == "committed"


def test_failure_after_commit_recovers_as_duplicate(monkeypatch):
    target = offer()
    original = cc.commit_updates
    with monkeypatch.context() as patch:
        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("response lost after commit")
        patch.setattr(cc, "commit_updates", lost_response)
        assert submit(target)["result"] == "error"
    assert submit(target)["duplicates"]
    assert count() == 1


def test_concurrent_writers_cannot_overwrite_each_other():
    target = offer()
    barrier = threading.Barrier(2)
    results = []
    def writer(value, ref):
        barrier.wait(timeout=5)
        results.append(submit(target, value=value, evidence_ref=ref))
    threads = [threading.Thread(target=writer, args=("上海", "msg-1")), threading.Thread(target=writer, args=("北京", "msg-2"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(item["result"] for item in results) == ["committed", "conflict"]
    assert count() == 1
