# -*- coding: utf-8 -*-
"""Volition 契约（批D 拟人决策链步2）检查。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts.volition import POSTURE_UNKNOWN, build_volition  # noqa: E402


def test_silence_and_posture_mapping():
    v = build_volition(
        drive={"decision": "silence", "reason": "not_about_her", "path": "none", "hooks": []},
        section={"about": "room", "about_her": False},
    )
    assert v.speak is False and v.impulse == "none" and v.posture == "沉默"
    assert v.about_subject == "room" and v.about_her is False


def test_answer_summoned_posture():
    v = build_volition(
        drive={"decision": "speak", "path": "answer", "reason": "answer", "hooks": ["被叫到"]},
        section={"about": "her", "about_her": True, "targets": ["Elaina"], "genre": "分享", "about_confidence": 0.9},
    )
    assert v.speak is True and v.posture == "答" and v.target == "Elaina"
    assert v.confidence == 0.9 and v.about_subject == "her" and v.about_her is True


def test_proactive_topic_mapping():
    v = build_volition(
        drive={"decision": "speak", "path": "proactive_topic", "reason": "proactive_topic", "hooks": ["话题命中"]},
        section={"about": "other", "genre": "玩笑"},
    )
    assert v.posture == "逗"
    v2 = build_volition(
        drive={"decision": "speak", "path": "proactive_topic", "reason": "proactive_topic", "hooks": ["话题命中"]},
        section={"about": "other", "genre": "分享"},
    )
    assert v2.posture == "接"


def test_guardian_posture():
    v = build_volition(
        drive={"decision": "speak", "path": "proactive_strong", "reason": "proactive_strong", "hooks": ["护主"]},
        section={"about": "other", "genre": "玩笑"},
    )
    assert v.posture == "护"


def test_to_dict_roundtrip():
    v = build_volition(
        drive={"decision": "speak", "path": "answer", "reason": "answer", "hooks": []},
        section={}, mood=0.5, tools_on=True,
    )
    d = v.to_dict()
    assert d["speak"] is True and d["impulse"] == "answer"
    assert d["mood_gate"] == 0.5 and d["needs_tools"] is True
    assert set(d) == {
        "speak", "impulse", "posture", "confidence", "reason", "target", "hooks",
        "about_subject", "about_her", "genre", "mood_gate", "budget_allowed",
        "needs_tools", "must_not", "task_stance", "task_goal", "task_next",
        "delivery_hint",
    }


def test_static_wiring_and_purity():
    import inspect

    assert not (ROOT / "core/context/scene_effects.py").exists(), \
        "场景运行段随 N1 退役（意愿影子读数不再由它记）"
    mod = (ROOT / "core/contracts/volition.py").read_text(encoding="utf-8")
    for banned in ("sqlite3", "core.bootstrap", "llm", "log."):
        assert banned not in mod, banned




def test_delivery_hint_derivation():
    v = build_volition(
        drive={"decision": "speak", "path": "proactive_topic", "reason": "proactive_topic", "hooks": ["话题命中"]},
        section={"about": "other", "genre": "玩笑"},
    )
    assert v.delivery_hint == "short"
    v2 = build_volition(
        drive={"decision": "speak", "path": "answer", "reason": "answer", "hooks": ["被叫到"]},
        section={"about": "her", "genre": "分享"},
    )
    assert v2.delivery_hint == "short"
    v3 = build_volition(
        drive={"decision": "speak", "path": "answer", "reason": "answer", "hooks": []},
        section={"about": "her", "genre": "任务"},
    )
    assert v3.delivery_hint == "normal"
    v4 = build_volition(
        drive={"decision": "speak", "path": "answer", "reason": "answer", "hooks": [], "task_goal": "整理文档"},
        section={"about": "her"},
    )
    assert v4.delivery_hint == "deliver"


def test_unknown_first_class_posture_off_keeps_legacy():
    """P0-3 off（默认）：无判据仍落「接」，逐字旧行为。"""
    v = build_volition(
        drive={"decision": "speak", "path": "proactive_topic",
               "reason": "proactive_topic", "hooks": ["话题命中"]},
        section={"about": "other", "genre": "分享"},
    )
    assert v.posture == "接"


def test_unknown_first_class_posture_on_is_empty():
    """P0-3 on：没有判据的姿态 = 未判出（空串），不是「接」。"""
    v = build_volition(
        drive={"decision": "speak", "path": "proactive_topic",
               "reason": "proactive_topic", "hooks": ["话题命中"]},
        section={"about": "other", "genre": "分享"},
        unknown_first_class=True,
    )
    assert v.posture == POSTURE_UNKNOWN == ""
    answer = build_volition(
        drive={"decision": "speak", "path": "answer", "reason": "answer", "hooks": []},
        section={},
        unknown_first_class=True,
    )
    assert answer.posture == "答"


def test_band_mapping_static():
    """出稿参数：N1 的 Volition 退役后没有读数来源，分档只剩一条（固定 0.8/500）。"""
    import types

    from agents.conversational import ConversationalAgent

    def band(hint):
        agent = types.SimpleNamespace(
            _last_context=types.SimpleNamespace(extra={"volition": {"delivery_hint": hint}})
        )
        return ConversationalAgent._synthesize_band(agent)

    # 负锁：三个旧档位全部塌成默认档（hint 已无人写，读到什么都得是 0.8/500）。
    for hint in ("short", "deliver", "normal", ""):
        assert band(hint) == (0.8, 500), (hint, band(hint))
    bare = types.SimpleNamespace(_last_context=None)
    assert ConversationalAgent._synthesize_band(bare) == (0.8, 500)
    assert "DELIVERY_HINTS" in (ROOT / "core/contracts/volition.py").read_text(encoding="utf-8")


TESTS = [
    test_silence_and_posture_mapping,
    test_answer_summoned_posture,
    test_proactive_topic_mapping,
    test_guardian_posture,
    test_to_dict_roundtrip,
    test_delivery_hint_derivation,
    test_band_mapping_static,
    test_unknown_first_class_posture_off_keeps_legacy,
    test_unknown_first_class_posture_on_is_empty,
    test_static_wiring_and_purity,
]
def main() -> bool:
    ok = True
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
            ok = False
        else:
            print(f"PASS {test.__name__}")
    if ok:
        print(f"volition checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
