"""认知提交审查的六个反例；所有存储均隔离。"""
import sqlite3
from types import SimpleNamespace

import pytest

from core.context import cognitive_commit as cc, situation, situation_store as worlds
from core.context.goal_close import commit_close, close_local
from core.infra import actions
from loop import ledger, promises

SCOPE = cc.Scope("wechat", "bot", "c1", "private", "")


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


def test_group_cannot_render_private_legacy_promises():
    ledger.record_promise(who="private", what="private secret", chat_id="private")
    assert "private secret" not in promises.render_block("group@chatroom")
    assert promises.render_block("") == ""
    assert "private secret" in promises.render_block("private")


def test_scoped_models_are_separate_after_restart():
    first = worlds.get_world_model("c1", scope=SCOPE)
    situation.open_intention(first, "secret", kind="promise", evidence="private secret")
    worlds.persist_world_model("c1", scope=SCOPE)
    worlds.reset_world_models()
    other = worlds.get_world_model("c1", scope=cc.Scope("qq", "other", "c1", "private", ""))
    assert not other.events
    assert situation.open_intentions(worlds.get_world_model("c1", scope=SCOPE))


def test_prompt_uses_the_current_turn_scope():
    from loop import ports
    first = worlds.get_world_model("c1", scope=SCOPE)
    situation.open_intention(first, "secret", kind="promise", evidence="private secret")
    token = ports._READ_SCOPE.set(ports.ReadScope("qq", "other", "c1"))
    try:
        assert "private secret" not in promises.render_block("c1")
    finally:
        ports._READ_SCOPE.reset(token)


def test_same_subject_cannot_overwrite_another_scope():
    conn = cc.connect()
    proposal = cc.Proposal("p", "提醒:喝水", "close", {"state": "completed"}, ("event",))
    try:
        assert cc.commit_updates(conn, context=cc.CommitContext(scope=SCOPE, visible_event_ids=frozenset({"event"})), proposals=[proposal], idempotency_key="a").ok
        before = cc.read_state(conn, proposal.subject_key, scope=SCOPE)
        other = cc.Scope("wechat", "bot", "c2", "private", "")
        result = cc.commit_updates(conn, context=cc.CommitContext(scope=other, visible_event_ids=frozenset({"event"})), proposals=[proposal], idempotency_key="a")
        assert result.ok
        assert cc.read_state(conn, proposal.subject_key, scope=SCOPE) == before
        assert cc.read_state(conn, proposal.subject_key, scope=other)["revision"] == 1
        with pytest.raises(ValueError, match="ambiguous_subject_scope"):
            cc.read_state(conn, proposal.subject_key)
    finally:
        conn.close()


def test_empty_visibility_is_not_unrestricted():
    conn = cc.connect()
    try:
        result = cc.commit_updates(conn, context=cc.CommitContext(scope=SCOPE), proposals=[cc.Proposal("p", "goal", "close", {}, ("invented",))], idempotency_key="a")
        assert result.rejected == (("p", "evidence_not_visible"),)
    finally:
        conn.close()


@pytest.mark.parametrize("state", ["", "completed"])
def test_local_fallback_cannot_invent_completion_without_evidence(state):
    model = worlds.get_world_model("c1", scope=SCOPE)
    situation.open_intention(model, "goal", kind="remind")
    with pytest.raises(ValueError, match="goal_evidence_required"):
        close_local(model, "goal", result="sent", state=state)
    assert situation.open_intentions(model)


@pytest.mark.parametrize("status,chat,accepted", [("done", "c1", True), ("pending", "c1", False), ("done", "c2", False), (None, "c1", False)])
def test_completion_requires_a_real_same_chat_terminal_reminder(monkeypatch, status, chat, accepted):
    from core.terminal import store_port
    row = None if status is None else SimpleNamespace(source_platform="wechat", source_account_id="bot", source_chat_id=chat, status=status)
    monkeypatch.setattr(store_port, "get_repo", lambda: SimpleNamespace(reminders=SimpleNamespace(get=lambda key: row)))
    model = worlds.get_world_model("c1", scope=SCOPE)
    situation.open_intention(model, "goal", kind="remind")
    assert commit_close(model, "goal", evidence_ref="reminder:1", state="completed") is accepted
    if not accepted:
        with pytest.raises(ValueError, match="goal_evidence_unverified"):
            close_local(model, "goal", evidence_ref="reminder:1", state="completed")
        assert situation.open_intentions(model)


@pytest.mark.parametrize("chat,exists,accepted", [("c1", True, True), ("c2", True, False), ("c1", False, False)])
def test_message_evidence_must_exist_in_this_conversation(monkeypatch, chat, exists, accepted):
    from core.terminal import shadow
    row = SimpleNamespace(platform="wechat", bot_account_id="bot", chat_id=chat) if exists else None
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=lambda ref: row))
    model = worlds.get_world_model("c1", scope=SCOPE)
    situation.open_intention(model, "goal", kind="remind")
    assert commit_close(model, "goal", evidence_ref="message-event", state="completed") is accepted


def test_repeated_close_cannot_turn_abandoned_into_completed(monkeypatch):
    from core.terminal import shadow
    row = SimpleNamespace(platform="wechat", bot_account_id="bot", chat_id="c1")
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=lambda ref: row))
    model = worlds.get_world_model("c1", scope=SCOPE)
    situation.open_intention(model, "goal", kind="remind")
    assert commit_close(model, "goal", evidence_ref="failed-event", state="abandoned")
    assert commit_close(model, "goal", evidence_ref="done-event", state="completed")
    assert situation.project(model.events).intentions["goal"].state == "abandoned"


@pytest.mark.parametrize("terminal", ["abandoned", "completed"])
def test_terminal_state_survives_reload(terminal):
    model = worlds.get_world_model("c1", scope=SCOPE)
    situation.open_intention(model, "goal", kind="remind")
    worlds.persist_world_model("c1")
    conn = cc.connect()
    try:
        assert cc.commit_updates(conn, context=cc.CommitContext(scope=SCOPE, visible_event_ids=frozenset({"event"})), proposals=[cc.Proposal("p", "goal", "close", {"state": terminal}, ("event",))], idempotency_key="a").ok
    finally:
        conn.close()
    worlds.reset_world_models()
    restored = worlds.get_world_model("c1", scope=SCOPE)
    assert situation.project(restored.events).intentions["goal"].state == terminal


def test_rebuild_holds_write_lock_before_reading_events():
    conn = cc.connect()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    competitor = sqlite3.connect(path, timeout=0, isolation_level=None)
    checks = []

    def trace(sql):
        if sql.startswith("SELECT seq, event_id"):
            try:
                competitor.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
                checks.append(True)
            else:
                competitor.execute("ROLLBACK")
                checks.append(False)

    conn.set_trace_callback(trace)
    try:
        cc.rebuild(conn)
        assert checks == [True], "读取事件前必须取得写锁，排除旧快照覆盖新提交的窗口"
    finally:
        competitor.close()
        conn.close()
