"""给 loop/ports.py 造一个 TurnContext。

2026-10-26：web 端整体下线（`web/` 已删），出口闸不能再从 `web.routes_speech`
里借 `_make_turn`。语义照抄它，并修掉一个历史 bug：loop 传的是 `content=`，
而原来那个函数只读 `inbound_text=`，所以 speech facade 一直收到空的 inbound_text。
"""
from __future__ import annotations

import time

from .domain import AudienceKind, ConversationTarget, TurnContext


def make_turn(payload: dict) -> TurnContext:
    platform = str(payload.get("platform") or "wechat")
    account_id = str(payload.get("account_id") or "")
    chat_id = str(payload.get("chat_id") or "")
    is_group = str(payload.get("chat_type") or "private") == "group"
    mentioned = bool(payload.get("is_mentioned"))
    owner = bool(payload.get("is_owner"))
    target = ConversationTarget(
        platform=platform,
        account_id=account_id,
        chat_id=chat_id,
        chat_type="group" if is_group else "private",
        sender_id=str(payload.get("sender_id") or ""),
        sender_name=str(payload.get("sender_name") or ""),
    )
    if is_group:
        audience = AudienceKind.GROUP_MENTIONED if mentioned else AudienceKind.GROUP_AMBIENT
    else:
        audience = AudienceKind.OWNER_PRIVATE if owner else AudienceKind.NON_OWNER_PRIVATE
    text = str(payload.get("inbound_text") or payload.get("content") or "")
    return TurnContext(
        request_id=str(payload.get("request_id") or "loop-say-%d" % int(time.time() * 1000)),
        target=target,
        audience=audience,
        inbound_text=text,
        msg_type=str(payload.get("msg_type") or "text"),
        trigger_reason=str(payload.get("trigger_reason") or ""),
        is_mentioned=mentioned,
        is_owner=owner,
        group_recent_context=str(payload.get("group_recent_context") or ""),
    )
