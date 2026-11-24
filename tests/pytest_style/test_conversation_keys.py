"""完整会话键：迁移、重启、同名并发、日志重建与旧接口歧义。"""
import json
import threading
from types import SimpleNamespace

import pytest

from core.context import cognitive_commit as cc, situation, situation_store as worlds
from core.infra import actions
from loop import ledger, ports, promises

A = cc.Scope("wechat", "a", "same")
B = cc.Scope("wechat", "b", "same")
C = cc.Scope("qq", "a", "same")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setattr(actions, "DIR", str(tmp_path / "actions"))
    worlds.reset_world_models()
    cc.invalidate_closed_cache()
    yield
    worlds.reset_world_models()
    cc.invalidate_closed_cache()


def commit(conn, scope, operation="intend", *, supersedes="", occurrence="first", expected=0):
    return cc.commit_updates(conn, context=cc.CommitContext(scope, visible_event_ids=frozenset({"fact"})), proposals=[cc.Proposal("p", "提醒:喝水", operation, {"state": "abandoned" if operation == "close" else "active"}, ("fact",), occurrence=occurrence, supersedes=supersedes)], expected_revisions={"提醒:喝水": expected}, idempotency_key=occurrence)


def test_same_chat_survives_three_scoped_snapshots_and_enumeration():
    for index, scope in enumerate((A, B, C)):
        model = worlds.get_world_model("same", scope=scope)
        situation.open_intention(model, "提醒:喝水", kind="remind", evidence=str(index))
        assert worlds.persist_world_model("same", scope=scope)
    assert len(worlds.known_world_models()) == 3
    assert len(worlds.world_models_on_disk()) == 3
    assert len(worlds.all_world_models()) == 3
    worlds.reset_world_models()
    for index, scope in enumerate((A, B, C)):
        assert situation.open_intentions(worlds.get_world_model("same", scope=scope))[0].evidence == str(index)
    with pytest.raises(ValueError, match="ambiguous_world_scope"):
        worlds.persist_world_model("same")


@pytest.mark.parametrize("known", [True, False])
def test_legacy_snapshot_only_inherited_with_known_coordinates(known):
    path = worlds._model_path("same")
    path.parent.mkdir(parents=True)
    events = [{"kind": "intention_opened", "payload": {"key": "old"}, "at": ""}]
    payload = {"chat_id": "same", "scope": A.to_dict(), "events": events} if known else events
    path.write_text(json.dumps(payload), encoding="utf-8")
    original = path.read_bytes()
    model = worlds.get_world_model("same", scope=A)
    assert bool(model.events) is known
    assert worlds.persist_world_model("same", scope=A)
    assert path.read_bytes() == original
    assert not worlds.get_world_model("same", scope=B).events


def test_unscoped_load_remains_compatible_only_when_disk_identity_is_unique():
    worlds.get_world_model("same", scope=A)
    worlds.persist_world_model("same", scope=A)
    worlds.reset_world_models()
    assert worlds.load_world_model("same").scope["account_id"] == "a"
    worlds.get_world_model("same", scope=B)
    worlds.persist_world_model("same", scope=B)
    with pytest.raises(ValueError, match="ambiguous_world_scope"):
        worlds.load_world_model("same")


def test_versions_idempotency_supersedes_and_rebuild_are_scoped():
    conn = cc.connect()
    try:
        first = commit(conn, A)
        second = commit(conn, B)
        assert first.ok and second.ok
        assert first.committed != second.committed
        assert commit(conn, A).duplicates == first.committed
        rejected = commit(conn, B, "revise", supersedes=first.committed[0], occurrence="foreign", expected=1)
        assert rejected.rejected == (("p", "supersedes_scope_mismatch"),)
        assert commit(conn, A, "close", occurrence="close", expected=1).ok
        assert cc.read_state(conn, "提醒:喝水", scope=A)["revision"] == 2
        assert cc.read_state(conn, "提醒:喝水", scope=B)["revision"] == 1
        before = [cc.read_state(conn, "提醒:喝水", scope=scope) for scope in (A, B)]
        conn.execute("UPDATE cognitive_state_scoped SET payload='{}'")
        cc.rebuild(conn)
        assert [cc.read_state(conn, "提醒:喝水", scope=scope) for scope in (A, B)] == before
    finally:
        conn.close()


def test_old_mixed_log_rebuilds_scoped_projection_without_rewriting_history():
    conn = cc.connect()
    try:
        assert commit(conn, A).ok and commit(conn, B).ok
        conn.execute("UPDATE cognitive_events SET revision=17 WHERE scope=?", (cc._json(B.to_dict()),))
        conn.execute("DROP TABLE cognitive_state_scoped")
        before = [tuple(row) for row in conn.execute("SELECT * FROM cognitive_events")]
        cc.ensure_schema(conn)
        assert cc.read_state(conn, "提醒:喝水", scope=B)["revision"] == 1
        assert [tuple(row) for row in conn.execute("SELECT * FROM cognitive_events")] == before
        cc.ensure_schema(conn)
        assert commit(conn, B, "close", occurrence="next", expected=1).ok
    finally:
        conn.close()


def test_legacy_plain_idempotency_token_still_deduplicates():
    conn = cc.connect()
    try:
        first = commit(conn, A)
        conn.execute("UPDATE cognitive_events SET idempotency_key='first'")
        assert commit(conn, A).duplicates == first.committed
        assert commit(conn, B).ok
        assert conn.execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0] == 2
    finally:
        conn.close()


def test_failed_projection_migration_rolls_back_and_can_retry(monkeypatch):
    conn = cc.connect()
    try:
        assert commit(conn, A).ok
        conn.execute("DROP TABLE cognitive_state_scoped")
        original = cc._write_state

        def fail_after_write(*args):
            original(*args)
            raise RuntimeError("injected migration failure")

        with monkeypatch.context() as patch:
            patch.setattr(cc, "_write_state", fail_after_write)
            with pytest.raises(RuntimeError, match="injected migration failure"):
                cc.ensure_schema(conn)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='cognitive_state_scoped'").fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0] == 1
        cc.ensure_schema(conn)
        assert cc.read_state(conn, "提醒:喝水", scope=A)["revision"] == 1
    finally:
        conn.close()


def test_unknown_legacy_cognitive_scope_is_preserved_without_assignment():
    conn = cc.connect()
    try:
        assert commit(conn, A).ok
        conn.execute("UPDATE cognitive_events SET scope='{}'")
        conn.execute("DROP TABLE cognitive_state_scoped")
        cc.ensure_schema(conn)
        assert cc.read_state(conn, "提醒:喝水", scope=A) is None
        assert conn.execute("SELECT scope FROM cognitive_events").fetchone()[0] == "{}"
    finally:
        conn.close()


def test_different_scopes_can_both_commit_revision_zero_concurrently(tmp_path):
    path = str(tmp_path / "race.db")
    conn = cc.connect(path)
    conn.close()
    barrier = threading.Barrier(2)
    results, errors = [], []

    def writer(scope):
        own = cc.connect(path)
        try:
            barrier.wait(timeout=5)
            results.append(commit(own, scope))
        except Exception as exc:
            errors.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=writer, args=(scope,)) for scope in (A, B)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors and len(results) == 2 and all(result.ok for result in results)


def test_close_overlay_only_closes_its_own_same_named_goal():
    for scope in (A, B):
        situation.open_intention(worlds.get_world_model("same", scope=scope), "提醒:喝水", kind="remind")
        worlds.persist_world_model("same", scope=scope)
    conn = cc.connect()
    try:
        assert commit(conn, A, "close").ok
    finally:
        conn.close()
    worlds.reset_world_models()
    assert not situation.open_intentions(worlds.get_world_model("same", scope=A))
    assert situation.open_intentions(worlds.get_world_model("same", scope=B))


def test_legacy_promise_scope_is_not_guessed_from_matching_chat():
    ledger.record_promise(who="old", what="unknown provenance", chat_id="same")
    token = ports._READ_SCOPE.set(ports.ReadScope("wechat", "a", "same"))
    try:
        ledger.record_promise(who="a", what="account a secret", chat_id="same")
        assert "account a secret" in promises.render_block("same")
        assert "unknown provenance" not in promises.render_block("same")
    finally:
        ports._READ_SCOPE.reset(token)
    token = ports._READ_SCOPE.set(ports.ReadScope("wechat", "b", "same"))
    try:
        assert "account a secret" not in promises.render_block("same")
        assert "unknown provenance" not in promises.render_block("same")
    finally:
        ports._READ_SCOPE.reset(token)


def test_pending_recovery_visits_both_same_named_conversations(monkeypatch):
    from core.context.goal_close import close_local
    from core.context.goal_reconcile import import_all_pending
    from core.terminal import shadow
    facts = {scope.account_id: SimpleNamespace(platform=scope.platform, bot_account_id=scope.account_id, chat_id=scope.chat_id) for scope in (A, B)}
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=lambda ref: facts.get(ref)))
    for scope in (A, B):
        model = worlds.get_world_model("same", scope=scope)
        situation.open_intention(model, "goal", kind="remind")
        close_local(model, "goal", evidence_ref=scope.account_id, state="completed")
        worlds.persist_world_model("same", scope=scope)
    worlds.reset_world_models()
    report = import_all_pending()
    assert len(report) == 2 and sum(report.values()) == 2
    assert sum(import_all_pending().values()) == 0


def test_tick_routes_and_saves_both_scopes_in_an_isolated_dry_run(monkeypatch):
    from core.context.intention_tick import run_tick
    from core.context.intention_runner import IntentionExecution
    from core.terminal import shadow
    monkeypatch.setenv("YINYUE_FEATURE_INTENTION_TICK", "on")
    facts = {scope.account_id: SimpleNamespace(platform=scope.platform, bot_account_id=scope.account_id, chat_id=scope.chat_id) for scope in (A, B)}
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=lambda ref: facts.get(ref)))
    calls = []
    for scope in (A, B):
        model = worlds.get_world_model("same", scope=scope)
        situation.open_intention(model, "goal", kind="remind", evidence=scope.account_id, due_at="2026-11-07 10:00")

    async def execute(chat, intent):
        calls.append((chat, intent.evidence))
        return IntentionExecution(ok=True, result="sent", evidence_ref=intent.evidence)

    # 没有真实 I/O 或异步等待；若意外挂起立即失败。
    coroutine = run_tick(execute, now="2026-11-07 10:01")
    try:
        with pytest.raises(StopIteration) as done:
            coroutine.send(None)
    finally:
        coroutine.close()
    assert done.value.value.closed == 2
    assert set(calls) == {("same", "a"), ("same", "b")}
    worlds.reset_world_models()
    for scope in (A, B):
        assert not situation.open_intentions(worlds.get_world_model("same", scope=scope))
