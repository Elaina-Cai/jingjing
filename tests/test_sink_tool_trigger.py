#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M2 第二触发：工具反复现查 → 候选（纯函数，不碰 DB）。

覆盖：同群同词条 3 次现查（双写折一）、问句/私聊/非外部工具排除、多来源合并。
归属：docs/梗与群术语设计.md（M2 触发面）。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.quality.memory_sinking import collect_tool_candidates, merge_candidates  # noqa: E402

CHAT = "88888888@chatroom"
PRIV = "wxid_owner_x"


def _row(tool: str, args: dict, at: str, chat: str = CHAT) -> dict:
    return {
        "chat_id": chat,
        "content": tool,
        "action_payload_json": json.dumps({"action": tool, "input": args}, ensure_ascii=False),
        "occurred_at": at,
    }


def test_repeated_term_lookup() -> None:
    rows = [
        _row("acg_lookup", {"keyword": "kxkx"}, "2026-09-26 10:00:00"),
        _row("acg_lookup", {"keyword": "kxkx"}, "2026-09-26 10:00:00.003"),  # 双写：并入
        _row("acg_lookup", {"keyword": "kxkx"}, "2026-09-26 10:30:00"),
        _row("web_search", {"query": "kxkx"}, "2026-09-26 11:00:00"),
    ]
    out = collect_tool_candidates(rows, min_occurrences=3)
    assert len(out) == 1, out
    assert out[0]["term"] == "kxkx" and out[0]["occurrences"] == 3, out
    print("  ok   同群同词条 3 次现查（双写折一）→ 候选")


def test_ignores_private_sentence_and_other_tools() -> None:
    rows = []
    for t in ("10:00:00", "10:10:00", "10:20:00"):
        rows.append(_row("web_search", {"query": "咕咕嘎嘎 什么意思"}, f"2026-09-26 {t}"))
        rows.append(_row("web_search", {"query": "kxkx"}, f"2026-09-26 {t}", chat=PRIV))
        rows.append(_row("search_messages", {"query": "kxkx"}, f"2026-09-26 {t}"))
    out = collect_tool_candidates(rows, min_occurrences=3)
    assert out == [], out
    print("  ok   问句 / 私聊 / 非外部工具 全部排除")


def test_merge_sums_sources() -> None:
    recall = [
        {"chat_id": CHAT, "term": "kxkx", "occurrences": 2, "best_score": 0.6,
         "memory_ids": ["proj:aaa"]}
    ]
    tool = [
        {"chat_id": CHAT, "term": "kxkx", "occurrences": 1, "best_score": 0.0,
         "memory_ids": [], "source": "tool"}
    ]
    out = merge_candidates(recall, tool)
    assert len(out) == 1 and out[0]["occurrences"] == 3, out
    assert set(out[0]["sources"]) == {"recall", "tool"}, out[0]
    print("  ok   多来源合并（2+1=3，来源标注）")


def main() -> int:
    test_repeated_term_lookup()
    test_ignores_private_sentence_and_other_tools()
    test_merge_sums_sources()
    print("sink tool trigger: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
