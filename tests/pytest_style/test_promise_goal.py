"""承诺的两套账：账本是审计，目标是真源。

这一轮只切「写 + 展示」：
- `promise` 记完账本后开一个 `promise:<id>` 目标（origin=self_promise）；
- 展示优先读目标，账本行只在还没有目标时补位，同一行不会出现两次。
关闭路径仍走账本的显式关闭；等关闭口接过来后再彻底停用账本读。
"""
import pytest

from core.context import situation
from core.context.promise_goal import ensure_promise_goal, promise_goal_ids
from core.context.situation_store import get_world_model, reset_world_models
from core.infra import actions
from loop import ledger, promises


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setattr(actions, "DIR", str(tmp_path / "actions"))
    reset_world_models()
    yield
    reset_world_models()


def test_promise_goal_is_created_once():
    key = ensure_promise_goal(
        "c1", promise_id=11, what="找两篇资料", who="阿蓝", at="2026-11-07 10:00"
    )

    assert key == "promise:11"
    model = get_world_model("c1")
    goal = situation.project(model.events).intentions[key]
    assert goal.kind == "promise"
    assert goal.origin == "self_promise"
    assert goal.desired_outcome == "找两篇资料"
    assert goal.evidence_refs == ("ledger:11",)
    assert goal.state == "active"

    assert ensure_promise_goal("c1", promise_id=11, what="换个说法") == key
    opened = [e for e in model.events if e.kind == "intention_opened"]
    assert len(opened) == 1, "同一条承诺不能开第二个目标"


def test_existing_promise_goal_gets_source_ref_on_replay():
    key = ensure_promise_goal(
        "c1", promise_id=11, what="找两篇资料", at="2026-11-07 10:00"
    )
    ensure_promise_goal(
        "c1",
        promise_id=11,
        what="找两篇资料",
        at="2026-11-07 10:00",
        source_refs=("event-request",),
    )
    ensure_promise_goal(
        "c1",
        promise_id=11,
        what="找两篇资料",
        at="2026-11-07 10:00",
        source_refs=("event-request",),
    )

    model = get_world_model("c1")
    goal = situation.project(model.events).intentions[key]
    assert goal.evidence_refs == ("ledger:11", "event-request")
    assert len([
        event
        for event in model.events
        if event.kind == "intention_evidence"
        and event.payload.get("ref") == "event-request"
    ]) == 1


def test_promise_goal_ids_include_open_goals():
    ensure_promise_goal("c1", promise_id=11, what="找资料", at="2026-11-07 10:00")

    assert promise_goal_ids("c1") == frozenset({"promise:11"})
    assert promise_goal_ids("") == frozenset()


def test_render_prefers_goal_and_hides_the_ledger_copy():
    row = ledger.record_promise(who="阿蓝", what="找两篇资料", chat_id="c1")
    ensure_promise_goal(
        "c1", promise_id=row["id"], what="找两篇资料", who="阿蓝", at=row["at"]
    )

    block = promises.render_block("c1")

    assert block.count("找两篇资料") == 1, block
    assert block.startswith("[我还欠着的]")


def test_revoked_promise_source_is_hidden_before_a_job_runs(monkeypatch):
    from core.context.cognitive_commit import Scope
    from core.memory import belief_commit

    scope = Scope("wechat", "bot", "c1", "private", "")
    ensure_promise_goal(
        "c1",
        promise_id="revoked",
        what="这条请求已经撤回",
        scope=scope,
        source_refs=("revoked-request",),
    )
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope, store=None: "dead" if ref == "revoked-request" else "unknown",
    )

    assert "这条请求已经撤回" not in promises.render_block("c1", scope=scope)


def test_unknown_promise_source_stays_visible_with_a_label(monkeypatch):
    from core.context.cognitive_commit import Scope
    from core.memory import belief_commit

    scope = Scope("wechat", "bot", "c1", "private", "")
    ensure_promise_goal(
        "c1",
        promise_id="uncertain",
        what="来源暂时查不到",
        scope=scope,
        source_refs=("missing-request",),
    )
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope, store=None: "unknown",
    )

    block = promises.render_block("c1", scope=scope)

    assert "来源暂时查不到" in block
    assert "依据未核实" in block


def test_legacy_ledger_promise_still_shows_before_mapping():
    ledger.record_promise(who="阿蓝", what="还没映射的旧承诺", chat_id="c1")

    block = promises.render_block("c1")

    assert "还没映射的旧承诺" in block
    assert promise_goal_ids("c1") == frozenset()


def test_close_promise_closes_the_goal_and_records_evidence(monkeypatch):
    from core.context.cognitive_commit import (
        Scope,
        connect,
        invalidate_closed_cache,
        read_state,
    )

    scope = Scope("wechat", "bot", "c1", "private", "")
    row = ledger.record_promise(who="阿蓝", what="找资料", chat_id="c1")
    key = ensure_promise_goal(
        "c1", promise_id=row["id"], what="找资料", at=row["at"], scope=scope
    )

    from types import SimpleNamespace
    from core.terminal import shadow
    fact = SimpleNamespace(platform="wechat", bot_account_id="bot", chat_id="c1")
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=lambda ref: fact if ref == "event-done" else None))
    ledger.close_promise(row["id"], reason="done", chat_id="c1", evidence_ref="event-done")

    goal = situation.project(get_world_model("c1").events).intentions[key]
    assert goal.status == "closed" and goal.state == "completed"
    conn = connect()
    try:
        state = read_state(conn, key)
    finally:
        conn.close()
    assert state["operation"] == "close"
    invalidate_closed_cache()


def test_close_promise_without_a_goal_only_writes_the_audit_row():
    row = ledger.record_promise(who="阿蓝", what="旧承诺", chat_id="c1")

    ledger.close_promise(row["id"], reason="done")

    assert ledger.open_promises() == []
    assert promise_goal_ids("c1") == frozenset()


def test_expired_promise_is_abandoned():
    from core.context.cognitive_commit import Scope

    scope = Scope("wechat", "bot", "c1", "private", "")
    row = ledger.record_promise(who="阿蓝", what="会过期的事", chat_id="c1")
    key = ensure_promise_goal(
        "c1", promise_id=row["id"], what="会过期的事", at=row["at"], scope=scope
    )

    ledger.close_promise(row["id"], reason="expired（超时未完成）", chat_id="c1")

    goal = situation.project(get_world_model("c1").events).intentions[key]
    assert goal.status == "closed" and goal.state == "abandoned"


def test_import_legacy_maps_old_rows_once():
    from loop.promises import import_legacy

    first = ledger.record_promise(who="阿蓝", what="旧事一", chat_id="c1")
    second = ledger.record_promise(who="小蓝", what="旧事二", chat_id="c2")
    ensure_promise_goal(
        "c1", promise_id=first["id"], what="旧事一", at=first["at"]
    )

    assert import_legacy() >= 1
    assert "promise:%s" % first["id"] in promise_goal_ids("c1")
    assert "promise:%s" % second["id"] in promise_goal_ids("c2")

    before = len(
        [e for e in get_world_model("c2").events if e.kind == "intention_opened"]
    )
    import_legacy()
    after = len(
        [e for e in get_world_model("c2").events if e.kind == "intention_opened"]
    )
    assert after == before, "迁移必须幂等，不能每启动一次多开一个目标"
