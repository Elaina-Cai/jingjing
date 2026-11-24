# -*- coding: utf-8 -*-
"""批A3：④情绪模块（理解时刻的积分与衰减；只算不存）。

覆盖：
* 行为信号只读时序/角色（回她 + 等待阶梯；群聊跳过等待阶梯）；
* 理解证据的结构化映射（维护/晾着/被起哄-命中她）；
* 有增量才写事件；who/who_id 按人记录；
* 读时衰减（半衰期回归），以及「先衰减再加 delta」的累积口径。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.emotion import (  # noqa: E402
    current_emotion,
    derive_behavior_signals,
    derive_understanding_evidence,
    update_emotion,
)
from core.context.situation import WorldModel, note_emotion, project  # noqa: E402

NOW = datetime(2026, 3, 1, 12, 0, 0)


def _msg(*, speaker="P3", minutes_ago=5, content="在吗", is_self=False):
    return SimpleNamespace(
        speaker=speaker, content=content, is_self=is_self,
        occurred_at=NOW - timedelta(minutes=minutes_ago),
    )


def _data(*, rows=(), admitted_rows=(), is_group=False, current="在吗",
          sender_name="P3", sender_id="wxid_p3", self_names=("鲸鲸",), now=NOW,
          explicit_address=False, summons=(), about_me=""):
    return SimpleNamespace(
        window_rows=tuple(rows), admitted_rows=tuple(admitted_rows),
        current=current, is_group=is_group, sender_name=sender_name,
        sender_id=sender_id, self_names=tuple(self_names), now=now, chat_id="c1",
        explicit_address=explicit_address, summons=tuple(summons), about_me=about_me,
    )


def test_behavior_signals_reply_to_her_uses_wait_ladder() -> None:
    data = _data(rows=[_msg(speaker="鲸鲸", is_self=True, minutes_ago=3 * 24 * 60)])
    signals = derive_behavior_signals(data)
    assert signals.replied_to_her is True
    assert signals.waited_minutes > 4000          # 3 天 -> wait_day 档

    assert derive_behavior_signals(_data(rows=[_msg()])).owner_initiated is True
    assert derive_behavior_signals(_data(rows=())).owner_initiated is False


def test_behavior_signals_group_rule_skips_wait_ladder() -> None:
    data = _data(
        rows=[_msg(speaker="鲸鲸", is_self=True, minutes_ago=3000)],
        is_group=True,
        explicit_address=True,
    )
    signals = derive_behavior_signals(data)
    assert signals.owner_initiated is True
    assert signals.replied_to_her is False
    # 2026-10-16 语义门：群聊里没对她说话的 ambient 回合不再计正面积分
    ambient = _data(
        rows=[_msg(speaker="鲸鲸", is_self=True, minutes_ago=3000)],
        is_group=True,
    )
    assert derive_behavior_signals(ambient).owner_initiated is False


def test_understanding_evidence_mapping() -> None:
    data = _data()
    assert derive_understanding_evidence(data, {"owner_state": "在维护她"}) == (0.20, "defended")
    assert derive_understanding_evidence(data, {"owner_state": "被晾着"}) == (-0.25, "left_out")
    assert derive_understanding_evidence(
        data, {"genre": "起哄", "targets": ["鲸鲸"]},
    ) == (-0.10, "teased")
    # 起哄但没点到她：不移动她的状态
    assert derive_understanding_evidence(
        data, {"genre": "起哄", "targets": ["别人"]},
    ) == (0.0, "")


def test_update_emotion_writes_evidence_event_with_who() -> None:
    model = WorldModel()
    result = update_emotion(
        model, data=_data(sender_name="P5", sender_id="wxid_pan"),
        section={"owner_state": "在维护她"}, at="2026-03-01 12:00",
    )
    assert result is not None and result.level > 0
    view = project(model.events).emotion
    assert view.reason == "defended"
    assert view.who == "P5" and view.who_id == "wxid_pan"
    assert view.level == result.level


def test_update_emotion_no_signal_writes_nothing() -> None:
    model = WorldModel()
    assert update_emotion(
        model, data=_data(), section={"owner_state": "中性"}, at="2026-03-01 12:00",
    ) is None
    assert project(model.events).emotion.updated_at == ""
    assert project(model.events).emotion.level == 0.0


def test_current_emotion_decays_with_time() -> None:
    model = WorldModel()
    note_emotion(model, level=-0.5, reason="left_out", at="2026-03-01 00:00")
    stored = current_emotion(model)
    assert stored.level == -0.5 and stored.updated_at == "2026-03-01 00:00"
    faded = current_emotion(model, now=NOW)        # 12h = 一个半衰期
    assert -0.5 < faded.level < 0
    assert faded.reason == "left_out"


def test_update_emotion_decays_before_applying_new_delta() -> None:
    model = WorldModel()
    first = update_emotion(
        model, data=_data(), section={"owner_state": "在维护她"},
        at="2026-03-01 00:00",
    )
    assert first is not None
    # 12 小时（一个半衰期）后再一次维护：0.20 先衰减到 0.10，再加 0.20 = 0.30
    second = update_emotion(
        model, data=_data(now=NOW), section={"owner_state": "在维护她"},
        at="2026-03-01 12:00",
    )
    assert second is not None
    assert abs(second.level - 0.30) < 0.02, second.level
    assert second.level > first.level


def test_reason_doctrine_keeps_old_cause_until_new_evidence_dominates() -> None:
    model = WorldModel()
    note_emotion(
        model, level=-0.5, reason="left_out",
        who="P3", who_id="wxid_p3", at="2026-03-01 00:00",
    )
    # 12h = 一个半衰期：-0.5 先衰减到 -0.25，再加 +0.20 维护 = -0.05（仍未转正）
    result = update_emotion(
        model, data=_data(now=NOW), section={"owner_state": "在维护她"},
        at="2026-03-01 12:00",
    )
    assert result is not None and result.level < 0
    assert result.reason == "left_out"   # 新起因没压倒旧状态：不许把状态说成被维护


def test_behavior_signals_prefer_full_window_over_admitted_subset() -> None:
    # 完整窗口：P3 一小时前来过、她半小时前回了一条 → 真相是"她在等 P3"。
    # 准入子集把她的发言过滤掉了，只剩 P3 那条；若用它读时序会误判成"他主动来"。
    data = _data(
        rows=[_msg(minutes_ago=60), _msg(speaker="鲸鲸", is_self=True, minutes_ago=30)],
        admitted_rows=[_msg(minutes_ago=60)],
    )
    signals = derive_behavior_signals(data)
    assert signals.owner_initiated is False
    assert signals.replied_to_her is True
    assert abs(signals.waited_minutes - 30.0) < 1e-6


def test_emotion_state_is_per_person_and_returning_speaker_continues() -> None:
    from datetime import datetime as _dt

    model = WorldModel()
    t0 = _dt(2026, 3, 1, 0, 0)
    t6 = _dt(2026, 3, 1, 6, 0)
    t12 = _dt(2026, 3, 1, 12, 0)

    first = update_emotion(
        model, data=_data(sender_name="P5", sender_id="wxid_pan", now=t0),
        section={"owner_state": "在维护她"}, at="2026-03-01 00:00",
    )
    assert first is not None and abs(first.level - 0.20) < 1e-9

    # 6h 后换 P3：不许把P5衰减后的 0.14 当自己的底子，P3 从 0 起算。
    second = update_emotion(
        model, data=_data(sender_name="P3", sender_id="wxid_p3", now=t6),
        section={"owner_state": "在维护她"}, at="2026-03-01 06:00",
    )
    assert second is not None and abs(second.level - 0.20) < 1e-9

    # 又 6h 后 P3 再来说：接 P3 自己 06:00 那条（0.20 → 0.1414 + 0.20 = 0.3414），
    # 不是全局最新的P5，也不是 P3 上一次的原始 0.20。
    third = update_emotion(
        model, data=_data(sender_name="P3", sender_id="wxid_p3", now=t12),
        section={"owner_state": "在维护她"}, at="2026-03-01 12:00",
    )
    assert third is not None and abs(third.level - 0.3414) < 0.005, third.level
    assert project(model.events).emotion.who_id == "wxid_p3"


# ---------------------------------------------------------------------------
# standalone runner（项目约定：python tests/test_emotion.py；与 pytest 双兼容）
# ---------------------------------------------------------------------------

def main() -> bool:
    checks = [
        test_behavior_signals_reply_to_her_uses_wait_ladder,
        test_behavior_signals_group_rule_skips_wait_ladder,
        test_understanding_evidence_mapping,
        test_update_emotion_writes_evidence_event_with_who,
        test_update_emotion_no_signal_writes_nothing,
        test_current_emotion_decays_with_time,
        test_update_emotion_decays_before_applying_new_delta,
        test_reason_doctrine_keeps_old_cause_until_new_evidence_dominates,
        test_behavior_signals_prefer_full_window_over_admitted_subset,
        test_emotion_state_is_per_person_and_returning_speaker_continues,
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
    print(f"emotion checks: {passed}/{len(checks)} passed")
    return passed == len(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
