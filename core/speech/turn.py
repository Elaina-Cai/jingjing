from __future__ import annotations

import uuid
from typing import Any

from core.speech.domain import AudienceKind, ConversationTarget, TurnContext
from safelog import warn


def _extra(context: Any) -> dict:
    if context is None:
        return {}
    return getattr(context, "extra", None) or {}


def relationship_facts(repo, target: ConversationTarget):
    """Return (relationship_tier, intimacy) for a target using the DB."""
    tier = ""
    intimacy = None
    if target.is_group:
        return "群聊", None
    try:
        rel = repo.relationships.get(persona_id=1, target_wxid=target.chat_id or target.sender_id)
        if rel is not None:
            intimacy = getattr(rel, "intimacy", None)
            relation_type = (getattr(rel, "relation_type", "") or "").strip()
            if relation_type:
                tier = relation_type
            elif intimacy is not None:
                if intimacy >= 0.8:
                    tier = "亲密"
                elif intimacy >= 0.5:
                    tier = "熟悉"
                else:
                    tier = "普通"
    except Exception:
        warn("core/speech/turn.py:35 Exception", err=repr(__import__("sys").exc_info()[1]))
    return tier, intimacy


def snapshot_from_context(
    context: Any,
    *,
    platform: str | None = None,
    account_id: str | None = None,
    chat_id: str | None = None,
    chat_type: str | None = None,
    sender_id: str | None = None,
    sender_name: str | None = None,
    inbound_text: str | None = None,
    msg_type: str = "text",
    trigger_reason: str | None = None,
    is_mentioned: bool = False,
    is_owner: bool | None = None,
    group_recent_context: str = "",
):
    """Resolve and attach a SpeechSnapshot to any AgentContext-like object.

    Used by the tool layer and Web chat so every LLM/tool entry point gets a
    policy snapshot even when it was not created by MessageRouter.
    """
    from core.terminal.store_port import get_repo
    from core.bootstrap import get_speech_facade
    from core.config.settings import config

    extra = _extra(context)
    platform = platform or extra.get("platform") or "wechat"
    account_id = account_id or extra.get("account_id") or ""
    if not account_id:
        try:
            accounts = getattr(context, "accounts", None) or {}
            account_id = accounts.get(platform, "") or ""
        except Exception:
            warn("core/speech/turn.py:72 Exception", err=repr(__import__("sys").exc_info()[1]))
            account_id = ""
    if not account_id and platform == "wechat":
        account_id = config.wechat.yinyue_wxid or ""
    chat_id = chat_id or extra.get("chat_id") or extra.get("source_chat") or ""
    chat_type = chat_type or ("group" if extra.get("is_group") else "private")
    sender_id = sender_id if sender_id is not None else str(extra.get("sender_id") or "")
    sender_name = sender_name if sender_name is not None else str(extra.get("sender_name") or "")
    inbound_text = inbound_text if inbound_text is not None else str(
        extra.get("user_message") or ""
    )
    if trigger_reason is None:
        from core.routing.turn_decision import structured_reason

        trigger_reason = structured_reason(getattr(context, "turn_decision", None))
    if is_owner is None:
        # Deterministic Authority is always active; display names and legacy
        # owner id sets never decide this.
        try:
            from core.terminal.authority import (
                active_context_is_owner,
                active_is_owner,
            )

            if context is not None:
                is_owner = active_context_is_owner(
                    context,
                    platform=platform,
                    account_id=account_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    sender_id=sender_id,
                    sender_name=sender_name,
                )
            else:
                is_owner = active_is_owner(
                    platform=platform,
                    account_id=account_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    sender_id=sender_id,
                    sender_name=sender_name,
                )
        except Exception:
            warn("core/speech/turn.py:115 Exception", err=repr(__import__("sys").exc_info()[1]))
            is_owner = False

    try:
        from core.terminal.store_port import get_repo as _get_repo
        rel_repo = _get_repo()
        relationship_tier, intimacy = relationship_facts(
            rel_repo,
            ConversationTarget(
                platform=platform, account_id=account_id, chat_id=chat_id,
                chat_type="group" if chat_type == "group" else "private",
                sender_id=sender_id, sender_name=sender_name,
            ),
        )
    except Exception:
        warn("core/speech/turn.py:129 Exception", err=repr(__import__("sys").exc_info()[1]))
        relationship_tier, intimacy = "", None

    if not relationship_tier and is_owner:
        relationship_tier = "亲密"

    turn = TurnContext(
        request_id=uuid.uuid4().hex,
        target=ConversationTarget(
            platform=platform,
            account_id=account_id,
            chat_id=chat_id,
            chat_type="group" if chat_type == "group" else "private",
            sender_id=sender_id,
            sender_name=sender_name,
        ),
        audience=AudienceKind.WEB_CHAT,
        inbound_text=inbound_text,
        msg_type=msg_type,
        trigger_reason=trigger_reason,
        is_mentioned=is_mentioned,
        is_owner=bool(is_owner),
        group_recent_context=group_recent_context,
        relationship_tier=relationship_tier,
        intimacy=intimacy,
    )
    snapshot = get_speech_facade().resolve(turn, get_repo())
    if context is not None:
        try:
            context.turn_context = snapshot.turn
            context.speech_snapshot = snapshot
            if isinstance(getattr(context, "extra", None), dict):
                context.extra["speech_revision"] = snapshot.policy.revision
                context.extra["speech_persona_id"] = snapshot.persona.persona_id
        except Exception:
            warn("core/speech/turn.py:163 Exception", err=repr(__import__("sys").exc_info()[1]))
    return snapshot
