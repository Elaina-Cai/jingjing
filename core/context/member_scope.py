"""群成员名单的"相关人"口径（2026-10-23）。

问题：名单块以前每轮把整张名册推给模型。实测生产里最大的群 73 人，而一条消息
真正用到的只有 1–2 个人 —— 命中率 <3%，却每轮付满额 token。这与"三小时后不成立
的信息不该占着注意力"是同一条纪律。

做法：只渲染「这一轮相关的人」，其余折成一句计数 + 一句"要查谁调工具"。
相关的人按优先级取，最多 ``MAX_RELEVANT`` 个：

1. 当前发言人（``sender_name``）
2. 本轮被引用/回复的人（``quoted_speaker`` / ``referent.name``）
3. 正文里 @ 到的人（``mentions``）
4. （已退役）N1 判出的收件人——那个结论对象随 N1 一起删掉了，这一档不再有输入。

纯函数、不碰库、不读环境：选择规则必须可单测。
"""

from __future__ import annotations

from typing import Any
from safelog import warn

#: **不设人数上限**：相关的人有几个就列几个（"@ 了六个人"是真实场景，硬截会
#: 把该认得的人裁掉）。只设**字数预算**，超了才截，并且必须明说"还有 N 位没列"。
MAX_BLOCK_CHARS = 600
#: 一个相关的人都没有时，兜底列最活跃的几个人（按 message_count 已排序）
FALLBACK_TOP = 2


def _clean(value: Any) -> str:
    return str(value or "").strip()


def relevant_names(extra: Any) -> list[str]:
    """按优先级列出「这一轮相关的人」的名字（去重、保序、不按人数截断）。"""
    data = extra if hasattr(extra, "get") else {}
    names: list[str] = []
    sender = _clean(data.get("sender_name"))
    if sender:
        names.append(sender)
    for key in ("quoted_speaker", "quoted_sender_name"):
        value = _clean(data.get(key))
        if value:
            names.append(value)
    referent = data.get("referent")
    if isinstance(referent, dict):
        for key in ("name", "display_name"):
            value = _clean(referent.get(key))
            if value:
                names.append(value)
    mentions = data.get("mentions") or ()
    if isinstance(mentions, (list, tuple)):
        names.extend(_clean(item) for item in mentions if _clean(item))
    out: list[str] = []
    for name in names:
        if name not in out:
            out.append(name)
    return out


def select_rows(
    rows: list[Any],
    names: list[str],
    *,
    max_chars: int = MAX_BLOCK_CHARS,
    detail_len: Any = None,
) -> tuple[list[Any], int]:
    """挑出相关的人，**按字数预算截**（不按人数）。

    返回 ``(选中的行, 未列出的相关人数)``：

    - 相关的人一个都不少地进来，直到累计字数超过 ``max_chars``；
    - 超了的部分不静默丢：调用方要用返回的第二项写一句"还有 N 位没列"；
    - 一个都没命中时退回前 ``FALLBACK_TOP`` 行（按活跃度），未列出数 = 0。

    ``detail_len`` 可选：``callable(row) -> int``，把逐人备注的字数算进预算。
    ``rows`` 元素第 0 位是昵称。
    """
    wanted = set(names)
    picked: list[Any] = []
    used = 0
    skipped = 0
    for row in rows:
        if _clean(row[0]) not in wanted:
            continue
        cost = len(_clean(row[0])) + 8
        if detail_len is not None:
            try:
                cost += int(detail_len(row) or 0)
            except Exception:  # noqa: BLE001 - 估长失败按名字算
                warn("core/context/member_scope.py:89 Exception", err=repr(__import__("sys").exc_info()[1]))
        if picked and used + cost > max_chars:
            skipped += 1
            continue
        picked.append(row)
        used += cost
    if picked:
        return picked, skipped
    return list(rows[:FALLBACK_TOP]), 0