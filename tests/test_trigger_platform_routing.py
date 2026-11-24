# -*- coding: utf-8 -*-
"""群触发路径的平台解析回归锁（2026-10-15 P1）。

QQ 的 chat_id 带 group:/user: 前缀；触发路径此前硬编码 wechat，
把 QQ 群回合交给微信 UI 发送（搜索 group:681195563 失败、中止）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.routing.turn_pipeline import _platform_of_chat  # noqa: E402

SRC = ROOT / "core/routing/turn_pipeline.py"


def test_platform_of_chat():
    assert _platform_of_chat("group:681195563") == "qq"
    assert _platform_of_chat("user:1768136519") == "qq"
    assert _platform_of_chat("10000000001@chatroom") == "wechat"
    assert _platform_of_chat("wxid_abc") == "wechat"
    assert _platform_of_chat("") == "wechat"


def test_parse_chat_id_cross_check():
    from core.execution.qq_provider import parse_chat_id

    kind, target = parse_chat_id("group:681195563")
    assert kind == "group" and target == "681195563"


def _trigger_source() -> str:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    lines = SRC.read_text(encoding="utf-8").splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_generate_trigger_turn":
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError("_generate_trigger_turn not found")


def test_trigger_path_has_no_wechat_literal():
    src = _trigger_source()
    assert '"wechat"' not in src and "'wechat'" not in src
    assert "_platform_of_chat(chat_id)" in src


TESTS = [
    test_platform_of_chat,
    test_parse_chat_id_cross_check,
    test_trigger_path_has_no_wechat_literal,
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
        print(f"trigger platform checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
