#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M3 注入口径：词条行「命中才注入」；非词条行常驻；多义卡同进同出；大小写不敏感。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.person_notes import (  # noqa: E402
    build_chat_context_block,
    is_lexicon_part,
    lexicon_term,
)

LINES = [
    "示例宿舍群：日常互相拆台、玩梗，玩笑为主 | kxkx=群里约面试的暗号 | fkxqs=青协缩写",
]


def test_helpers() -> None:
    assert is_lexicon_part("kxkx=群里约面试的暗号") is True
    assert lexicon_term("kxkx=群里约面试的暗号") == "kxkx"
    assert is_lexicon_part("示例宿舍群：日常拆台") is False
    assert is_lexicon_part("作业群成员（唯一准版）：P5=计算机25-4班；Elaina=计算机25-4班") is False
    assert is_lexicon_part("a=b=c") is False  # 两个等号不是词条行
    assert is_lexicon_part("这个词条名字太长了=释义") is False
    print("  ok   词条行判定（≤8 字、单等号）")


def test_focus_hit_only() -> None:
    base = build_chat_context_block("c@chatroom", lines=LINES)
    assert "kxkx=群里约面试的暗号" in base and "fkxqs=青协缩写" in base  # 无 focus = 旧行为
    miss = build_chat_context_block("c@chatroom", lines=LINES, focus_text="今天天气不错")
    assert "kxkx=" not in miss and "fkxqs=" not in miss
    assert "示例宿舍群" in miss  # 非词条行常驻
    hit = build_chat_context_block("c@chatroom", lines=LINES, focus_text="刚才开会说kxkx的事")
    assert "kxkx=群里约面试的暗号" in hit and "fkxqs=青协缩写" not in hit
    print("  ok   命中才注入（非词条行常驻）")


def test_case_insensitive_and_multisense() -> None:
    lines = ["kxkx=释义A | kxkx=释义B"]
    block = build_chat_context_block("c@chatroom", lines=lines, focus_text="KXKX 怎么用")
    assert "kxkx=释义A" in block and "kxkx=释义B" in block  # 多义同进
    assert len(block.splitlines()) == 3, block
    print("  ok   大小写不敏感 + 多义同进同出")


def main() -> int:
    test_helpers()
    test_focus_hit_only()
    test_case_insensitive_and_multisense()
    print("glossary injection: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
