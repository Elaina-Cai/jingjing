"""N3 话题簇事实桥（P1.2 话题线退役后的剩余面）。

WP-10 第 5 行（2026-10-10，owner 覆盖）：话题线 LLM producer / flag / 缓存
全部退役，[局况] 不再渲染话题行。本模块只保留：

* ``cluster_facts``：取 N3 owner（``core/context/threads.py``）的话题簇事实，
  供焦点/回放读取；开关关或取不到一律退空。
* ``_listing_line``：历史回放与测试用的逐行格式化（带 canonical 归属）。

不再有 ``YINYUE_FEATURE_TOPIC_LINE``、不再起后台刷新、不再调 LLM。
"""

from __future__ import annotations

from typing import Any, Sequence
from safelog import warn


def cluster_facts(rows: Sequence[Any]) -> str:
    """取 owner 的话题簇事实；**开关关 / 取不到 → ``""``**。"""
    if not threads_enabled():
        return ""
    try:
        from core.context.threads import refine_topic_line

        return str(refine_topic_line(rows) or "")
    except Exception:  # noqa: BLE001 - 事实取不到不拖垮调用方
        warn("core/context/topic_line.py:26 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def threads_enabled() -> bool:
    """``YINYUE_FEATURE_THREADS`` 是否在做事（读 owner 的唯一读数）。"""
    try:
        from core.context.threads import threads_enabled as _enabled

        return bool(_enabled())
    except Exception:  # noqa: BLE001 - 读不到开关 = 关（回滚位）
        warn("core/context/topic_line.py:36 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def _listing_line(row: Any) -> str:
    """一条消息的呈现行：带 canonical 归属（``→ @谁`` / ``[归属不明]``）。"""
    try:
        from core.context.attribution import default_context, render_attribution_line

        line = render_attribution_line(row, ctx=default_context(), always=True)
        if line.startswith("- "):
            return line[2:]
        if line:
            return line
    except Exception:  # noqa: BLE001 - 渲染失败退回旧形制
        warn("core/context/topic_line.py:50 Exception", err=repr(__import__("sys").exc_info()[1]))
    speaker = "鲸鲸" if getattr(row, "is_self", False) else (
        str(getattr(row, "speaker", "") or "?")
    )
    return f"{speaker}: {str(getattr(row, 'content', '') or '')[:80]}"


__all__ = [
    "cluster_facts",
    "threads_enabled",
]
