"""执行侧关闭：先写认知库，写不进去才本地兜底（防重放）。

这条链路与对话侧共用 `goal_close.commit_close`：
- runner 只决定「该关哪条」并把执行事实 id 交给回调；
- tick / bridge 提供回调，真正写认知库并标记派生；
- 写失败时 runner/bridge 退回本地关闭——不是双写，是「已经执行过的动作不能重放」。
"""
import asyncio

import pytest

from core.context import situation
from core.context.cognitive_commit import (
    Scope,
    connect,
    invalidate_closed_cache,
    read_state,
)
from core.context.goal_close import commit_close
from core.context.intention_runner import IntentionExecution, run_due_retryable
from core.context.intention_tick import run_tick
from core.context.situation_store import get_world_model, reset_world_models

KEY = "提醒:喝水"
DUE = "2026-11-07 10:00"
NOW = "2026-11-07 10:01"
SCOPE = Scope("wechat", "bot", "c1", "private", "")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch, request):
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setenv("YINYUE_FEATURE_INTENTION_TICK", "on")
    from types import SimpleNamespace
    from core.terminal import store_port
    status = "failed" if "failure_is_abandoned" in request.node.name else "done"
    row = SimpleNamespace(source_platform="wechat", source_account_id="bot", source_chat_id="c1", status=status)
    monkeypatch.setattr(store_port, "get_repo", lambda: SimpleNamespace(reminders=SimpleNamespace(get=lambda key: row if key == 1 else None)))
    invalidate_closed_cache()
    reset_world_models()
    yield
    reset_world_models()
    invalidate_closed_cache()


def _model():
    model = situation.WorldModel(scope={"platform": "wechat", "account_id": "bot",
                                        "chat_id": "c1", "chat_type": "private"})
    situation.open_intention(model, KEY, kind="remind", due_at=DUE, evidence="提醒我喝水")
    return model


def _local_closes(model):
    return [
        event for event in model.events
        if event.kind == "intention_closed" and not event.payload.get("_derived")
    ]


def test_runner_closes_through_the_cognitive_commit():
    model = _model()
    calls = []

    def close(key, at, result, evidence_ref, state=""):
        calls.append((key, at, result, evidence_ref, state))
        return commit_close(
            model, key, at=at, result=result, evidence_ref=evidence_ref
        )

    async def execute(_intent):
        return IntentionExecution(ok=True, result="sent", evidence_ref="reminder:1")

    results = asyncio.run(run_due_retryable(model, NOW, execute, close=close))

    assert results[0].closed and results[0].evidence_ref == "reminder:1"
    assert calls == [(KEY, NOW, "sent", "reminder:1", "completed")]
    conn = connect()
    try:
        state = read_state(conn, KEY)
    finally:
        conn.close()
    assert state["operation"] == "close" and state["payload"]["result"] == "sent"
    assert _local_closes(model) == [], "写进认知库就不再留一份本地关闭"


def test_runner_falls_back_to_local_when_the_commit_fails():
    model = _model()

    def close(*_args):
        return False

    async def execute(_intent):
        return IntentionExecution(ok=True, result="sent", evidence_ref="reminder:1")

    results = asyncio.run(run_due_retryable(model, NOW, execute, close=close))

    assert results[0].closed
    assert len(_local_closes(model)) == 1, "执行已发生，写失败也必须本地关闭，防重放"
    conn = connect()
    try:
        assert read_state(conn, KEY) is None
    finally:
        conn.close()


def test_runner_failure_is_abandoned_not_completed():
    model = _model()

    async def execute(_intent):
        return IntentionExecution(ok=False, result="send_failed", evidence_ref="reminder:1")

    asyncio.run(run_due_retryable(model, NOW, execute, close=lambda *args: False))

    local = _local_closes(model)
    assert len(local) == 1
    assert local[0].payload["state"] == "abandoned"
    assert situation.project(model.events).intentions[KEY].state == "abandoned"


def test_tick_uses_the_same_commit_path():
    model = _model()

    async def execute(_chat_id, _intent):
        return IntentionExecution(ok=True, result="sent", evidence_ref="reminder:1")

    report = asyncio.run(run_tick(execute, now=NOW, models={"c1": model}))

    assert report.closed == 1
    conn = connect()
    try:
        assert read_state(conn, KEY)["operation"] == "close"
    finally:
        conn.close()
    assert _local_closes(model) == []


def test_bridge_close_uses_cognitive_when_evidence_is_given():
    from core.context.intention_bridge import close_reminder_intention

    model = get_world_model("c1", scope=SCOPE)
    key = "reminder:1@%s" % DUE
    situation.open_intention(model, key, kind="remind", due_at=DUE, evidence="提醒我喝水")

    assert close_reminder_intention(
        "c1", 1, at=NOW, result="delivered", evidence_ref="reminder:1", scope=SCOPE
    )
    conn = connect()
    try:
        assert read_state(conn, key)["operation"] == "close"
    finally:
        conn.close()
    assert _local_closes(model) == []


def test_bridge_close_without_evidence_refuses_completion():
    from core.context.intention_bridge import close_reminder_intention

    model = get_world_model("c1", scope=SCOPE)
    key = "reminder:2@%s" % DUE
    situation.open_intention(model, key, kind="remind", due_at=DUE, evidence="提醒我喝水")

    assert not close_reminder_intention("c1", 2, at=NOW, result="delivered")
    assert _local_closes(model) == []
    assert situation.open_intentions(model)


def test_commit_close_requires_both_scope_and_evidence():
    model = _model()
    assert commit_close(model, KEY, evidence_ref="") is False

    orphan = situation.WorldModel()
    situation.open_intention(orphan, KEY, kind="remind", due_at=DUE)
    assert commit_close(orphan, KEY, evidence_ref="reminder:1") is False


# ---- 2026-11-09：重复提醒的执行证据（`reminder:<id>@<due>`）----
# 背景：重复提醒投递成功后 `mark_done` 把状态改回 pending、remind_at 推进到下一次
# （db/repo_reminder.py:113-120），原来只认 `status == "done"` 的判据永远为假，
# 导致每次成功都抛 goal_evidence_unverified。带 @due 后靠「时刻已推进」证明这一跳。

def _repeat_row(monkeypatch, *, status: str, remind_at):
    from types import SimpleNamespace
    from core.terminal import store_port

    row = SimpleNamespace(
        source_platform="wechat", source_account_id="bot", source_chat_id="c1",
        status=status, remind_at=remind_at,
    )
    monkeypatch.setattr(
        store_port, "get_repo",
        lambda: SimpleNamespace(reminders=SimpleNamespace(
            get=lambda key: row if key == 1 else None)),
    )
    return row


def test_a_repeat_reminder_that_advanced_proves_this_run(monkeypatch):
    """remind_at 已经不是这一跳的时刻 → 这一跳真的投出去了。"""
    from datetime import datetime
    from core.context.goal_close import _verified_ref

    _repeat_row(monkeypatch, status="pending", remind_at=datetime(2026, 11, 8, 10, 0))
    assert _verified_ref("reminder:1@2026-11-07 10:00", SCOPE, "completed") is True


def test_a_repeat_reminder_not_yet_advanced_is_not_proof(monkeypatch):
    """时刻还停在这一跳 → 不能算投过，否则会把「没发」写成完成。"""
    from datetime import datetime
    from core.context.goal_close import _verified_ref

    _repeat_row(monkeypatch, status="pending", remind_at=datetime(2026, 11, 7, 10, 0))
    assert _verified_ref("reminder:1@2026-11-07 10:00", SCOPE, "completed") is False


def test_a_bare_reminder_ref_still_requires_done(monkeypatch):
    """不带 @due 的旧引用维持原口径：只认 done，不拿 pending 凑。"""
    from datetime import datetime
    from core.context.goal_close import _verified_ref

    row = _repeat_row(monkeypatch, status="pending", remind_at=datetime(2026, 11, 8, 10, 0))
    assert _verified_ref("reminder:1", SCOPE, "completed") is False
    row.status = "done"
    assert _verified_ref("reminder:1", SCOPE, "completed") is True
