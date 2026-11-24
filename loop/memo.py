"""常驻板：我和这个人的关系与约定。每拍给她看一小段（<=300 字）。

和账的区别：
- 账（ledger）记"这一拍我干了什么"，是流水，下一拍读完就旧了。
- 常驻板记"我和这个人之间长期有效的事"——答应过什么、他忌讳什么、上次为什么闹别扭。
  这些不随时间消失，所以每拍都给她看一眼，只给最相关的那几条。

2026-11-08：写入统一到 notes.jsonl（现役唯一写口）；memo.jsonl 只读兼容，不再双写。
"""
from __future__ import annotations

import json
import os

from .safelog import warn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "data", "loop")
FILE = os.path.join(DIR, "memo.jsonl")  # 旧库：只读，不再追加

KINDS = {
    "promise": "答应过",
    "fact": "事实",
    "rule": "规矩",
    "note": "记着",
}


def add(
    text: str,
    *,
    kind: str = "note",
    target: str = "",
    at: str | None = None,
    evidence_refs: tuple[str, ...] = (),
    extra: dict | None = None,
) -> dict | None:
    """写一条常驻记忆。target 是"对谁"（会话名或 chat_id），空表示范围未知，不自动公开。

    权威写入是 loop.notes（notes.jsonl）；memo.jsonl 仅保留旧版本内容供只读兼容。
    保留旧契约：空文本返回 None 且不落盘；正文截到 200 字，避免单条挤占整块常驻板。
    """
    from loop import notes

    body = (text or "").strip()[:200]
    if not body:
        return None
    kind = kind if kind in KINDS else "note"
    row = notes.add(
        kind,
        body,
        target=(target or "").strip(),
        at=at or "",
        evidence_refs=evidence_refs,
        extra=extra,
    )
    return row or None


def _rows() -> list[dict]:
    """只读旧 memo.jsonl；新写入统一在 notes.jsonl。"""
    if not os.path.exists(FILE):
        return []
    out: list[dict] = []
    for line in open(FILE, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            warn("memo:53 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
    return out


def resident(target: str = "", *, budget: int = 300) -> str:
    """渲染当前会话与明确共享的常驻记忆；范围/排序/去重统一由 notes 提供。"""
    from loop import notes

    picked = notes.residents(target, budget)
    if not picked:
        return ""
    head = "[我和这个人的关系与约定]" if (target or "").strip() else "[我知道的事]"
    return head + "\n" + "\n".join("- " + line for line in picked)
