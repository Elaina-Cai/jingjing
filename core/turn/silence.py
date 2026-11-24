"""沉默台账：一条消息没回，必须能回答"为什么没回"。

主人 2026-10-23 的判据是"语义交回模型"，所以代码不再决定要不要说话。
但"她没回"这个结果必须留痕：历史上「该接没接」和「按规矩不该接」
在日志里长得一模一样（都只有一行 route_no_reply，或者干脆什么都没有）。

纪律：
* 只记账，不改行为——调用点就在拿到"不回"的结论之后，返回值一律 None。
* ``cause`` 是封闭词表（``SILENT_CAUSES``），判词原文另存 ``reason``；
  不认识的原因归 ``other`` 但原文必须留下，绝不静默丢弃。
* 失败一律吞掉：记账不许拖垮路由或回合。
"""

from __future__ import annotations

from typing import Any
from safelog import warn

SILENT_CAUSES: tuple[str, ...] = (
    "self_sent",               # 自己发的（只有拍自己不跳）
    "pat_other",               # 拍的不是她
    "pat_grace_group",         # 群里纯拍：交给群状态机挂宽限，不是"不接"
    "mention_not_addressed",   # 只是提到名字，不是对她说的
    "no_reply_mode",           # 策略表给的模式是 never / defer / 认不出
    "ignored_group",           # 这个群在忽略名单里
    "group_buffered",          # 进了群缓冲，等窗口/合并
    "group_expired",           # 宽限期过了没有再叫
    "noise_gate",              # 零 token 的刷屏判据跳过
    "perceive_skip",           # 会话内判断说这条不接
    "perceive_defer",          # 会话内判断说等会儿
    "silent_token",            # 模型自己吐了 [SILENT]
    "no_reply_text",           # 跑完了但没出正文
    "stale_or_empty",          # 空消息 / 过期触发
    "other",
)

# 路由/回合侧判词 -> 台账 cause。判词原文会一起记进 reason，不丢信息。
_ALIASES: dict[str, str] = {
    "self_sent": "self_sent",
    "pat_not_self": "pat_other",
    "patted_in_group": "pat_grace_group",
    "patted_in_private": "pat_grace_group",
    "mention_not_addressed": "mention_not_addressed",
    "never": "no_reply_mode",
    "defer": "no_reply_mode",
    "unknown_reply_mode": "no_reply_mode",
    "ignored_group": "ignored_group",
    "stale_or_empty": "stale_or_empty",
    "silent": "silent_token",
}


def cause_of(reason: Any) -> str:
    """把路由/回合给的判词折成词表里的 ``cause``（认不出就 other）。"""
    text = str(reason or "").strip()
    if not text:
        return "other"
    if text in SILENT_CAUSES:
        return text
    if text in _ALIASES:
        return _ALIASES[text]
    head = text.split(":", 1)[0].strip()
    if head in _ALIASES:
        return _ALIASES[head]
    if head in SILENT_CAUSES:
        return head
    return "other"


def note_silenced(
    cause: Any,
    *,
    chat_id: str = "",
    msg_id: str = "",
    why: str = "",
    is_group: Any = None,
    where: str = "",
    log: Any = None,
) -> None:
    """记一条「这条没回」。只记，不返回值，永不抛。"""
    try:
        raw = str(cause or "").strip()
        folded = cause_of(raw)
        if log is None:
            import structlog

            log = structlog.get_logger()
        fields: dict[str, Any] = {
            "cause": folded,
            "chat_id": str(chat_id or ""),
            "msg_id": str(msg_id or ""),
        }
        if where:
            fields["where"] = str(where)
        if raw and raw != folded:
            fields["reason"] = raw[:120]
        if why:
            fields["why"] = str(why)[:200]
        if is_group is not None:
            fields["is_group"] = bool(is_group)
        log.info("turn_silenced", **fields)
    except Exception:  # noqa: BLE001 - 记账不许影响主流程
        warn("core/turn/silence.py:101 Exception", err=repr(__import__("sys").exc_info()[1]))