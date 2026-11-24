"""Terminal event recording helpers.

The production path writes to the formal SQLite event stream only.  The
explicit ``store=`` argument is kept so tests can inject an in-memory fake
store without going through bootstrap.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

import structlog

from core import temporal
from core.terminal.command_intent import classify_intent
from core.terminal.event_model import (
    Event,
    EventType,
    HUMAN_SPEECH_TYPES,
    Sensitivity,
    localnow,
)
from core.terminal.identity_service import IdentityService
from core.terminal.principal import VisibilityScope
from core.terminal.revoke import RevokeNotice, choose_revoke_target, parse_revoke_notice
from safelog import warn

log = structlog.get_logger()


def link_event_best_effort(store: Any, event_id: str, **fields: str) -> bool:
    """回填事件行的链接列；不支持窄更新的 store（测试替身/旧实现）直接跳过。

    见 ``event_store_db.TerminalEventStore.link_event``：这是 append-only 的
    受控例外（只写 command_id / authorization_id / turn_id / parent_event_id）。
    best-effort：任何失败只记一行日志，不影响消息入库与回复。
    """
    if not str(event_id or ""):
        return False
    link = getattr(store, "link_event", None)
    if link is None:
        return False
    payload = {key: str(value) for key, value in fields.items() if str(value or "")}
    if not payload:
        return False
    try:
        return bool(link(str(event_id), **payload))
    except Exception as exc:
        log.warning(
            "terminal_event_link_failed",
            event_id=str(event_id)[:64],
            columns=sorted(payload),
            error=str(exc)[:200],
        )
        return False


def _stamp_message_identity(raw: dict, event: Any, fallback_turn_id: str = "") -> None:
    """把事件行上的 ``turn_id`` / ``event_id`` 回写到入站消息行（采集侧穿透）。

    这一批消息载荷随后由 EventBus 发布给回复链（scheduler → agent extra →
    thought/action/reply 事件）：回写让下游不必再查一次库就拿到同一个 turn_id。
    事件行是权威——库里已有 turn_id 时以库里的值为准。
    """
    try:
        event_id = str(getattr(event, "event_id", "") or "")
        turn_id = (
            str(getattr(event, "turn_id", "") or "") or str(fallback_turn_id or "")
        )
        if event_id:
            raw["event_id"] = event_id
        if turn_id:
            raw["turn_id"] = turn_id
    except Exception:
        warn("core/terminal/shadow.py:69 Exception", err=repr(__import__("sys").exc_info()[1]))


def get_default_store() -> Any:
    """Return the formal SQLite event stream store."""
    from core.terminal.event_runtime import get_terminal_event_store

    return get_terminal_event_store(force=True)


def parse_time(value: object, default: datetime | None = None) -> datetime:
    """datetime / ISO 文本 → 应用时钟（naive 本地墙钟）。

    解析与换算都在 owner ``temporal.parse_naive_local`` 里（旧实现是本模块自带的
    第二份复制件）；本函数只保留「解析不出来给 now()」这一条本地兜底。
    """
    return temporal.parse_naive_local(value, default) or default or datetime.now()


#: 自我回声合并窗口：同一句回复的我们自己写的 reply 与微信同步回来的 is_self
#: message 在这个时间差内视为同一条消息（见 record_message_events）。
SELF_ECHO_WINDOW_SECONDS = 900


def _normalized_body(text: object) -> str:
    return " ".join(str(text or "").split())


#: 行首 @ 提及：@ 后跟非空白的提及名，前后可有零宽字符。
_LEADING_MENTION_RE = re.compile(
    r"^[\u200b\u200c\u200d\u2060\ufeff]*@[^\s@\u2005]+[\u200b\u200c\u200d\u2060\ufeff\u2005]*"
)


def _strip_leading_mentions(text: object) -> str:
    """去掉行首的平台 @ 提及，供自我回声比对（不动句中的 @）。

    发送侧写的 reply 正文是模型出稿（没有平台 @ 前缀）；微信同步回来的
    is_self 行则带着渲染出来的 ``@对方\u2005`` 前缀（\u2005 是窄不换行空格）。
    只剥行首：先丢前导空白与零宽字符，再反复匹配 ``@提及`` + 紧随的空白类
    分隔符；提及名本身不含 @ 与空白，所以不会把正文一起吃掉。
    """
    body = str(text or "").lstrip("\u200b\u200c\u200d\u2060\ufeff \t\u2005")
    for _ in range(4):
        m = _LEADING_MENTION_RE.match(body)
        if m is None:
            break
        body = body[m.end():]
    return body


def _resolve_revoke_target(
    store: Any,
    *,
    platform: str,
    account_id: str,
    chat_id: str,
    occurred_at: datetime,
    notice: RevokeNotice,
) -> tuple[Event | None, str, str]:
    """Resolve a revoke to one human message, or leave it explicitly unknown."""
    reference = str(notice.reference_external_msg_id or "").strip()
    lookup = getattr(store, "get_by_external_msg_id", None)
    if reference:
        if lookup is None:
            return None, "unknown", reference
        try:
            target = lookup(
                platform=platform,
                bot_account_id=account_id,
                chat_id=chat_id,
                external_msg_id=reference,
            )
        except Exception as exc:
            warn("terminal_revoke_exact_lookup_failed", err=repr(exc)[:120])
            return None, "unknown", reference
        return (
            target,
            "exact" if target is not None else "unknown",
            reference,
        )

    query = getattr(store, "query", None)
    if query is None or (not notice.is_self and not notice.actor_label):
        return None, "unknown", ""
    try:
        recent = query(
            event_types=HUMAN_SPEECH_TYPES,
            chat_id=chat_id,
            platform=platform,
            bot_account_id=account_id,
            limit=100,
        )
    except Exception as exc:
        warn("terminal_revoke_hint_query_failed", err=repr(exc)[:120])
        return None, "unknown", ""

    target = choose_revoke_target(notice, occurred_at, recent or ())
    if target is None:
        return None, "unknown", ""
    return target, "inferred", str(target.external_msg_id or "")


def _same_echo_body(incoming: str, stored: str) -> bool:
    """发送侧正文与同步回声是否为同一句（含截断）。

    严格相等之外接受两种现实差异：① 同步行带 ``@对方`` 前缀；② 发送侧
    写入时被 ``SELF_MESSAGE_TEXT_MAX`` 截断（尾部对不上）。所以比较前先各自
    剥前缀，再接受前缀包含（短的是长者的前缀），长度须 >= 8 与 caller 一致。
    """
    left = _strip_leading_mentions(incoming).strip()
    right = _strip_leading_mentions(stored).strip()
    if not left or not right:
        return False
    if left == right:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= 8 and long.startswith(short)


def _find_recent_reply(
    store: object,
    *,
    platform: str,
    bot_account_id: str,
    chat_id: str,
    content: str,
    occurred_at: datetime,
    window_seconds: float = SELF_ECHO_WINDOW_SECONDS,
):
    """同会话、同内容、时间近的 REPLY 事件；找不到返回 None（绝不抛）。"""
    query = getattr(store, "query", None)
    if query is None:
        return None
    body = _strip_leading_mentions(content).strip()
    if len(body) < 8:
        return None
    try:
        recent = query(
            event_types=(EventType.REPLY,),
            chat_id=str(chat_id or ""),
            platform=str(platform or ""),
            bot_account_id=str(bot_account_id or ""),
            limit=30,
        )
    except Exception:
        warn("core/terminal/shadow.py:163 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    for event in reversed(list(recent or ())):
        if not _same_echo_body(body, str(getattr(event, "content", "") or "")):
            continue
        stamp = getattr(event, "occurred_at", None)
        if not isinstance(stamp, datetime):
            continue
        try:
            delta = abs((occurred_at - stamp).total_seconds())
        except Exception:
            warn("core/terminal/shadow.py:173 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
        if delta <= float(window_seconds):
            return event
    return None


def record_message_events(
    messages: Iterable[dict],
    *,
    platform: str,
    account_id: str,
    identity_service: IdentityService | None = None,
    store: Any | None = None,
    group_id: str = "",
    command_recorder: Any | None = None,
    return_events: bool = False,
) -> int | list[Event]:
    """Record raw message rows as shadow events.  Best-effort and never raises.

    Idempotent by ``(platform, bot_account_id, chat_id, external_msg_id)``:
    a message already in the stream is returned instead of appended again,
    and its command recording is not replayed.

    A1-a / A1-c（2026-09-29）：

    * **turn_id 采集侧生成**：一条入站消息一个 ``turn_id``（``uuid4().hex``），
      在事件行**写入时**生成并回写到 ``raw["turn_id"]``；同一批载荷随后由
      EventBus 发布给回复链，同一回合的 thought / action / reply 直接沿用，
      不再二次推导。行上已有 turn_id（重放/上游已生成）时沿用原值；库里已有
      这行且没有 turn_id（Migration 50 之前的存量）时补写一次链接列。
    * 事件行写好后把 ``command_id`` / ``authorization_id`` 回填到这条消息行上
      （命令投影是 append 之后才建的），使 ``command_records`` 与
      ``terminal_events`` 能按 command_id join。
    """
    shadow_store = store or get_default_store()
    count = 0
    recorded: list[Event] = []
    existing_lookup = getattr(shadow_store, "get_by_external_msg_id", None)
    link_revision = (
        identity_service.link_graph.revision if identity_service is not None else 0
    )
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        try:
            turn_id = str(raw.get("turn_id") or "") or uuid.uuid4().hex
            chat_id = str(raw.get("chat_id") or "")
            chat_type = str(raw.get("chat_type") or "private")
            sender_id = str(raw.get("sender_id") or "")
            sender_name = str(raw.get("sender_name") or "")
            external_msg_id = str(raw.get("msg_id") or raw.get("id") or "")
            content = raw.get("content") or ""
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            occurred_at = parse_time(raw.get("created_at"))
            is_group = chat_type == "group"
            group = group_id or (chat_id if is_group else "")
            intent = classify_intent(str(content or ""))
            visibility = (
                frozenset({VisibilityScope.GROUP_MEMBERS})
                if is_group
                else frozenset({VisibilityScope.ISSUER_ONLY})
            )
            principal = None
            metadata: dict = {
                "msg_id": external_msg_id,
                "msg_type_name": str(raw.get("msg_type_name") or ""),
                "source": str(raw.get("source") or platform),
                "is_self": int(raw.get("is_self") or 0),
                "sender_name": sender_name,
                "group_id": group,
                "link_component_id": "",
                "link_revision": link_revision,
                "intent": intent.event_type.value,
                "intent_reasons": list(intent.reasons),
            }
            if external_msg_id and existing_lookup is not None:
                try:
                    existing_event = existing_lookup(
                        platform=platform,
                        bot_account_id=account_id,
                        chat_id=chat_id,
                        external_msg_id=external_msg_id,
                    )
                except Exception:
                    warn("core/terminal/shadow.py:257 Exception", err=repr(__import__("sys").exc_info()[1]))
                    existing_event = None
                if existing_event is not None:
                    # 存量行（Migration 50 之前）没有 turn_id：补写链接列，
                    # 让「消息行 / thought / reply 同一个 turn_id」成立。
                    if not str(getattr(existing_event, "turn_id", "") or ""):
                        link_event_best_effort(
                            shadow_store,
                            getattr(existing_event, "event_id", ""),
                            turn_id=turn_id,
                        )
                    _stamp_message_identity(raw, existing_event, turn_id)
                    recorded.append(existing_event)
                    count += 1
                    continue
            try:
                is_self_row = int(raw.get("is_self") or 0) == 1
            except (TypeError, ValueError):
                warn("core/terminal/shadow.py:274 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
                is_self_row = False
            if is_self_row and str(content or "").strip():
                matched = _find_recent_reply(
                    shadow_store,
                    platform=platform,
                    bot_account_id=account_id,
                    chat_id=chat_id,
                    content=str(content),
                    occurred_at=occurred_at or localnow(),
                )
                if matched is not None:
                    # 同一条消息我们已经写过 reply（发送时），同步回声不再追一条
                    # message：只把外部 msg_id 回填给那条 reply（窄更新受控例外）。
                    link_external = getattr(shadow_store, "link_external_msg_id", None)
                    if external_msg_id and link_external is not None:
                        try:
                            link_external(
                                str(getattr(matched, "event_id", "") or ""),
                                external_msg_id=external_msg_id,
                            )
                        except Exception:
                            warn("core/terminal/shadow.py:295 Exception", err=repr(__import__("sys").exc_info()[1]))
                    log.info(
                        "terminal_self_echo_merged",
                        platform=platform,
                        chat_id=chat_id,
                        msg_id=external_msg_id,
                        reply_event_id=str(getattr(matched, "event_id", "") or ""),
                    )
                    recorded.append(matched)
                    count += 1
                    continue
            revoke = parse_revoke_notice(content)
            if revoke is not None:
                target, resolution, ref_external_msg_id = _resolve_revoke_target(
                    shadow_store,
                    platform=platform,
                    account_id=account_id,
                    chat_id=chat_id,
                    occurred_at=occurred_at or localnow(),
                    notice=revoke,
                )
                metadata["revoke"] = {
                    "ref_external_msg_id": ref_external_msg_id,
                    "resolution": resolution,
                    "actor_label": revoke.actor_label,
                    "is_self": revoke.is_self,
                    "notice": revoke.notice,
                    "revoke_time": revoke.revoke_time,
                }
                recorded_event = shadow_store.append(
                    Event.new(
                        event_type=EventType.REVOKE,
                        occurred_at=occurred_at,
                        platform=platform,
                        bot_account_id=account_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        content=str(content),
                        visibility_scope=visibility,
                        metadata=metadata,
                        external_msg_id=external_msg_id,
                        parent_event_id=str(getattr(target, "event_id", "") or ""),
                        turn_id=turn_id,
                        link_revision=link_revision,
                    )
                )
                recorded.append(recorded_event)
                _stamp_message_identity(raw, recorded_event, turn_id)
                log.info(
                    "terminal_revoke_recorded",
                    platform=platform,
                    chat_id=chat_id,
                    resolution=resolution,
                    target_event_id=str(getattr(target, "event_id", "") or ""),
                    msg_id=external_msg_id,
                )
                count += 1
                continue
            if identity_service is not None:
                principal = identity_service.resolve_principal(
                    platform=platform,
                    bot_account_id=account_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    occurred_at=occurred_at,
                    group_id=group,
                )
                metadata["principal"] = principal.as_dict()
                metadata["link_component_id"] = principal.link_component_id
            recorded_event = shadow_store.append(
                Event.new(
                    event_type=intent.event_type,
                    occurred_at=occurred_at,
                    platform=platform,
                    bot_account_id=account_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    actor_person_id=sender_id,
                    actor_account_id=sender_id,
                    content=str(content),
                    visibility_scope=visibility,
                    metadata=metadata,
                    external_msg_id=external_msg_id,
                    turn_id=turn_id,
                    link_revision=link_revision,
                )
            )
            recorded.append(recorded_event)
            _stamp_message_identity(raw, recorded_event, turn_id)

            if command_recorder is not None:
                try:
                    command = command_recorder.record_from_message(
                        raw,
                        platform=platform,
                        account_id=account_id,
                        occurred_at=occurred_at,
                        principal=principal,
                        intent=intent,
                    )
                except Exception as exc:
                    log.warning(
                        "terminal_command_record_failed",
                        platform=platform,
                        account_id=account_id,
                        chat_id=chat_id,
                        msg_id=external_msg_id,
                        error=str(exc)[:200],
                    )
                else:
                    # A1-a：命令投影落库后把 command_id / authorization_id 回填到
                    # 这条消息行（append 时还不知道命令 id）。
                    link_event_best_effort(
                        shadow_store,
                        getattr(recorded_event, "event_id", ""),
                        command_id=str(getattr(command, "command_id", "") or ""),
                        authorization_id=str(
                            getattr(command, "authorization_id", "") or ""
                        ),
                    )
            count += 1
        except Exception as exc:
            log.warning(
                "terminal_event_record_failed",
                platform=platform,
                account_id=account_id,
                chat_id=str(raw.get("chat_id") or ""),
                msg_id=str(raw.get("msg_id") or raw.get("id") or ""),
                error=str(exc)[:200],
            )
            continue
    return recorded if return_events else count


def record_owner_thought(
    *,
    content: str,
    platform: str = "",
    bot_account_id: str = "",
    chat_id: str = "",
    parent_event_id: str = "",
    turn_id: str = "",
    store: Any | None = None,
) -> Event:
    """Record an owner-visible chain-of-thought event in the shadow stream."""
    shadow_store = store or get_default_store()
    return shadow_store.append(
        Event.new(
            event_type=EventType.THOUGHT,
            platform=platform,
            bot_account_id=bot_account_id,
            chat_id=chat_id,
            content=content,
            parent_event_id=parent_event_id,
            turn_id=turn_id,
            sensitivity=Sensitivity.OWNER_CHAIN_OF_THOUGHT,
            visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
        )
    )


def _sent_by_bot(
    bot_account_id: str, person_id: str
) -> tuple[str, tuple[str, ...]]:
    """Resolve actor/recipient for an event the bot itself emitted.

    The bot is the *actor* of everything it says or does, so ``actor_person_id``
    carries the bot's account id (the only stable identifier the stream has for
    it) and ``actor_account_id`` mirrors it.  ``person_id`` here is the
    counterpart the bot is talking to, so it belongs in
    ``recipient_person_ids`` -- **never** in the actor slot.  Putting it in the
    actor slot mislabels the speaker as the person being answered (the
    "张冠李戴" bug that ``gate_hard_rules.check_actor_semantics`` now guards
    against).

    Private-chat replies must stay visible to the person they were sent to, so
    the ACL (``visibility.event_visible`` / ``memory_gateway._memory_allowed``)
    grants ``ISSUER_ONLY`` / ``SELF`` access to a principal found in
    ``recipient_person_ids`` too.
    """
    actor = str(bot_account_id or "")
    counterpart = str(person_id or "")
    return (actor, (counterpart,) if counterpart else ())


def record_thought_event(
    *,
    content: str,
    platform: str = "",
    bot_account_id: str = "",
    chat_id: str = "",
    chat_type: str = "",
    person_id: str = "",
    parent_event_id: str = "",
    turn_id: str = "",
    principal: Any | None = None,
    store: Any | None = None,
) -> Event:
    """Owner-visible thought event for a turn.

    ``parent_event_id`` 是这一回合**触发消息的事件 id**，``turn_id`` 是它的
    回合并集键（A1-c）；两者都由采集侧随载荷带下来，缺失时留空（不猜）。
    """
    shadow_store = store or get_default_store()
    actor_id, recipient_ids = _sent_by_bot(bot_account_id, person_id)
    return shadow_store.append(
        Event.new(
            event_type=EventType.THOUGHT,
            platform=platform,
            bot_account_id=bot_account_id,
            chat_id=chat_id,
            chat_type=chat_type,
            actor_person_id=actor_id,
            actor_account_id=bot_account_id,
            recipient_person_ids=recipient_ids,
            content=content,
            parent_event_id=parent_event_id,
            turn_id=turn_id,
            sensitivity=Sensitivity.OWNER_CHAIN_OF_THOUGHT,
            visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
            metadata={
                "group_id": chat_id if str(chat_type).lower() == "group" else "",
                "principal": principal.as_dict() if principal is not None else {},
            },
        )
    )


def record_reply_event(
    *,
    content: str,
    platform: str,
    bot_account_id: str,
    chat_id: str,
    chat_type: str = "private",
    person_id: str = "",
    parent_event_id: str = "",
    parent_msg_id: str = "",
    turn_id: str = "",
    principal: Any | None = None,
    store: Any | None = None,
) -> Event:
    """Record a message sent by the terminal.

    A1-c 语义：``parent_event_id`` 是**被回应消息的事件 id**——解析不出来就留空，
    并把源平台 msg_id 放进 ``metadata.parent_msg_id`` 供读侧兼容（旧行里
    ``parent_event_id`` 存的是平台 msg_id，chat_window 读侧按「像不像事件 id」
    兼容两种写法）。**绝不把平台 msg_id 写进 ``parent_event_id``**：那样按事件
    id join 会全 orphan（2026-09-29 实测 389/389）。

    旧参数名 ``reply_to_event_id``（A1-c 之前的写法）已删除：全仓无调用方，
    留着只会再次诱导调用方把平台 msg_id 传进 ``parent_event_id``。
    """
    shadow_store = store or get_default_store()
    resolved_parent = str(parent_event_id or "")
    is_group = str(chat_type or "").lower() == "group"
    visibility = (
        frozenset({VisibilityScope.GROUP_MEMBERS})
        if is_group
        else frozenset({VisibilityScope.ISSUER_ONLY})
    )
    actor_id, recipient_ids = _sent_by_bot(bot_account_id, person_id)
    metadata: dict = {
        "group_id": chat_id if is_group else "",
        "principal": principal.as_dict() if principal is not None else {},
    }
    if parent_msg_id:
        metadata["parent_msg_id"] = str(parent_msg_id)
    return shadow_store.append(
        Event.new(
            event_type=EventType.REPLY,
            platform=platform,
            bot_account_id=bot_account_id,
            chat_id=chat_id,
            chat_type=chat_type,
            actor_person_id=actor_id,
            actor_account_id=bot_account_id,
            recipient_person_ids=recipient_ids,
            content=content,
            visibility_scope=visibility,
            parent_event_id=resolved_parent,
            turn_id=turn_id,
            metadata=metadata,
        )
    )


def record_action_event(
    *,
    action: str,
    action_input: dict | None = None,
    output: str = "",
    error: str = "",
    ok: bool = True,
    duration_ms: int = 0,
    platform: str = "",
    bot_account_id: str = "",
    chat_id: str = "",
    chat_type: str = "private",
    person_id: str = "",
    parent_event_id: str = "",
    turn_id: str = "",
    principal: Any | None = None,
    store: Any | None = None,
) -> Event:
    """Record a tool call / executed action.  Raw action detail is owner-only.

    ``parent_event_id`` / ``turn_id`` 与 ``record_thought_event`` 同义（A1-c）：
    动作属于触发它的那一回合，缺一不可串链。
    """
    shadow_store = store or get_default_store()
    actor_id, recipient_ids = _sent_by_bot(bot_account_id, person_id)
    return shadow_store.append(
        Event.new(
            event_type=EventType.ACTION,
            platform=platform,
            bot_account_id=bot_account_id,
            chat_id=chat_id,
            chat_type=chat_type,
            actor_person_id=actor_id,
            actor_account_id=bot_account_id,
            recipient_person_ids=recipient_ids,
            content=action,
            action_payload={
                "action": action,
                "input": action_input or {},
                "output": str(output)[:500],
                "error": str(error or "")[:500],
                "ok": bool(ok),
                "duration_ms": int(duration_ms or 0),
            },
            visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
            parent_event_id=parent_event_id,
            turn_id=turn_id,
            metadata={
                "group_id": chat_id if str(chat_type).lower() == "group" else "",
                "principal": principal.as_dict() if principal is not None else {},
            },
        )
    )
