"""One internal turn decision (WP-05).

``ReplyMode`` (policy layer, kept as a data vocabulary) and the group scheduling
route translate into this single vocabulary.  The four route segments below are
the only place where the retired five-way trigger vocabulary still exists;
everything downstream reads ``TurnDecision``.

Mapping table: docs/当断则断_执行手册.md §5.1.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from core.speech.domain import EngagementDecision, ReplyMode
from safelog import warn

DEFAULT_DEFER_SECONDS = 15.0
DEFAULT_CANCEL_IF = ("human_answered",)

# ------------------------------------------------------------------ 路由段（WP-05 收尾）
# 旧五路触发词汇删除后，路由侧只剩四个段，由 ``TurnDecision.trigger_reasons[0]`` 承载：
# 它们只服务路由/审计，不再是一个独立分类器。四个字面量**只在这里定义一次**，
# group_state / router / relevance 只读这些常量；``route_segment`` 是唯一取段口，
# ``route_label`` 只给日志/prompt 保留旧词面（不参与任何判定）。
ROUTE_SEGMENT_MENTION = "mentioned_in_group"   # 旧 IMMEDIATE：被 @ / 引用 / 叫到
ROUTE_SEGMENT_AMBIENT = "ambient"              # 旧 AMBIENT：进判断的群回合
ROUTE_SEGMENT_DEFERRED = "deferred"            # 旧 DEFERRED：等 15s / 可取消
ROUTE_SEGMENT_PAT = "patted_in_group"          # 旧 PAT：纯拍（挂起或反问）
ROUTE_SEGMENTS = (
    ROUTE_SEGMENT_MENTION,
    ROUTE_SEGMENT_AMBIENT,
    ROUTE_SEGMENT_DEFERRED,
    ROUTE_SEGMENT_PAT,
)

_ROUTE_TO_TRIGGER = {
    ROUTE_SEGMENT_MENTION: "immediate",
    ROUTE_SEGMENT_AMBIENT: "ambient",
    ROUTE_SEGMENT_DEFERRED: "deferred",
    ROUTE_SEGMENT_PAT: "pat",
    "": "none",
}

_ROUTE_LABELS = {
    ROUTE_SEGMENT_MENTION: "immediate",
    ROUTE_SEGMENT_AMBIENT: "ambient",
    ROUTE_SEGMENT_DEFERRED: "deferred",
    ROUTE_SEGMENT_PAT: "pat",
}


def route_segment(decision: Any) -> str:
    """这一回合的路由段（``trigger_reasons[0]``）；无段 = 静默（旧 NONE）。"""
    reasons = tuple(getattr(decision, "trigger_reasons", ()) or ())
    return str(reasons[0]) if reasons else ""


def route_label(decision: Any) -> str:
    """路由段的旧词面（仅日志 / prompt 兼容），不参与判定。"""
    return _ROUTE_LABELS.get(route_segment(decision), "none")


def structured_reason(decision: Any) -> str:
    """结构化 reason 原样串（策略/审计唯一读口；显示走 reason_text.render_reason_text）。

    * ``trigger_reasons`` 非空 → ``+`` 连接（fold 后的复合形态）；
    * 否则退 ``reason`` 原样（含自定义规则 key）。
    """
    reasons = tuple(
        str(item) for item in (getattr(decision, "trigger_reasons", ()) or ())
        if str(item or "")
    )
    return "+".join(reasons) or str(getattr(decision, "reason", "") or "")


@dataclass(frozen=True)
class TurnDecision:
    """The only action vocabulary between policy/scheduling and generation."""

    action: Literal["reply_now", "defer", "silent"] = "silent"
    reason: str = ""
    reason_chain: tuple[str, ...] = ()
    defer_seconds: float = 0.0
    cancel_if: tuple[str, ...] = ()
    source: str = "trigger"
    trigger_reasons: tuple[str, ...] = ()
    relevance: Any = None
    reply_chat_id: str = ""
    reply_platform: str = "wechat"
    reply_account: str = "self"
    reply_account_id: str = ""
    sender_id: str = ""
    is_group: bool = False
    # WP-06 stage B：旧路由决策结构退役后平移过来的字段（外部读者保持可读）。
    needs_judge: bool = False
    active_intent: str = ""
    active_unknown_answer: bool = False
    #: 直回路径：agent 生成中调 send_sticker 先入队，等文字回复发完再发。
    #: typed 迁移（2026-10-10）：替代旧 extra["defer_sticker_until_after_reply"]；
    #: 触发路径没有 flush 口，保持 False。
    defer_sticker_until_after_reply: bool = False

    @property
    def should_reply(self) -> bool:
        return self.action == "reply_now"

    @property
    def should_defer(self) -> bool:
        return self.action == "defer"

    @property
    def is_silent(self) -> bool:
        return self.action == "silent"



def with_reason(
    decision: TurnDecision,
    *,
    reason: str | None = None,
    trigger_reasons: "tuple[str, ...] | None" = None,
) -> TurnDecision:
    """WP-08 renderer Phase 2：fold / 状态同步时生成新的 frozen decision。"""
    fields: dict[str, object] = {}
    if reason is not None:
        fields["reason"] = str(reason)
    if trigger_reasons is not None:
        fields["trigger_reasons"] = tuple(str(item) for item in trigger_reasons)
    if not fields:
        return decision
    try:
        from dataclasses import replace

        return replace(decision, **fields)
    except Exception:  # noqa: BLE001 - 非 dataclass（duck/测试）原地写回
        warn("core/routing/turn_decision.py:135 Exception", err=repr(__import__("sys").exc_info()[1]))
        for name, value in fields.items():
            try:
                setattr(decision, name, value)
            except Exception:  # noqa: BLE001
                warn("core/routing/turn_decision.py:139 Exception", err=repr(__import__("sys").exc_info()[1]))
        return decision


def with_active_reply(
    decision: TurnDecision,
    *,
    reply_platform: str,
    reply_account_id: str = "",
    reply_chat_id: str = "",
    active_intent: str = "",
    active_unknown_answer: bool = False,
) -> TurnDecision:
    """Conversation-active gate 的 frozen 改写（与旧就地赋值逐字段等价）。"""
    return replace(
        decision,
        action="reply_now",
        reason="conversation_active",
        reply_platform=reply_platform,
        reply_account="self",
        reply_account_id=reply_account_id or decision.reply_account_id,
        reply_chat_id=reply_chat_id,
        is_group=True,
        active_intent=active_intent,
        active_unknown_answer=active_unknown_answer,
    )


def _address(
    *,
    reply_chat_id: str = "",
    reply_platform: str = "wechat",
    reply_account: str = "self",
    reply_account_id: str = "",
    sender_id: str = "",
    is_group: bool = False,
) -> dict[str, Any]:
    return {
        "reply_chat_id": reply_chat_id,
        "reply_platform": reply_platform,
        "reply_account": reply_account,
        "reply_account_id": reply_account_id,
        "sender_id": sender_id,
        "is_group": is_group,
    }


def from_engagement(
    engagement: EngagementDecision,
    *,
    source: str = "direct",
    is_mentioned: bool = False,
    pat_on_self: bool = False,
    is_group: bool = False,
    reply_chat_id: str = "",
    reply_platform: str = "wechat",
    reply_account: str = "self",
    reply_account_id: str = "",
    sender_id: str = "",
) -> TurnDecision:
    """ReplyMode -> TurnDecision; the **only** mapping point (router branch)."""
    reason = str(getattr(engagement, "reply_reason", "") or "")
    address = _address(
        reply_chat_id=reply_chat_id, reply_platform=reply_platform,
        reply_account=reply_account, reply_account_id=reply_account_id,
        sender_id=sender_id, is_group=is_group,
    )
    mode = getattr(engagement, "reply_mode", None)
    if mode == ReplyMode.ALWAYS or mode == ReplyMode.URGENT:
        return TurnDecision(
            action="reply_now", reason=reason or mode.value,
            reason_chain=(f"engagement:{mode.value}",), source=source, **address,
        )
    if mode == ReplyMode.MENTION:
        if is_mentioned or (is_group and pat_on_self):
            return TurnDecision(
                action="reply_now", reason=reason or "mentioned_in_group",
                reason_chain=("engagement:mention",), source=source, **address,
            )
        return TurnDecision(
            action="silent", reason=reason or "mention_not_addressed",
            reason_chain=("engagement:mention_not_addressed",), source=source, **address,
        )
    if mode == ReplyMode.DEFER:
        seconds = float(getattr(engagement, "defer_seconds", 0.0) or DEFAULT_DEFER_SECONDS)
        return TurnDecision(
            action="defer", reason=reason or "group_deferred_to_state",
            reason_chain=("engagement:defer",), defer_seconds=seconds,
            cancel_if=DEFAULT_CANCEL_IF, source=source, **address,
        )
    if mode == ReplyMode.NEVER:
        return TurnDecision(
            action="silent", reason=reason or "never",
            reason_chain=("engagement:never",), source=source, **address,
        )
    return TurnDecision(
        action="silent", reason=f"unknown_reply_mode:{mode!r}",
        reason_chain=("engagement:unknown",), source=source, **address,
    )


def _trigger_value(trigger: Any) -> str:
    return str(getattr(trigger, "value", trigger) or "").strip().lower()


def from_trigger(
    trigger: Any,
    *,
    source: str = "trigger",
    relevance: Any = None,
    reason: str = "",
    trigger_reasons: tuple[str, ...] = (),
    pat_deferred: bool = False,
    defer_seconds: float = DEFAULT_DEFER_SECONDS,
    cancel_if: tuple[str, ...] = DEFAULT_CANCEL_IF,
    reply_chat_id: str = "",
    reply_platform: str = "wechat",
    reply_account: str = "self",
    reply_account_id: str = "",
    sender_id: str = "",
    is_group: bool = True,
) -> TurnDecision:
    """旧触发词面 -> TurnDecision（GroupState 分类的唯一映射点）。"""
    value = _trigger_value(trigger)
    address = _address(
        reply_chat_id=reply_chat_id, reply_platform=reply_platform,
        reply_account=reply_account, reply_account_id=reply_account_id,
        sender_id=sender_id, is_group=is_group,
    )
    relevance_action = str(getattr(relevance, "action", "") or "")
    relevance_reason = str(getattr(relevance, "reason", "") or "")
    relevance_chain = tuple(getattr(relevance, "chain", ()) or ())
    if (
        relevance is not None
        and relevance_action == "skip"
        and value in ("ambient", "deferred", "pat")
    ):
        return TurnDecision(
            action="silent", reason=relevance_reason,
            reason_chain=relevance_chain or ("relevance:skip",),
            source=source, trigger_reasons=trigger_reasons, relevance=relevance,
            **address,
        )
    if value in ("", "none"):
        return TurnDecision(
            action="silent", reason=reason or "stale_or_empty",
            reason_chain=("trigger:none",), source=source,
            trigger_reasons=trigger_reasons, relevance=relevance, **address,
        )
    if value == "immediate":
        return TurnDecision(
            action="reply_now", reason=reason or relevance_reason or "mentioned_in_group",
            reason_chain=relevance_chain or ("trigger:immediate",),
            source=source, trigger_reasons=trigger_reasons, relevance=relevance, **address,
        )
    if value == "pat":
        if pat_deferred:
            return TurnDecision(
                action="defer", reason=reason or relevance_reason or "patted_in_group",
                reason_chain=("trigger:pat_deferred",), defer_seconds=defer_seconds,
                cancel_if=cancel_if, source=source, trigger_reasons=trigger_reasons,
                relevance=relevance, **address,
            )
        return TurnDecision(
            action="reply_now", reason=reason or relevance_reason or "patted_in_group",
            reason_chain=relevance_chain or ("trigger:pat",),
            source=source, trigger_reasons=trigger_reasons, relevance=relevance, **address,
        )
    if value == "deferred":
        return TurnDecision(
            action="defer", reason=reason or "group_deferred_to_state",
            reason_chain=("trigger:deferred",), defer_seconds=defer_seconds,
            cancel_if=cancel_if, source=source, trigger_reasons=trigger_reasons,
            relevance=relevance, **address,
        )
    if value == "ambient":
        return TurnDecision(
            action="reply_now", reason=reason or relevance_reason or "default",
            reason_chain=relevance_chain or ("trigger:ambient",),
            source=source, trigger_reasons=trigger_reasons, relevance=relevance, **address,
        )
    return TurnDecision(
        action="silent", reason=f"unknown_trigger:{value!r}",
        reason_chain=("trigger:unknown",), source=source,
        trigger_reasons=trigger_reasons, relevance=relevance, **address,
    )


def from_route(
    segment: str,
    *,
    reason: str = "",
    pat_deferred: bool = False,
    defer_seconds: float = DEFAULT_DEFER_SECONDS,
    cancel_if: tuple[str, ...] = DEFAULT_CANCEL_IF,
    relevance: Any = None,
    source: str = "trigger",
    reply_chat_id: str = "",
    reply_platform: str = "wechat",
    reply_account: str = "self",
    reply_account_id: str = "",
    sender_id: str = "",
    is_group: bool = True,
) -> TurnDecision:
    """路由段 -> ``TurnDecision`` 的唯一构造口（旧触发词汇只留在本模块的映射表里）。"""
    trigger = _ROUTE_TO_TRIGGER.get(str(segment or ""), "")
    return from_trigger(
        trigger,
        source=source,
        relevance=relevance,
        reason=reason,
        trigger_reasons=(str(segment),) if segment else (),
        pat_deferred=pat_deferred,
        defer_seconds=defer_seconds,
        cancel_if=cancel_if,
        reply_chat_id=reply_chat_id,
        reply_platform=reply_platform,
        reply_account=reply_account,
        reply_account_id=reply_account_id,
        sender_id=sender_id,
        is_group=is_group,
    )


__all__ = [
    "DEFAULT_CANCEL_IF", "DEFAULT_DEFER_SECONDS", "TurnDecision", "with_active_reply",
    "ROUTE_SEGMENTS", "ROUTE_SEGMENT_AMBIENT", "ROUTE_SEGMENT_DEFERRED",
    "ROUTE_SEGMENT_MENTION", "ROUTE_SEGMENT_PAT",
    "from_engagement", "from_route", "from_trigger",
    "route_label", "route_segment",
]
