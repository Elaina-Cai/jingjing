"""P1 纵切：统一认知提交的确定性反例。

验的是四条硬约束，不是「函数能跑」：
1. 幂等：同一批重试不重复落账，同键换内容直接拒绝；
2. 版本：过期版本整批不写，不覆盖别人刚写的状态；
3. 权限/证据：模型不能扩大 scope，证据必须来自本回合可见事件；
4. 可重建：投影只是缓存，重放事件必须得到同一结果。
"""
from __future__ import annotations

import os
import sqlite3
import threading

import pytest

from core.context.cognitive_commit import (
    CommitContext,
    Proposal,
    Scope,
    commit_updates,
    connect,
    ensure_schema,
    event_id_for,
    read_cursor,
    read_state,
    rebuild,
)

SCOPE = Scope("wechat", "bot_a", "g1", "group", "g1")
CONTEXT = CommitContext(scope=SCOPE, actor_key="person-a", source="model", visible_event_ids=frozenset({"ev-1", "ev-2"}))


@pytest.fixture()
def db(tmp_path):
    conn = connect(str(tmp_path / "cognitive.db"))
    try:
        yield conn
    finally:
        conn.close()


def _believe(payload=None, *, proposal_id="p1", evidence=("ev-1",), subject="belief:alice"):
    return Proposal(
        proposal_id=proposal_id,
        subject_key=subject,
        operation="believe",
        payload=payload or {"claim": "alice 在准备搬家"},
        evidence_refs=evidence,
    )


def _count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM cognitive_events").fetchone()["n"])


def test_commit_writes_event_projection_and_cursor(db):
    result = commit_updates(
        db, context=CONTEXT, proposals=[_believe()], expected_revisions={"belief:alice": 0},
        idempotency_key="turn-1",
    )

    assert result.ok and len(result.committed) == 1
    assert result.revisions["belief:alice"] == 1
    state = read_state(db, "belief:alice")
    assert state["payload"]["claim"] == "alice 在准备搬家"
    assert state["operation"] == "believe" and state["revision"] == 1
    assert read_cursor(db) == 1
    assert _count(db) == 1


def test_retry_of_successful_batch_is_a_no_op(db):
    first = commit_updates(
        db, context=CONTEXT, proposals=[_believe()], expected_revisions={"belief:alice": 0},
        idempotency_key="turn-1",
    )
    # 重试时版本已经变成 1，但这是「同一件事」，不该被当成冲突。
    second = commit_updates(
        db, context=CONTEXT, proposals=[_believe()], expected_revisions={"belief:alice": 0},
        idempotency_key="turn-1",
    )

    assert first.ok and second.ok
    assert second.committed == ()
    assert second.duplicates == first.committed
    assert second.revisions["belief:alice"] == 1
    assert _count(db) == 1


def test_same_key_with_changed_content_is_rejected(db):
    commit_updates(
        db, context=CONTEXT, proposals=[_believe()], idempotency_key="turn-1",
    )
    changed = commit_updates(
        db,
        context=CONTEXT,
        proposals=[_believe({"claim": "换了个说法"})],
        idempotency_key="turn-1",
    )

    assert not changed.ok
    assert changed.rejected[0][1] == "idempotency_conflict"
    assert _count(db) == 1
    assert read_state(db, "belief:alice")["payload"]["claim"] == "alice 在准备搬家"


def test_stale_revision_rejects_the_whole_batch(db):
    commit_updates(db, context=CONTEXT, proposals=[_believe()], idempotency_key="turn-1")

    batch = [
        _believe(proposal_id="p2", payload={"claim": "第二次"}),
        _believe(proposal_id="p3", subject="belief:bob", payload={"claim": "bob 加班"}),
    ]
    result = commit_updates(
        db, context=CONTEXT, proposals=batch,
        expected_revisions={"belief:alice": 0, "belief:bob": 0},
        idempotency_key="turn-2",
    )

    assert not result.ok
    assert {pid for pid, _ in result.rejected} == {"p2"}
    assert result.rejected[0][1].startswith("revision_conflict")
    assert read_state(db, "belief:bob") is None, "同批其他主体也不得写入"
    assert _count(db) == 1


def test_evidence_must_be_visible_to_this_turn(db):
    context = CommitContext(scope=SCOPE, visible_event_ids=frozenset({"ev-allowed"}))
    result = commit_updates(
        db, context=context,
        proposals=[_believe(evidence=("ev-secret",))],
        idempotency_key="turn-1",
    )

    assert not result.ok
    assert result.rejected[0][1] == "evidence_not_visible"
    assert _count(db) == 0


def test_proposal_cannot_widen_scope(db):
    other = Proposal(
        proposal_id="p1",
        subject_key="belief:alice",
        operation="believe",
        payload={"claim": "别处的记忆"},
        evidence_refs=("ev-1",),
        scope=Scope("wechat", "bot_a", "g2", "group", "g2"),
    )
    result = commit_updates(db, context=CONTEXT, proposals=[other], idempotency_key="turn-1")

    assert not result.ok
    assert result.rejected[0][1] == "scope_mismatch"
    assert _count(db) == 0


def test_belief_without_evidence_is_rejected(db):
    result = commit_updates(
        db, context=CONTEXT, proposals=[_believe(evidence=())], idempotency_key="turn-1"
    )

    assert not result.ok
    assert result.rejected[0][1] == "evidence_required"
    assert _count(db) == 0


def test_supersedes_keeps_history_but_projection_uses_the_new_one(db):
    first = commit_updates(db, context=CONTEXT, proposals=[_believe()], idempotency_key="turn-1")
    old_event = first.committed[0]
    second = commit_updates(
        db,
        context=CONTEXT,
        proposals=[
            Proposal(
                proposal_id="p2",
                subject_key="belief:alice",
                operation="revise",
                payload={"claim": "其实 alice 已经搬完了"},
                evidence_refs=("ev-2",),
                supersedes=old_event,
            )
        ],
        expected_revisions={"belief:alice": 1},
        idempotency_key="turn-2",
    )

    assert second.ok
    assert _count(db) == 2, "历史事件不能被删"
    state = read_state(db, "belief:alice")
    assert state["payload"]["claim"] == "其实 alice 已经搬完了"
    assert state["revision"] == 2


def test_rebuild_reproduces_the_stored_projection(db):
    commit_updates(db, context=CONTEXT, proposals=[_believe()], idempotency_key="turn-1")
    before = read_state(db, "belief:alice")
    db.execute("UPDATE cognitive_state_scoped SET payload = '{}', operation = 'broken'")

    revisions = rebuild(db)

    assert revisions["belief:alice"] == 1
    assert read_state(db, "belief:alice") == before


def test_two_writers_cannot_overwrite_each_other(tmp_path):
    path = str(tmp_path / "race.db")
    conn = connect(path)
    ensure_schema(conn)
    conn.close()

    barrier = threading.Barrier(2)
    results: list = []

    def writer(claim: str, key: str) -> None:
        own = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        own.row_factory = sqlite3.Row
        try:
            barrier.wait()
            results.append(
                commit_updates(
                    own,
                    context=CONTEXT,
                    proposals=[
                        Proposal(
                            proposal_id="p1",
                            subject_key="belief:alice",
                            operation="believe",
                            payload={"claim": claim},
                            evidence_refs=("ev-1",),
                        )
                    ],
                    expected_revisions={"belief:alice": 0},
                    idempotency_key=key,
                )
            )
        finally:
            own.close()

    threads = [
        threading.Thread(target=writer, args=("A 写的", "k-a")),
        threading.Thread(target=writer, args=("B 写的", "k-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 2
    assert sum(1 for result in results if result.ok) == 1, "只能有一个写入者成功"
    rejected = next(result for result in results if not result.ok)
    assert rejected.rejected[0][1].startswith("revision_conflict")

    check = connect(path)
    try:
        assert read_state(check, "belief:alice")["revision"] == 1
        assert _count(check) == 1
    finally:
        check.close()


def test_event_id_is_stable_and_scope_sensitive():
    first = event_id_for(SCOPE, "msg-1", "belief:alice", "believe")
    again = event_id_for(SCOPE, "msg-1", "belief:alice", "believe")
    other_chat = event_id_for(Scope("wechat", "bot_a", "g2", "group", "g2"), "msg-1", "belief:alice", "believe")
    other_subject = event_id_for(SCOPE, "msg-1", "belief:bob", "believe")

    assert first == again
    assert len({first, other_chat, other_subject}) == 3


def test_schema_creation_is_idempotent(tmp_path):
    path = str(tmp_path / "twice.db")
    for _ in range(3):
        conn = connect(path)
        ensure_schema(conn)
        conn.close()

    conn = connect(path)
    try:
        assert commit_updates(
            conn, context=CONTEXT, proposals=[_believe()], idempotency_key="turn-1"
        ).ok
    finally:
        conn.close()


def test_production_world_db_is_blocked_under_pytest():
    """测试进程里读写生产库都必须被拒绝（2026-11-07 真实污染过）。"""
    from core.context.cognitive_commit import _production_world_db, closed_subjects_for_chat

    path = _production_world_db()
    if not os.path.exists(path):
        pytest.skip("本机没有生产 world.db")

    with pytest.raises(RuntimeError):
        connect(path)
    assert closed_subjects_for_chat("c1", path=path) == frozenset()


def test_connect_defaults_to_world_db_path(tmp_path, monkeypatch):
    """默认落点必须是 world.db（与入站/队列/回执同库），不是另起一个库。"""
    from db import world_db

    monkeypatch.delenv("YINYUE_COGNITIVE_DB", raising=False)

    monkeypatch.setattr(world_db, "PATH", str(tmp_path / "world.db"))
    conn = connect()
    try:
        assert commit_updates(
            conn, context=CONTEXT, proposals=[_believe()], idempotency_key="turn-1"
        ).ok
        assert read_cursor(conn) == 1
        assert (tmp_path / "world.db").exists()
    finally:
        conn.close()


def test_closed_subjects_read_does_not_create_a_database(tmp_path, monkeypatch):
    """读侧派生绝不能因为「查一下」就把库建出来（更不许碰生产库）。"""
    from core.context.cognitive_commit import closed_subjects_for_chat, invalidate_closed_cache

    absent = tmp_path / "absent.db"
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(absent))
    invalidate_closed_cache()

    assert closed_subjects_for_chat("c1") == frozenset()
    assert not absent.exists()


def test_closed_subjects_can_be_scoped_to_a_full_conversation(tmp_path, monkeypatch):
    from core.context.cognitive_commit import (
        closed_subjects_for_chat,
        closed_subjects_for_scope,
        invalidate_closed_cache,
    )

    path = str(tmp_path / "scope.db")
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", path)
    invalidate_closed_cache()
    conn = connect(path)
    try:
        assert commit_updates(
            conn,
            context=CommitContext(scope=SCOPE, visible_event_ids=frozenset({"evt-1"})),
            proposals=[
                Proposal("p1", "提醒:喝水", "close", {"x": 1}, ("evt-1",))
            ],
            idempotency_key="k-1",
        ).ok
    finally:
        conn.close()

    assert closed_subjects_for_scope(SCOPE) == frozenset({"提醒:喝水"})
    assert closed_subjects_for_scope(
        Scope("qq", "bot_a", "g1", "group", "g1")
    ) == frozenset()
    # 过渡口径仍可用（旧世界模型只有 chat_id）。
    assert closed_subjects_for_chat("g1") == frozenset({"提醒:喝水"})
