# -*- coding: utf-8 -*-
"""Step 11：世界窗口（原「行动视图」）渲染与开关测试。

2026-10-23 改名：块头 `[读场]` → `[世界窗口]`——`[读场]` 原来归
``agents/conversational.py::_read_block``（N1 的 semantic_reads），两个事实源不能共用一个头；
当晚 `_read_block` 随读场块一并退役，`[读场]` 这个头在 system 侧不再存在。
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.context.act_view import ACT_VIEW_HEADER, act_view_enabled, render_act_view  # noqa: E402
from core.context.assembler import _BLOCK_POLICY  # noqa: E402
from core.context.situation import WorldModel, apply_world_updates, note_position, open_intention  # noqa: E402


def test_act_view_off_by_default(monkeypatch):
    monkeypatch.delenv("YINYUE_FEATURE_GROUP_SCENE", raising=False)
    model = WorldModel()
    apply_world_updates(model, {"observe": [{"key": "P5.location", "value": "到了", "source": "self_reported"}]})
    assert act_view_enabled("c", True) is False
    assert render_act_view(model)  # 渲染本身与开关无关
    assert ACT_VIEW_HEADER in render_act_view(model)
    assert "[世界窗口]" in render_act_view(model)


def test_act_view_renders_belief_position_intention(monkeypatch):
    model = WorldModel()
    apply_world_updates(model, {"observe": [{"key": "P5.location", "value": "到了", "source": "self_reported"}]})
    note_position(model, "owner_teased", "主人最近被逗/被起哄")
    open_intention(model, "提醒:喝水", kind="remind", due_at="08:00")
    block = render_act_view(model)
    assert block.startswith(ACT_VIEW_HEADER)
    assert "P5.location" in block and "主人最近被逗" in block and "提醒:喝水" in block
    assert len(block.splitlines()) <= 9


def test_act_view_prefix_registered():
    prefixes = tuple(item[0] for item in _BLOCK_POLICY)
    assert "[世界窗口]" in prefixes
    # 负锁：读场块退役后 system 侧不再登记 `[读场]`（同头两源会让 trace/预算分不清谁是谁）
    assert prefixes.count("[读场]") == 0, "2026-10-23 退役：读场块不再登记"
    assert "[世界窗口]" not in ("[读场]",)


def test_act_view_shows_who_said_it(monkeypatch):
    """[世界窗口] 的事实行带「谁说的」（2026-10-13）。"""
    monkeypatch.delenv("YINYUE_FEATURE_GROUP_SCENE", raising=False)
    model = WorldModel()
    apply_world_updates(model, {"observe": [
        {"key": "P5.location", "value": "到了", "source": "self_reported",
         "speaker": "P5"},
    ]})
    block = render_act_view(model)
    assert "P5.location = 到了（P5 说的）" in block, block


def test_act_view_shows_who_said_what_to_whom(monkeypatch):
    """[世界窗口] 带「最近：谁 → 对谁：说了什么」，且有保底行位（Step 17）。"""
    monkeypatch.delenv("YINYUE_FEATURE_GROUP_SCENE", raising=False)
    from core.context.situation import observe_message

    model = WorldModel()
    observe_message(model, who="P5", targets=("P3",), text="我是真晕啊")
    observe_message(model, who="P3", targets=("P5",), text="要饿晕了")
    block = render_act_view(model)
    assert "最近：P5→P3：我是真晕啊" in block, block
    assert "最近：P3→P5：要饿晕了" in block, block


def test_act_view_filters_dead_facts_and_marks_unknown(monkeypatch):
    from core.memory import belief_commit

    model = WorldModel(scope={
        "platform": "wechat",
        "account_id": "bot-a",
        "chat_id": "room",
    })
    apply_world_updates(model, {"observe": [
        {
            "key": "dead.address",
            "value": "旧地址",
            "source": "self_reported",
            "evidence_refs": ["dead-event"],
        },
        {
            "key": "unknown.address",
            "value": "待核实地址",
            "source": "self_reported",
            "evidence_refs": ["missing-event"],
        },
    ]})
    original = belief_commit.evidence_state
    try:
        belief_commit.evidence_state = (  # type: ignore[assignment]
            lambda ref, scope: "dead" if ref == "dead-event" else "unknown"
        )
        block = render_act_view(model, verify_evidence=True)
    finally:
        belief_commit.evidence_state = original  # type: ignore[assignment]
    assert "dead.address" not in block
    assert "unknown.address = 待核实地址" in block
    assert "[依据未核实]" in block


def test_act_view_filters_revoked_intentions(monkeypatch):
    from core.memory import belief_commit

    model = WorldModel(scope={
        "platform": "wechat",
        "account_id": "bot-a",
        "chat_id": "room",
    })
    open_intention(
        model,
        "dead.request",
        kind="act",
        at="08:00",
        due_at="09:00",
        evidence_refs=("dead-event",),
    )
    open_intention(
        model,
        "unknown.request",
        kind="act",
        at="08:00",
        due_at="09:00",
        evidence_refs=("missing-event",),
    )
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "dead" if ref == "dead-event" else "unknown",
    )

    block = render_act_view(model, verify_evidence=True)

    assert "dead.request" not in block
    assert "unknown.request" in block
    assert "[依据未核实]" in block


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
