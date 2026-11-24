# -*- coding: utf-8 -*-
"""D-3：完成条件与最终报告的口径。

一条纪律：**引用存在只证明登记过，不证明已经送到。**
这里钉三件事：

1. 找到第一篇时能说出「还差什么」（完成条件 + 已有证据 + 还缺哪类）；
2. 「资料找齐 / 产物保存 / 投递动作 / 本机观察 / 平台送达」各自按证据说，
   认不出的引用不许被升级成「已送达」；
3. 复核回合的提示词里真的带着这行，不是一个没人调用的函数。
"""
from __future__ import annotations

from dataclasses import asdict

import pytest

from core.context import goal_report, situation, situation_store as store
from core.context.cognitive_commit import Scope
from core.context.goal_wake import pending_wakes
from loop import goal_review as review

NOW = "2026-11-18 10:00:00"
SCOPE = Scope("wechat", "a", "c1")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("DATA_DIR", "LOG_DIR", "PLATFORMS_DIR", "YINYUE_WORLD_MODEL_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("META_DB_PATH", str(tmp_path / "meta.db"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("YINYUE_GOAL_REVIEW_WAKE", "1")
    store.reset_world_models()
    yield
    store.reset_world_models()


def _goal(refs=(), done_when="两份独立结果都符合条件且已保存"):
    model = store.get_world_model("c1", scope=SCOPE)
    situation.open_intention(
        model, "整理两篇资料", kind="act", at="2026-11-18 09:00",
        evidence="你帮我整理两篇资料",
        desired_outcome="两篇资料都整理好、各自保存",
        done_when=done_when,
        next_action="还差第二篇",
        review_at=NOW, evidence_refs=tuple(refs), scope="chat:c1",
    )
    return next(g for g in situation.open_intentions(model) if g.key == "整理两篇资料")


def _prompt(goal_key="整理两篇资料") -> str:
    model = store.get_world_model("c1", scope=SCOPE)
    item = asdict(next(w for w in pending_wakes(model) if w.goal_id == goal_key))
    return review.prompt([item])


def test_unknown_reference_is_never_promoted_to_delivery():
    assert goal_report.proof_of("artifact:1") == "产物保存"
    assert goal_report.proof_of("queue:887") == "投递动作"
    assert goal_report.proof_of("ack:wx-1") == "平台送达"
    # 认不出的引用不许猜，更不许当成送达。
    assert goal_report.proof_of("聊天里说了一句") == ""
    assert "平台送达" in goal_report.missing_classes(["artifact:1", "queue:887"])


def test_one_artifact_still_names_what_is_missing():
    goal = _goal(refs=("artifact:1",))
    text = goal_report.status(goal)
    assert "两份独立结果都符合条件且已保存" in text
    assert "artifact:1" in text
    assert "尚未取得证据" in text and "平台送达" in text
    # 一条产物引用不能被读成「三件事都办了」：其余三类必须明写在缺的里边。
    assert "尚未取得证据：投递动作、本机观察、平台送达" in text


def test_full_evidence_leaves_nothing_missing():
    goal = _goal(refs=("artifact:1", "artifact:2", "queue:9", "work_item:r1", "ack:wx-9"))
    assert goal_report.missing_classes(("artifact:1", "artifact:2", "queue:9", "work_item:r1", "ack:wx-9")) == ()
    assert "尚未取得证据" not in goal_report.status(goal)


def test_review_prompt_carries_condition_and_missing_evidence():
    _goal(refs=("artifact:1",))
    text = _prompt()
    assert "整理两篇资料" in text
    assert "完成条件：两份独立结果都符合条件且已保存" in text
    assert "已登记证据：artifact:1" in text
    assert "尚未取得证据" in text


def test_prompt_does_not_add_noise_for_goals_without_a_contract():
    """没写完成条件、也没证据的目标，多一行都是噪音。"""
    model = store.get_world_model("c1", scope=SCOPE)
    situation.open_intention(model, "随口答应的事", kind="promise", review_at=NOW, desired_outcome="看看情况")
    assert goal_report.line(next(g for g in situation.open_intentions(model) if g.key == "随口答应的事")) == ""


def test_review_turn_still_uses_review_goal_not_completion():
    """复核不等于完成：带上这行之后，目标依然是开着的。"""
    _goal(refs=("artifact:1",))
    _prompt()
    model = store.get_world_model("c1", scope=SCOPE)
    assert [g.key for g in situation.open_intentions(model)] == ["整理两篇资料"]
