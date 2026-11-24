# -*- coding: utf-8 -*-
"""C · 依据不作数的经验，不许再当方法用。

施工单 §4 那一条：「错范围或过期证据不会影响选择」。

判据（`belief_commit.alive`）：证据行还在吗、还是本会话的吗、还是别人说的人话吗。
**一条经验挂多条证据时，只要还有一条活着就留着**——不因为一条旧引用废掉整条经验；
也绝不因为「读不到库」就悄悄废掉（fail open）。

微信撤回不删原行。采集层可把 `revokemsg` 按明确消息号或唯一本地提示链到原事件；
链接无法确认时仍按未知处理，不把相近消息猜成被撤回的那条。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.context import cognitive_commit as cc
from core.context import reflection
from core.memory import belief_commit
from core.terminal import shadow
from loop import brain

SCOPE = cc.Scope("wechat", "a", "same")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """别落到 data/world.db：裸 shell 跑 pytest 时会被生产库守卫拦下。"""
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    yield


class _Store:
    def __init__(self, rows: dict):
        self.rows = rows

    def get(self, ref: str):
        return self.rows.get(ref)


def _row(ref: str, **over):
    base = {"event_id": ref, "platform": "wechat", "bot_account_id": "a", "chat_id": "same",
            "event_type": "message", "metadata": {}}
    base.update(over)
    return SimpleNamespace(**base)


def _store(monkeypatch, rows):
    monkeypatch.setattr(shadow, "get_default_store", lambda: _Store(rows))


def _alive_map(monkeypatch, verdict: dict):
    """直接把「这条证据还算不算数」判死判活——过滤逻辑本身与库解耦。"""
    monkeypatch.setattr(belief_commit, "evidence_state",
                        lambda ref, scope: "alive" if verdict.get(str(ref), True) else "dead")


def _lesson(monkeypatch, refs, text="先查索引再定位", method="read_file"):
    conn = cc.connect()

    class _Fake:
        evidence_refs = tuple(refs)
        confidence = 0.8
        method = "read_file"
        lesson = text
        revision = 1
        outcome = "success"
        source = "experience_tool"

        def __str__(self):
            return text

    monkeypatch.setattr(reflection, "current_lessons_for_scope", lambda scope: (_Fake(),))
    monkeypatch.setattr(reflection, "render_lesson", lambda lesson, **kw: text)
    conn.close()
    return method


def test_lesson_with_live_evidence_stays(monkeypatch):
    store = _Store({"evt:1": _row("evt:1")})
    assert belief_commit.evidence_state("evt:1", SCOPE, store=store) == "alive"
    _alive_map(monkeypatch, {"evt:1": True})
    _lesson(monkeypatch, ["evt:1"])
    detail: dict = {}
    block = brain._lessons_block(SCOPE, "查索引", detail=detail)
    assert "先查索引再定位" in block and not detail.get("evidence_dead")


def test_lesson_whose_evidence_is_provably_dead_disappears(monkeypatch):
    _alive_map(monkeypatch, {"evt:dead": False})
    _lesson(monkeypatch, ["evt:dead"])
    detail: dict = {}
    assert brain._lessons_block(SCOPE, "查索引", detail=detail) == ""
    assert detail.get("reason") == "evidence_dead" and detail.get("evidence_dead") == 1


def test_missing_row_is_unknown_and_keeps_the_lesson(monkeypatch):
    """事件库会轮转：行找不到 ≠ 证据死了。拿不准就留着。"""
    store = _Store({})
    assert belief_commit.evidence_state("evt:gone", SCOPE, store=store) == "unknown"
    _lesson(monkeypatch, ["evt:gone"])
    assert "先查索引再定位" in brain._lessons_block(SCOPE, "查索引")


def test_evidence_from_another_chat_does_not_count(monkeypatch):
    """真口径走 alive：跨会话的行不算数（这里直接验 alive 自己）。"""
    store = _Store({"evt:x": _row("evt:x", chat_id="other")})
    assert belief_commit.evidence_state("evt:x", SCOPE, store=store) == "dead"
    _alive_map(monkeypatch, {"evt:x": False})
    _lesson(monkeypatch, ["evt:x"])
    assert brain._lessons_block(SCOPE, "查索引") == ""


def test_her_own_message_is_not_evidence(monkeypatch):
    store = _Store({"evt:mine": _row("evt:mine", metadata={"is_self": True})})
    assert belief_commit.evidence_state("evt:mine", SCOPE, store=store) == "dead"
    _alive_map(monkeypatch, {"evt:mine": False})
    _lesson(monkeypatch, ["evt:mine"])
    assert brain._lessons_block(SCOPE, "查索引") == ""


@pytest.mark.parametrize("kind", ["question", "request", "command"])
def test_human_speech_types_are_alive_evidence(kind):
    store = _Store({"evt:human": _row("evt:human", event_type=kind)})
    assert belief_commit.evidence_state(
        "evt:human",
        SCOPE,
        store=store,
    ) == "alive"


def test_one_live_ref_rescues_the_lesson(monkeypatch):
    _alive_map(monkeypatch, {"evt:gone": False, "evt:2": True})
    _lesson(monkeypatch, ["evt:gone", "evt:2"])
    assert "先查索引再定位" in brain._lessons_block(SCOPE, "查索引")


def test_store_unreadable_keeps_the_lesson(monkeypatch):
    """读不到库 ≠ 证据失效。宁可留着经验，也不许悄悄废掉。"""
    def boom():
        raise RuntimeError("库挂了")

    monkeypatch.setattr(shadow, "get_default_store", boom)
    _lesson(monkeypatch, ["evt:1"])
    assert "先查索引再定位" in brain._lessons_block(SCOPE, "查索引")


def test_scope_must_be_complete_to_judge():
    assert belief_commit.evidence_state("evt:1", cc.Scope("wechat", "", "same")) == "dead"
    assert belief_commit.evidence_state("", SCOPE) == "dead"
