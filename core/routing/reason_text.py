# -*- coding: utf-8 -*-
"""触发原因的唯一渲染 owner（WP-08 renderer 方案 Phase 1/2/3）。

判定只读结构化字段（``reason`` / ``trigger_reasons``）；本模块只负责把结构化真相 +
路径事实渲染成"人看/进 prompt/进 trace"的字符串。

Phase 3（签字：全文案 + 中性连接词 + 未知原样透出）：
* direct：逐段映射人话，段间 ``，同时``；``content`` 段固定 ``，另外``；
  未知 token 原样输出（不编造语义）；``conversation_active`` 是最终原因，
  压过折叠前的旧路由段（防御性，正常时序下 1745 行 replace 已写同值）；
* trigger：按 route segment 输出人话整句（B2，2026-10-10 签字）；显式 route_label_text 兼容旧模板。

策略/审计侧读 ``turn_decision.structured_reason``，不要读本模块输出。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core import temporal
from core.routing.turn_decision import route_segment
from safelog import warn

#: direct 段词面：token -> (段首文案, 复合续接文案)。
#: tail 用于省略重复主语/地点；没有独立 tail 的段沿用 full。
_DIRECT_FACES: dict[str, tuple[str, str]] = {
    "mentioned_in_group": ("有人在群里 @ 了她", " @ 了她"),
    "patted_in_group": ("有人在群里拍了拍她", "拍了拍她"),
    "quoted_in_group": ("有人在群里引用了她", "引用了她"),
    "name_call": ("有人在群里叫了她的名字", "叫了她的名字"),
    "patted_in_private": ("有人在私聊里拍了拍她", "在私聊里拍了拍她"),
    "owner_private_message": ("主人私聊了她", "主人私聊了她"),
    "non_owner_private_urgent": ("非主人私聊，且标了紧急", "非主人私聊，且标了紧急"),
    "web_chat": ("网页端有人来找她说话", "网页端有人来找她说话"),
    "conversation_active": ("对话仍在进行，她接了话", "对话仍在进行，她接了话"),
    "engagement_fallback": ("策略解析失败，按保守默认接了话", "策略解析失败，按保守默认接了话"),
    "group_deferred_to_state": ("群里的话先挂起，等状态变化", "群里的话先挂起，等状态变化"),
    "ignored_group": ("这个群被她忽略了", "这个群被她忽略了"),
    "owner_group_message": ("主人在群里说了话", "主人在群里说了话"),
    "addressed_other": ("群里这句话是说给别人的", "群里这句话是说给别人的"),
    "default": ("群里的普通消息", "群里的普通消息"),
    "ambient": ("群里的环境回合", "群里的环境回合"),
    "group_ambient": ("群里的环境回合", "群里的环境回合"),
    "deferred": ("延迟窗口里的话", "延迟窗口里的话"),
    "pat_after_reply": ("她刚回过这个人，随后又被拍了拍", "她刚回过这个人，随后又被拍了拍"),
    "stale_or_empty": ("消息过期或没有可判内容", "消息过期或没有可判内容"),
    "pat_not_self": ("这条拍不是冲她来的", "这条拍不是冲她来的"),
    "self_sent": ("这条是她自己发的", "这条是她自己发的"),
}
_CONTENT_REASON = "content"
_CONTENT_FACE = "这条还带了内容"

#: trigger 分支词面：route segment -> 人话整句（sender/elapsed 占位）。
_TRIGGER_TEMPLATES: dict[str, str] = {
    "mentioned_in_group": "群里{sender}{elapsed} @ 了她",
    "patted_in_group": "群里{sender}{elapsed}拍了拍她",
    "ambient": "群里{sender}{elapsed}说的话进了环境接话",
    "deferred": "群里{sender}{elapsed}说的话先挂起，等状态变化",
}
_TRIGGER_FALLBACK = "群里{sender}{elapsed}说的话没有触发她"


def _render_trigger_phrase(decision: Any, *, sender_name: str, elapsed: str) -> str:
    """trigger 路径整句人话（B2 签字）；未知/空段统一用 fallback。"""
    template = _TRIGGER_TEMPLATES.get(route_segment(decision), _TRIGGER_FALLBACK)
    return template.format(sender=sender_name, elapsed=elapsed)


def is_pure_pat(decision: Any) -> bool:
    """只有"拍"这一段、没有折叠进 @/引用等其它段（等价旧字符串等值的直回语义）。"""
    return tuple(getattr(decision, "trigger_reasons", ()) or ()) == ("patted_in_group",)


def has_pat(decision: Any) -> bool:
    """本轮触发段里是否含"拍"（折叠过的也算）。"""
    return "patted_in_group" in tuple(getattr(decision, "trigger_reasons", ()) or ())


def _direct_pieces(decision: Any) -> tuple[str, ...]:
    """显示用段序列：最终原因优先；否则 trigger_reasons；否则 reason 按 ``+`` 拆。"""
    reason = str(getattr(decision, "reason", "") or "")
    if reason == "conversation_active":
        return ("conversation_active",)
    reasons = tuple(
        str(item) for item in (getattr(decision, "trigger_reasons", ()) or ())
        if str(item or "")
    )
    if reasons:
        return reasons
    if not reason:
        return ()
    return tuple(part.strip() for part in reason.split("+") if part.strip())


def _render_direct(pieces: tuple[str, ...]) -> str:
    """逐段人话 + 中性连接词；未知 token 原样透出（策略自定义 key 不丢线索）。"""
    out = ""
    for index, piece in enumerate(pieces):
        if piece == _CONTENT_REASON:
            out = _CONTENT_FACE if index == 0 else out + "，另外" + _CONTENT_FACE
            continue
        face = _DIRECT_FACES.get(piece)
        if index == 0:
            out = face[0] if face else piece
            continue
        if face is None:
            out += "，另外 " + piece
        else:
            out += "，同时" + (face[1] or face[0])
    return out


def render_reason_text(
    decision: Any,
    *,
    sender_name: str = "",
    message_time: datetime | None = None,
    route_label_text: str = "",
    source_override: str = "",
) -> str:
    """唯一渲染入口：结构化 decision + 路径事实 -> 显示文本（任何异常不得抛）。"""
    if decision is None:
        return ""
    try:
        source = str(source_override or getattr(decision, "source", "") or "")
        if source == "trigger":
            elapsed = temporal.elapsed_phrase(message_time) if message_time is not None else ""
            if route_label_text:
                # 兼容位：显式 label 仍走旧模板（现网无调用者，留给回放/外部）
                return f"群里{str(sender_name or '')}{elapsed}说的话触发了{route_label_text}"
            return _render_trigger_phrase(
                decision, sender_name=str(sender_name or ""), elapsed=elapsed,
            )
        return _render_direct(_direct_pieces(decision))
    except Exception:  # noqa: BLE001 - 渲染失败按空串，绝不拖垮回合
        warn("core/routing/reason_text.py:134 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
