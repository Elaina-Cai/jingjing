# -*- coding: utf-8 -*-
"""Step 2.5：世界模型中枢（事件溯源 + BDI 投影）测试。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.situation import (  # noqa: E402
    WorldModel,
    apply_scene_read,
    apply_world_updates,
    assert_unknown,
    close_intention,
    confirm_hypothesis,
    contradict_hypothesis,
    open_intention,
    project,
    render_situation,
)


def test_observe_keeps_source() -> None:
    model = WorldModel()
    apply_world_updates(model, {"observe": [
        {"key": "P5.location", "value": "到了", "source": "self_reported"},
        {"key": "P4.file", "value": "缺数据文件", "source": "chat_observed"},
    ]}, at="08:10")
    view = project(model.events)
    assert view.beliefs["P5.location"].source == "self_reported"
    assert view.beliefs["P5.location"].value == "到了"
    assert view.beliefs["P4.file"].source == "chat_observed"


def test_revision_is_event_sourced() -> None:
    model = WorldModel()
    apply_world_updates(model, {"observe": [
        {"key": "P5.location", "value": "还没到", "source": "self_reported"},
    ]}, at="08:02")
    apply_world_updates(model, {"retract": [
        {"key": "P5.location", "reason": "后文我到了"},
    ]}, at="08:10")
    apply_world_updates(model, {"observe": [
        {"key": "P5.location", "value": "到了", "source": "self_reported"},
    ]}, at="08:10")
    assert len(model.events) == 3
    assert project(model.events).beliefs["P5.location"].value == "到了"


def test_hypothesis_confirmed_enters_beliefs() -> None:
    model = WorldModel()
    apply_world_updates(model, {"abduce": [
        {"key": "t3027.addressee", "value": "P5", "confidence": 0.6,
         "evidence": "P5刚到且P3在路上"},
    ]}, at="08:10")
    assert project(model.events).beliefs["t3027.addressee"].status == "pending"
    confirm_hypothesis(model, "t3027.addressee", at="08:08", evidence="拍P5")
    belief = project(model.events).beliefs["t3027.addressee"]
    assert belief.status == "active"
    assert belief.confidence >= 0.5


def test_hypothesis_contradicted_is_revoked() -> None:
    model = WorldModel()
    apply_world_updates(model, {"abduce": [
        {"key": "x", "value": "P5", "confidence": 0.6},
    ]})
    contradict_hypothesis(model, "x", reason="后文否定")
    assert project(model.events).beliefs["x"].status == "revoked"


def test_unknown_is_first_class_and_rendered() -> None:
    model = WorldModel()
    assert_unknown(model, "P3.file")
    belief = project(model.events).beliefs["P3.file"]
    assert belief.status == "unknown"
    assert belief.source == "unknown"
    text = render_situation(model)
    assert "P3.file" in text
    assert "群里还没说" in text


def test_intentions_open_and_close() -> None:
    model = WorldModel()
    open_intention(model, "P3会来吗", kind="observe", at="08:08")
    assert project(model.events).intentions["P3会来吗"].status == "open"
    assert "待办" in render_situation(model)
    close_intention(model, "P3会来吗", at="08:10")
    assert project(model.events).intentions["P3会来吗"].status == "closed"


def test_holdout_arrival_line_and_render_limit() -> None:
    model = WorldModel()
    apply_world_updates(model, {"observe": [
        {"key": "P5.location", "value": "到了", "source": "self_reported"},
        {"key": "P3.location", "value": "刚出门", "source": "self_reported"},
        {"key": "P2.location", "value": "还在宿舍", "source": "self_reported"},
        {"key": "P4.file", "value": "缺数据文件", "source": "chat_observed"},
    ]}, at="08:20")
    text = render_situation(model)
    assert "P5.location" in text and "P4.file" in text
    assert len(text.splitlines()) <= 6


def test_apply_scene_read_lands_updates_from_n1_section() -> None:
    model = WorldModel()
    section = {
        "roles": {"her": 0.1},
        "updates": [
            {"op": "observe", "key": "P5.location", "value": "到了",
             "source": "self_reported"},
            {"op": "abduce", "key": "t3027.addressee", "value": "P5",
             "confidence": 0.6, "evidence": "P5刚到"},
        ],
    }
    apply_scene_read(model, section, at="08:10")
    view = project(model.events)
    assert view.beliefs["P5.location"].value == "到了"
    assert view.beliefs["t3027.addressee"].status == "pending"
    assert view.beliefs["t3027.addressee"].confidence == 0.6


def test_position_evidence_projection() -> None:
    from core.context.situation import note_position, position_lines

    model = WorldModel()
    note_position(model, "owner_defended", "主人最近在维护她", at="08:20")
    view = project(model.events)
    assert view.desires["owner_defended"] == "主人最近在维护她"
    assert any("维护" in line for line in position_lines(model))


def test_observe_records_who_and_render_shows_it() -> None:
    """谁说的（2026-10-13）：事件存 who，渲染带「谁说的」。"""
    model = WorldModel()
    apply_world_updates(model, {"observe": [
        {"key": "P5.location", "value": "到了", "source": "self_reported",
         "speaker": "P5"},
        {"key": "P4.file", "value": "缺数据文件", "source": "chat_observed"},
    ]}, at="08:10")
    view = project(model.events)
    assert view.beliefs["P5.location"].who == "P5"
    assert view.beliefs["P4.file"].who == ""
    rendered = render_situation(model)
    assert "P5.location = 到了（P5 说的，self_reported）" in rendered, rendered
    # 没有说话人的旧形状逐字不变
    assert "P4.file = 缺数据文件（chat_observed）" in rendered, rendered


def test_message_observed_projects_who_targets_and_text() -> None:
    """Step 17：每轮「谁 → 对谁（集合）→ 说了什么」进中枢可查。"""
    from core.context.situation import observe_message, recent_messages

    model = WorldModel()
    observe_message(
        model, who="P5", targets=("P3", "P3", "P3"),
        text="我是真晕啊", genre="分享", at="10:03:55",
    )
    view = project(model.events)
    assert len(view.messages) == 1
    message = view.messages[0]
    assert message.who == "P5"
    assert message.targets == ("P3", "P3")      # 集合去重保序
    assert message.text == "我是真晕啊"
    lines = recent_messages(model)
    assert lines == ("P5→P3、P3：我是真晕啊",), lines


def test_message_observed_truncates_long_text() -> None:
    from core.context.situation import observe_message, recent_messages

    model = WorldModel()
    observe_message(model, who="P1", targets=(), text="长" * 300)
    message = project(model.events).messages[0]
    assert len(message.text) == 120 and message.text.endswith("…")
    assert "未判出" in recent_messages(model)[0]


def test_emotion_projection_tracks_latest_and_renders() -> None:
    """批A1：④情绪模块的事件进②，投影取最新一条并进局面渲染。"""
    from core.context.situation import EmotionState, note_emotion

    model = WorldModel()
    note_emotion(model, level=-0.4, reason="left_hanging", at="10:00")
    note_emotion(model, level=0.6, reason="owner_initiated", at="10:05")
    view = project(model.events)
    assert view.emotion == EmotionState(
        level=0.6, updated_at="10:05", reason="owner_initiated",
    )
    rendered = render_situation(model)
    assert "情绪：+0.6（owner_initiated）" in rendered, rendered


def test_emotion_default_adds_no_render_line() -> None:
    """没有情绪事件时，局面渲染与接线前逐字一致（不含情绪行）。"""
    model = WorldModel()
    apply_world_updates(model, {"observe": [
        {"key": "P5.location", "value": "到了", "source": "self_reported"},
    ]}, at="08:10")
    view = project(model.events)
    assert view.emotion.level == 0.0 and view.emotion.updated_at == ""
    assert "情绪：" not in render_situation(model)


def test_emotion_level_is_clamped_and_bad_values_are_steady() -> None:
    """level 只收 -1..+1；越界截断，坏值/布尔按 0（稳态），绝不猜。"""
    from core.context.situation import note_emotion

    model = WorldModel()
    note_emotion(model, level=5.0, at="10:00")
    assert project(model.events).emotion.level == 1.0
    note_emotion(model, level="高", at="10:01")
    assert project(model.events).emotion.level == 0.0
    note_emotion(model, level=True, at="10:02")
    assert project(model.events).emotion.level == 0.0


def test_emotion_projection_keeps_who_and_labels_render() -> None:
    """批A2：情绪按人记（显示名 + 稳定 id），渲染带上是谁的情绪。"""
    from core.context.situation import note_emotion

    model = WorldModel()
    note_emotion(
        model, level=0.6, reason="owner_initiated",
        who="P5", who_id="wxid_pan", at="10:05",
    )
    view = project(model.events)
    assert view.emotion.who == "P5" and view.emotion.who_id == "wxid_pan"
    assert "情绪（P5）：+0.6（owner_initiated）" in render_situation(model)


# ---------------------------------------------------------------------------
# standalone runner（项目约定：python tests/test_situation_state.py；与 pytest 双兼容）
# ---------------------------------------------------------------------------

def main() -> bool:
    checks = [
        test_observe_keeps_source,
        test_revision_is_event_sourced,
        test_hypothesis_confirmed_enters_beliefs,
        test_hypothesis_contradicted_is_revoked,
        test_unknown_is_first_class_and_rendered,
        test_intentions_open_and_close,
        test_holdout_arrival_line_and_render_limit,
        test_apply_scene_read_lands_updates_from_n1_section,
        test_position_evidence_projection,
        test_observe_records_who_and_render_shows_it,
        test_message_observed_projects_who_targets_and_text,
        test_message_observed_truncates_long_text,
        test_emotion_projection_tracks_latest_and_renders,
        test_emotion_default_adds_no_render_line,
        test_emotion_level_is_clamped_and_bad_values_are_steady,
        test_emotion_projection_keeps_who_and_labels_render,
    ]
    passed = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - 失败必须逐个报出来，不许静默
            print(f"  FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  ok   {check.__name__}")
    print(f"situation_state checks: {passed}/{len(checks)} passed")
    return passed == len(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
