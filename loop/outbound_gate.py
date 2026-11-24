"""全自动外发前的最后一道闸（用户 2026-11-04 选「全 C」）。

「全 C」＝她不再停下来等点头，外发一律自己做主。代价是**发出去收不回**，所以这道
闸有三道，都是"红线上不许自动做"，不是"判断该不该说"：

1. 内容闸（``loop/content_gate.py``）：钱、地址/联系方式、替别人答应、时间承诺
   —— 这四类由她自己自动发出去就收不回（2026-11-04，用户 m03054 的第 3 项）；
2. 同一句话在同一个会话里 120 秒内不发第二遍
   （E-45：模型与确定性通道各发一遍，群里真的出现了两条一样的话）；
3. ``reason`` 是 ``spont``（她自己找的话）时，同一个会话 300 秒内最多一条
   —— 回人家的问话（``reply``）不受这条限制，否则一问一答都做不了。
   ``job`` 留了位子：多步任务上线时由 ``loop/job_bridge.py`` 传进来。

判据是纯算术加一张进程内表：不读库、不看模型、不给内容打分。
被拦下来不是"失败"，是一条**可记账的裁决**（``ack="skipped"``），调用方必须写日志。
"""

from __future__ import annotations

import time
from typing import Any

#: 同一句原文的冷却（秒）
DEDUPE_WINDOW_S = 120.0
#: 主动开口（不是回话）时，同一个会话的最小间隔（秒）
PROACTIVE_GAP_S = 300.0
#: 哪些 reason 算"主动开口"
#: 2026-11-04：`job` 也进来了 —— 多步行动链每拍推一步，同一件事同一句话
#: 不该在两拍里连发两遍（E-45 双发的同源问题）。
PROACTIVE_REASONS: tuple[str, ...] = ("spont", "job")

_RECENT: dict[tuple[str, str], float] = {}
_LAST_PROACTIVE: dict[str, float] = {}


def _norm(text: Any) -> str:
    """比对用正文：空白归一、去掉句尾标点（同一句话换个句号不算新话）。"""
    t = " ".join(str(text or "").split())
    return t.rstrip("。.…、，,~～!！?？ ").strip()


def _now(now: Any = None) -> float:
    return float(now) if now is not None else time.time()


def check(chat_id: str, text: Any, *, reason: str = "reply", now: Any = None) -> tuple[bool, str]:
    """能不能发：``(True, "ok")`` 或 ``(False, 原因)``。**不记账**——真的排进队列后再 ``remember``。"""
    chat = str(chat_id or "")
    key = _norm(text)
    if not key:
        return False, "empty_text"
    # 内容闸排在最前：这几类话连"占窗口"都不该占（被拦下来的不算说过）。
    from . import content_gate as _cg
    _c_ok, _c_why = _cg.check(key)
    if not _c_ok:
        return False, _c_why
    ts = _now(now)
    last = _RECENT.get((chat, key))
    if last is not None and 0 <= ts - last < DEDUPE_WINDOW_S:
        return False, "duplicate"
    if str(reason or "") in PROACTIVE_REASONS:
        prev = _LAST_PROACTIVE.get(chat)
        if prev is not None and 0 <= ts - prev < PROACTIVE_GAP_S:
            return False, "too_soon"
    return True, "ok"


def remember(chat_id: str, text: Any, *, reason: str = "reply", now: Any = None) -> None:
    """记下"这句真的排进队列了"。只由调用方在入队成功后调（失败的不占窗口）。"""
    chat = str(chat_id or "")
    key = _norm(text)
    if not chat or not key:
        return
    ts = _now(now)
    _RECENT[(chat, key)] = ts
    if str(reason or "") in PROACTIVE_REASONS:
        _LAST_PROACTIVE[chat] = ts


def reset() -> None:
    """清空进程内表（只给测试用；生产没有"忘掉"这个操作）。"""
    _RECENT.clear()
    _LAST_PROACTIVE.clear()
