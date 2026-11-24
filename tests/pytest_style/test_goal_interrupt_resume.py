# -*- coding: utf-8 -*-
"""D：连续任务（建立 → 推进 → 中断恢复 → 取消 → 验收）的端到端契约。

用的是现役目标真源：``situation.open_intention`` / ``goal_wake.note_goal_signal``
/ ``open_intentions`` / ``close_intention`` + 世界模型落盘。不新增状态机，
只把「进程重启后还认不认、取消后还动不动、重复证据算不算两次」钉住。

外部动作（真发消息、真跑 Job）没有授权，这一片全部用本地证据替身：
``artifact:*`` 是合成产物引用，不代表任何平台送达。
"""
from __future__ import annotations

import pytest

from core.context import situation
from core.context.goal_wake import note_goal_signal, pending_wakes
from core.context.situation_store import get_world_model, reset_world_models
from core.execution.data_channels import get_world, save_world

SCOPE = {"platform": "wechat", "account_id": "bot", "chat_id": "c1", "chat_type": "private"}
KEY = "整理两篇资料"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    reset_world_models()
    yield
    reset_world_models()


def _open_full(chat: str = "c1", key: str = KEY) -> None:
    model = get_world(chat, scope={**SCOPE, "chat_id": chat})
    situation.open_intention(
        model, key, kind="act", at="2026-11-17 20:00",
        evidence="你帮我整理两篇符合条件的资料",
        desired_outcome="两篇符合条件的资料都整理好、各自保存",
        done_when="两份独立结果都符合条件且已保存",
        next_action="先找第一篇",
        review_at="2026-11-18 09:00",
        evidence_refs=("msg:req-1",), scope="chat:" + chat,
        origin="owner_request", motivation="他让我办这件事",
    )
    assert save_world(chat, scope=model.scope)


def _reload(chat: str = "c1"):
    reset_world_models()  # 等价于进程重启：内存缓存清空，只剩盘上的世界模型
    return get_world_model(chat, scope={**SCOPE, "chat_id": chat})


def test_goal_survives_restart_with_scope_and_next_step():
    _open_full()
    model = _reload()
    goal = next(g for g in situation.open_intentions(model) if g.key == KEY)
    assert goal.desired_outcome == "两篇符合条件的资料都整理好、各自保存"
    assert goal.done_when == "两份独立结果都符合条件且已保存"
    assert goal.next_action == "先找第一篇"
    assert goal.review_at == "2026-11-18 09:00"
    assert "msg:req-1" in goal.evidence_refs


def test_first_result_keeps_goal_open_and_duplicate_ref_is_ignored():
    _open_full()
    model = get_world("c1", scope=SCOPE)
    assert note_goal_signal(model, KEY, source="evidence", ref="artifact:1",
                            at="2026-11-17 21:00", detail="第一篇已保存") is True
    assert save_world("c1", scope=model.scope)
    reloaded = _reload()
    assert [g.key for g in situation.open_intentions(reloaded)] == [KEY]
    wakes = pending_wakes(reloaded)
    assert any(w.reason == "evidence" for w in wakes)
    # 同一引用重复上报不是第二次进展。
    assert note_goal_signal(reloaded, KEY, source="evidence", ref="artifact:1",
                            at="2026-11-17 21:05") is False


def test_empty_tool_result_is_not_progress():
    _open_full()
    model = get_world("c1", scope=SCOPE)
    assert note_goal_signal(model, KEY, source="receipt", ref="", at="2026-11-17 21:00") is False
    assert not [w for w in pending_wakes(model) if w.reason == "receipt"]


def test_cancel_before_action_stops_future_signals():
    _open_full()
    model = get_world("c1", scope=SCOPE)
    situation.close_intention(model, KEY, at="2026-11-17 20:30", result="他说不用了", state="abandoned")
    assert save_world("c1", scope=model.scope)
    reloaded = _reload()
    assert not situation.open_intentions(reloaded)
    assert note_goal_signal(reloaded, KEY, source="evidence", ref="artifact:1",
                            at="2026-11-17 21:00") is False
    assert not situation.open_intentions(reloaded)


def test_late_result_after_cancel_does_not_reactivate():
    _open_full()
    model = get_world("c1", scope=SCOPE)
    assert note_goal_signal(model, KEY, source="evidence", ref="artifact:1", at="2026-11-17 21:00")
    assert save_world("c1", scope=model.scope)
    model = get_world("c1", scope=SCOPE)
    situation.close_intention(model, KEY, at="2026-11-17 21:30", result="用户取消", state="abandoned")
    assert save_world("c1", scope=model.scope)
    reloaded = _reload()
    # 迟到的第二份结果只能作为审计事实，不能把目标重新打开。
    assert note_goal_signal(reloaded, KEY, source="evidence", ref="artifact:2",
                            at="2026-11-17 22:00") is False
    assert not situation.open_intentions(reloaded)


def test_replayed_request_creates_one_goal():
    from core.context.promise_goal import ensure_promise_goal, promise_goal_ids

    first = ensure_promise_goal("c1", promise_id="p-1", what="整理两篇资料", at="2026-11-17 20:00",
                                scope=SCOPE)
    second = ensure_promise_goal("c1", promise_id="p-1", what="整理两篇资料", at="2026-11-17 20:01",
                                 scope=SCOPE)
    assert first == second == "promise:p-1"
    assert promise_goal_ids("c1", scope=SCOPE) == frozenset({"promise:p-1"})
    model = _reload()
    assert len([g for g in situation.open_intentions(model) if g.key == "promise:p-1"]) == 1
