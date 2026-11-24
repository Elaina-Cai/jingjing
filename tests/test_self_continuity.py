# -*- coding: utf-8 -*-
"""连续性（批D Step 49）检查：standalone + pytest 兼容。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import self_contract as sc  # noqa: E402
from core.context.situation import WorldModel, note_self_message  # noqa: E402

ENV = "YINYUE_FEATURE_SELF_CONTINUITY"


def _model(lines):
    model = WorldModel()
    for i, text in enumerate(lines):
        note_self_message(model, content=text, at=f"2026-10-14 2{i}:00")
    return model


def test_mode_default_off():
    old = os.environ.get(ENV)
    try:
        os.environ.pop(ENV, None)
        assert sc.continuity_mode() == "off"
        os.environ[ENV] = "shadow"
        assert sc.continuity_mode() == "shadow"
        os.environ[ENV] = "on"
        assert sc.continuity_mode() == "on"
    finally:
        if old is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old


def test_recent_self_lines_last_and_order():
    lines = sc.recent_self_lines(_model(["一", "二", "三", "四", "五", "六"]))
    assert lines == ("二", "三", "四", "五", "六")


def test_recent_self_lines_max_chars():
    text = "这句话非常长" * 30
    lines = sc.recent_self_lines(_model([text]), limit=5, max_chars=120)
    assert lines == (text,)


def test_duplicate_of_exact_and_contained():
    lines = ("今晚吃烤鱼。", "数据库那玩意儿可勤快了。", "没漏。")
    assert sc.duplicate_of("今晚吃烤鱼。", lines) == "今晚吃烤鱼。"
    assert sc.duplicate_of("数据库那玩意儿可勤快了", lines) == "数据库那玩意儿可勤快了。"
    assert sc.duplicate_of("好", lines) == ""
    assert sc.duplicate_of("完全不同的说法", lines) == ""


def test_duplicate_of_does_not_flag_long_quote():
    lines = ("没漏。",)
    assert sc.duplicate_of("没漏。刚才那条就是答他的", lines) == ""


def test_self_continuity_block_renders():
    block = sc.self_continuity_block(("今晚吃烤鱼。", "没漏。"))
    assert "[你说过·作数]" in block
    assert "- 今晚吃烤鱼。" in block and "- 没漏。" in block
    assert "自相矛盾" in block
    assert sc.self_continuity_block(()) == ""


def test_continuity_scope_default_and_white_list() -> None:
    import os

    old = os.environ.get("YINYUE_SELF_CONTINUITY_CHATS")
    try:
        os.environ.pop("YINYUE_SELF_CONTINUITY_CHATS", None)
        assert sc.continuity_scope() == ()
        assert sc.in_continuity_scope("any") is True
        os.environ["YINYUE_SELF_CONTINUITY_CHATS"] = "g1, g2"
        assert sc.continuity_scope() == ("g1", "g2")
        assert sc.in_continuity_scope("g1") is True
        assert sc.in_continuity_scope("g3") is False
    finally:
        if old is None:
            os.environ.pop("YINYUE_SELF_CONTINUITY_CHATS", None)
        else:
            os.environ["YINYUE_SELF_CONTINUITY_CHATS"] = old


def test_continuity_scope_is_wired() -> None:
    out = (ROOT / "core/speech/tone_readings.py").read_text(encoding="utf-8")
    assert "in_continuity_scope" in out


def test_static_wiring_and_purity():
    from core import features

    assert features.SELF_CONTINUITY == "self_continuity"
    assert features.SELF_CONTINUITY in features.FLAG_REGISTRY
    import inspect

    import agents.conversational as conv_module

    block_src = inspect.getsource(conv_module._self_continuity_block)
    assert "self_continuity_shadow" in block_src, "续接块里没有影子读数"
    mod = (ROOT / "core/context/self_contract.py").read_text(encoding="utf-8")
    for banned in ("sqlite3", "core.bootstrap", "requests", "httpx"):
        assert banned not in mod, banned


TESTS = [
    test_mode_default_off,
    test_recent_self_lines_last_and_order,
    test_recent_self_lines_max_chars,
    test_duplicate_of_exact_and_contained,
    test_duplicate_of_does_not_flag_long_quote,
    test_self_continuity_block_renders,
    test_continuity_scope_default_and_white_list,
    test_continuity_scope_is_wired,
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
        print(f"self-continuity checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
