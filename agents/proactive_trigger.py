"""Event / owner driven proactive trigger runner.

This closes the gap between an explicit ``PROACTIVE_TRIGGER`` event (or the
owner pressing "试一次" in the Web UI) and the conversational agent:

    trigger -> whitelist / rate pre-check -> persona turn -> OutboundGateway

It deliberately does not schedule itself.  Deciding *when* the agent should
look for an opening (idle, memory due, event pattern, ...) still belongs to a
later worker; every actual send already flows through the Phase 4 gate and the
append-only proactive send log.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from safelog import warn

log = structlog.get_logger()

DEFAULT_PROACTIVE_PROMPT = (
    "[系统触发 · 主动触达机会]\n"
    "现在没有新的用户消息，是你主动联系这个会话的机会。"
    "主人已经把这个会话加入主动触达白名单，说明他愿意收到你的主动消息。\n"
    "请只结合当前会话里可核实的历史和记忆，主动找一个自然、有分寸的开场；"
    "如果记忆来源冲突、依据不明或没有可核实来源，就不要声称自己记得，"
    "改说眼前能确认的事或普通当前话题；"
    "优先说一句具体、像日常聊天的话，合适时可以发一个表情包。\n"
    "只有当你判断此刻开口会明显冒犯、打扰或不合时宜时，才输出 [静默]。"
)

MANUAL_PROACTIVE_SUFFIX = (
    "[主人手动触发]\n"
    "主人刚点了“试一次”，明确希望看到你主动说话。\n"
    "请优先用文字直接说一句自然的话；只有你觉得纯文字不合适时才发表情。"
    "涉及安全、冒犯或明显打扰时可以静默。"
)


def build_proactive_prompt(hint: str = "", *, force_speak: bool = False) -> str:
    """Build the synthetic user prompt for one proactive turn.

    ``force_speak`` is used by the owner-invoked Web "试一次" button.  It only
    biases the model towards speaking; the safety wording and the final
    OutboundGateway still let her stay silent when that is clearly the right
    call.
    """
    prompt = str(hint or "").strip() or DEFAULT_PROACTIVE_PROMPT
    if force_speak:
        prompt = f"{prompt}\n\n{MANUAL_PROACTIVE_SUFFIX}"
    return prompt


@dataclass(frozen=True)
class ProactiveTurnResult:
    ok: bool
    sent: bool = False
    silent: bool = False
    reason: str = ""
    reply: str = ""
    tools_used: tuple[str, ...] = ()
    audit_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "sent": self.sent,
            "silent": self.silent,
            "reason": self.reason,
            "reply": self.reply,
            "tools_used": list(self.tools_used),
            "audit_id": self.audit_id,
        }


def _is_silent_reply(agent: Any, reply: str) -> bool:
    checker = getattr(agent, "_is_silent_reply", None)
    if callable(checker):
        try:
            return bool(checker(reply))
        except Exception:
            warn("agents/proactive_trigger.py:83 Exception", err=repr(__import__("sys").exc_info()[1]))
    return str(reply or "").strip() in ("[静默]", "[silent]", "[不说话]")


def _sanitize(reply: str) -> str:
    try:
        from agents.base import sanitize_tool_markup

        return sanitize_tool_markup(str(reply or "")).strip()
    except Exception:
        warn("agents/proactive_trigger.py:93 Exception", err=repr(__import__("sys").exc_info()[1]))
        return str(reply or "").strip()


def apply_reply_prefix(reply: str, prefix: str) -> str:
    """按创建时约定给群提醒加 @ 前缀（幂等：已带前缀不重复加）。"""
    reply = str(reply or "")
    prefix = str(prefix or "").strip()
    if not reply.strip() or not prefix:
        return reply
    if reply.lstrip().startswith(prefix):
        return reply
    return f"{prefix} {reply.lstrip()}"


def bind_proactive_memory(context: Any) -> str:
    """Bind the same memory index used by normal turns before an owner/internal trigger."""
    try:
        from core.terminal.memory_gateway import attach_terminal_memory

        return attach_terminal_memory(context)
    except Exception as exc:  # noqa: BLE001 - memory binding must not block the trigger
        warn("proactive_memory_bind_failed", err=repr(exc)[:160])
        return ""


def prompt_with_proactive_memory(prompt: str, memory_block: str) -> str:
    body = str(prompt or "").rstrip()
    memory = str(memory_block or "").strip()
    if not memory:
        return body
    return (
        body
        + "\n\n[本轮可用记忆]\n"
        + memory
        + "\n只使用这里有依据的内容；没有依据的事不要补写。"
    )


def prompt_for_trigger(
    prompt: str,
    memory_block: str,
    *,
    trigger_kind: str,
) -> str:
    """Only proactive outreach receives the broad memory index in its user prompt."""
    if str(trigger_kind or "") == "reminder":
        return str(prompt or "").rstrip()
    return prompt_with_proactive_memory(prompt, memory_block)


async def run_proactive_turn(
    *,
    platform: str,
    bot_account_id: str = "",
    chat_id: str = "",
    chat_type: str = "private",
    hint: str = "",
    source: str = "manual",
    force_speak: bool = False,
    trigger_kind: str = "proactive",
    enforce_proactive_policy: bool = True,
    fallback_text: str = "",
    reply_prefix: str = "",
    supported_texts: tuple[str, ...] = (),
) -> ProactiveTurnResult:
    """Run one owner/internal-triggered turn that starts without an inbound msg.

    Defaults preserve the proactive semantics: the whitelist/quiet-hours
    pre-check runs, and the send is classified as proactive by
    ``OutboundGateway`` so its cooldown/rate limits apply.

    ``trigger_kind="reminder"`` + ``enforce_proactive_policy=False`` is the
    user-requested reminder delivery path: a reminder is a promise the user
    explicitly asked for, not unprompted outreach, so it must not be dropped
    by the whitelist / quiet-hours / cooldown gates.  ``fallback_text``
    guarantees that even a silent or empty model reply still delivers.
    """
    platform = str(platform or "").strip()
    bot_account_id = str(bot_account_id or "").strip()
    chat_id = str(chat_id or "").strip()
    chat_type = "group" if str(chat_type or "").lower() == "group" else "private"
    trigger_kind = str(trigger_kind or "proactive").strip() or "proactive"
    if not platform or not chat_id:
        return ProactiveTurnResult(False, reason="missing_target")

    if enforce_proactive_policy:
        try:
            from core.terminal.proactive_service import build_proactive_service

            service = build_proactive_service()
            if service is None:
                return ProactiveTurnResult(False, reason="policy_unavailable")
            decision = service.evaluate(
                platform=platform,
                bot_account_id=bot_account_id,
                chat_id=chat_id,
                chat_type=chat_type,
                kind="text",
            )
            if not decision.allowed:
                return ProactiveTurnResult(
                    False, reason=str(decision.reason or "not_whitelisted"),
                )
        except Exception as exc:  # noqa: BLE001 - fail closed, never send unchecked
            warn("agents/proactive_trigger.py:160 Exception", err=repr(exc))
            return ProactiveTurnResult(
                False, reason=f"policy_check_failed: {str(exc)[:160]}",
            )

    # ── 试验：状态驱动（写死版，见施工单 §3.3/§3.4）──────────────────────
    # 只抑制"真正的主动触达"（enforce_proactive_policy=True）；
    # 提醒路径（trigger_kind=reminder, enforce_proactive_policy=False）是
    # 用户明确请求的承诺，**不因状态被抑制**。
    # 纪律：这里只改"要不要开口"这个概率，不改台词。
    if enforce_proactive_policy:
        try:

            from core.infra.det import chance as _det_chance
            from core.state.resource import snapshot as _state_snapshot

            _st = _state_snapshot()
            _pressure = float(_st.pressure or 0.0)
            # 第二维度（时间/节律）：越晚越不主动。下限 0.15 —— 深夜也不**完全**
            # 禁，硬拦交给既有 quiet_hours（23-7，proactive_service）。两者叠加。
            _alert = max(0.15, min(1.0, float(_st.alertness or 1.0)))
            _keep = (1.0 - 0.6 * _pressure) * _alert
            if _keep < 1.0:
                # 确定性抽样（PIT-044）：同一分钟、同一个会话、同样的压力/清醒度，
                # 重放十次都是同一个结论。桶取到分钟：日志里人读得懂、bench 能精确复现，
                # 又不至于让整晚的主动都冻结成同一个值。
                _bucket = datetime.now().strftime("%Y%m%d%H%M")
                _suppressed = not _det_chance(_keep, "proactive", chat_id, _bucket)
                # 无论抑制与否都记一条：分子/分母都要有，否则抑制率算不出来
                # （施工单 §4 的三个读数都依赖这条日志）
                log.info(
                    "state_driven_call",
                    kind="proactive_decision",
                    pressure=round(_pressure, 3),
                    alertness=round(_alert, 3),
                    knob_proactive_prob=round(_keep, 3),
                    was_proactive=not _suppressed,
                    reason="state_suppressed" if _suppressed else "state_allowed",
                    key=_bucket,
                    chat_id=chat_id,
                )
                if _suppressed:
                    return ProactiveTurnResult(False, reason="state_suppressed")
        except Exception:  # noqa: BLE001 - 状态链路坏了绝不阻断主链
            warn("agents/proactive_trigger.py:198 Exception", err=repr(__import__("sys").exc_info()[1]))

    try:
        from core.bootstrap import (
            build_agent,
            get_outbound_gateway,
            get_runtime,
            get_tools,
        )
        from core.config.settings import config
        from core.speech.domain import OutboundDraft
        from core.speech.turn import snapshot_from_context
        from agents.base import AgentContext

        runtime = get_runtime()
        if bot_account_id:
            account_ctx = runtime.get(platform, bot_account_id)
        else:
            account_ctx = runtime.default
        if account_ctx is None:
            return ProactiveTurnResult(
                False, reason="no_account_context"
                if not bot_account_id else "requested_account_not_registered",
            )

        effective_account = str(
            getattr(account_ctx, "account_id", "") or bot_account_id or ""
        )
        prompt = build_proactive_prompt(hint, force_speak=force_speak)
        request_source = str(source or "manual")[:40]

        # Reuse the same per-conversation session as normal replies so the
        # model sees real history.  The synthetic trigger itself must not be
        # written into the transcript or legacy memory (record_user_turn=False
        # / proactive=True are consumed by ConversationalAgent).
        session_id = 0
        try:
            from core.routing.router import get_or_create_session_for_chat

            session_repo = getattr(account_ctx, "repo", None)
            if session_repo is not None:
                session_id = get_or_create_session_for_chat(
                    session_repo,
                    platform=platform,
                    chat_id=chat_id,
                    account_id=effective_account,
                )
        except Exception:  # noqa: BLE001 - history is an enhancement, not a gate
            warn("agents/proactive_trigger.py:246 Exception", err=repr(__import__("sys").exc_info()[1]))
            session_id = 0

        with runtime.activate(account_ctx):
            context = AgentContext(
                messages=[],
                extra={
                    "user_message": prompt,
                    "platform": platform,
                    "account_id": effective_account,
                    "chat_id": chat_id,
                    "is_group": chat_type == "group",
                    "session_id": session_id,
                    "message_time": datetime.now(),
                    "proactive": trigger_kind == "proactive",
                    "force_speak": bool(force_speak),
                    "record_user_turn": False,
                    "proactive_source": request_source,
                    "memory_claim_supported_texts": list(supported_texts),
                },
                trigger=trigger_kind,
                accounts=config.get_all_accounts(),
                owner_accounts=config.get_all_owner_accounts(),
                account_context=account_ctx,
            )
            memory_block = bind_proactive_memory(context)
            prompt = prompt_for_trigger(
                prompt,
                memory_block,
                trigger_kind=trigger_kind,
            )
            context.extra["user_message"] = prompt
            snapshot = snapshot_from_context(
                context,
                platform=platform,
                account_id=effective_account,
                chat_id=chat_id,
                chat_type=chat_type,
                sender_id="" if chat_type == "group" else chat_id,
                inbound_text=prompt,
                msg_type="text",
                trigger_reason=trigger_kind,
            )
            if snapshot is None:
                return ProactiveTurnResult(False, reason="snapshot_failed")

            agent = build_agent()
            tools = get_tools()
            if tools is not None:
                agent.set_tool_registry(tools)
            result = await agent.execute(context)
            output = result.output if isinstance(result.output, dict) else {}
            reply = _sanitize(str(output.get("reply") or ""))
            tools_used = tuple(str(name) for name in (result.tools_used or ()))
            already_sent = bool(
                context.extra.get("_last_sent_ok")
                or context.extra.get("_last_send_log_id")
            )
            if already_sent:
                return ProactiveTurnResult(
                    True, sent=True, reason="agent_sent",
                    reply=reply, tools_used=tools_used,
                    audit_id=context.extra.get("_last_audit_id"),
                )

            used_fallback = False
            if not reply and any(
                name in ("send_message", "send_sticker") for name in tools_used
            ):
                return ProactiveTurnResult(
                    False, reason="agent_send_failed", tools_used=tools_used,
                )
            if not reply or _is_silent_reply(agent, reply):
                # 模型没说出来或选择静默：带 fallback_text 的触发（提醒）
                # 必须把话送到，不能因此丢；主动触达保持原有静默语义。
                if not fallback_text:
                    return ProactiveTurnResult(
                        True, silent=True,
                        reason="no_reply" if not reply else "silent",
                        reply=reply, tools_used=tools_used,
                    )
                reply = fallback_text
                used_fallback = True

            reply = apply_reply_prefix(reply, reply_prefix)

            gateway = get_outbound_gateway()
            draft = OutboundDraft(
                request_id=snapshot.turn.request_id or uuid.uuid4().hex,
                kind="text",
                target=snapshot.turn.target,
                account_choice="self",
                account_id=effective_account,
                content=reply,
                who=chat_id,
                extra={
                    "proactive": trigger_kind == "proactive",
                    "trigger": trigger_kind,
                },
            )
            sent = await gateway.send_text(draft, snapshot, context)
            if not sent.ok:
                reason = "fallback_send_failed" if used_fallback else "send_failed"
            else:
                reason = "fallback" if used_fallback else ""
            return ProactiveTurnResult(
                bool(sent.ok),
                sent=bool(sent.ok),
                reason=reason,
                reply=reply,
                tools_used=tools_used,
                audit_id=sent.audit_id,
            )
    except Exception as exc:  # noqa: BLE001 - trigger must never kill the loop
        warn("agents/proactive_trigger.py:351 Exception", err=repr(exc))
        return ProactiveTurnResult(
            False, reason=f"trigger_failed: {str(exc)[:160]}",
        )
