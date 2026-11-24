"""Scheduler adapter for person memory extraction.

Thin adapter only:

    NEW_MESSAGES_SYNCED payload
      -> canonical MessageEvent
      -> identity resolver
      -> PersonMemoryService (core, platform-neutral)

All prompts, visibility and storage contracts live in ``core.people`` and
``db.repo_person_memory``.  This module may import platform/config code because
it is the scheduler boundary.
"""

from __future__ import annotations

import os
import structlog
from datetime import datetime
from typing import Any

from core import features
from core.terminal.store_port import get_meta_repo
from core.bootstrap import get_llm
from core.contracts.events import ChatType, MessageEvent, MessageKind
from core.identity.platforms import chat_type_for
from core.people.aggregator import RuleBasedProfileAggregator
from core.people.extractor import (
    CompositeAsyncObservationExtractor,
    RuleBasedObservationExtractor,
    SyncObservationExtractorAdapter,
)
from core.people.llm_extractor import LlmObservationExtractor
from core.people.service import PersonMemoryService
from db.repo_person_memory import PersonMemoryRepository
from llm.person_observation import LlmClientTextCompletion
from safelog import warn

log = structlog.get_logger()

_KIND_MAP: dict[str, MessageKind] = {
    "text": MessageKind.TEXT,
    "image": MessageKind.IMAGE,
    "sticker": MessageKind.STICKER,
    "voice": MessageKind.VOICE,
    "video": MessageKind.VIDEO,
    "file": MessageKind.FILE,
    "link": MessageKind.LINK,
    "location": MessageKind.LOCATION,
    "contact_card": MessageKind.CONTACT_CARD,
    "quote_reply": MessageKind.QUOTE_REPLY,
    "forward": MessageKind.FORWARD,
    "system": MessageKind.SYSTEM,
    "pat": MessageKind.PAT,
    "mini_program": MessageKind.MINI_PROGRAM,
    "transfer": MessageKind.TRANSFER,
}


def _kind(value: str) -> MessageKind:
    return _KIND_MAP.get(str(value or "").strip().lower(), MessageKind.UNKNOWN)


def _chat_type(platform: str, value: str, chat_id: str) -> ChatType:
    return chat_type_for(platform, value, chat_id)


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        warn("scheduler/person_memory.py:77 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        warn("scheduler/person_memory.py:84 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return default


def _build_service(meta_repo) -> tuple[PersonMemoryService, CompositeAsyncObservationExtractor]:
    repo = PersonMemoryRepository(meta_repo.engine)
    extractors: list[object] = [
        SyncObservationExtractorAdapter(RuleBasedObservationExtractor()),
    ]
    if features.enabled(features.PERSON_LLM):
        llm = get_llm()
        if llm is not None:
            extractors.append(LlmObservationExtractor(LlmClientTextCompletion(llm)))
    composite = CompositeAsyncObservationExtractor(*extractors)
    service = PersonMemoryService(
        repo,
        repo,
        None,
        RuleBasedProfileAggregator(),
        async_extractor=composite,
    )
    return service, composite


async def _process_payload_event(event) -> int:
    """Extract observations for one NEW_MESSAGES_SYNCED event.

    Returns the number of observations written.  Never raises: the event bus
    must not be blocked by person-memory failures.
    """

    if not features.enabled(features.PERSON_MEMORY):
        return 0
    payload = getattr(event, "payload", {}) or {}
    if not isinstance(payload, dict):
        return 0
    messages = payload.get("messages", []) or []
    if not messages:
        return 0

    platform = str(getattr(event, "platform", "") or "")
    account_id = str(getattr(event, "account_id", "") or "")
    if not platform or not account_id:
        return 0

    log.info(
        "person_memory_scan",
        platform=platform,
        account_id=account_id,
        messages=len(messages),
    )
    max_messages = max(1, _env_int("YINYUE_PERSON_MEMORY_MAX_MESSAGES_PER_BATCH", 500))
    if features.enabled(features.PERSON_LLM):
        # The LLM path is intentionally more expensive; keep the first batch
        # small unless the operator raises the limit explicitly.
        max_messages = max(1, _env_int("YINYUE_PERSON_LLM_MAX_MESSAGES_PER_BATCH", 20))

    try:
        meta_repo = get_meta_repo()
        service, _composite = _build_service(meta_repo)
    except Exception as exc:  # pragma: no cover - bootstrap failure
        log.warning("person_memory_service_init_failed", error=str(exc)[:160])
        return 0

    written = 0
    processed = 0
    all_extracted = []
    for msg in messages:
        if processed >= max_messages:
            break
        if not isinstance(msg, dict):
            continue
        if int(msg.get("is_self") or 0):
            continue
        sender_id = str(msg.get("sender_id") or "").strip()
        content = str(msg.get("content") or "").strip()
        if not sender_id or not content:
            continue

        chat_id = str(msg.get("chat_id") or "")
        chat_type = _chat_type(platform, msg.get("chat_type"), chat_id)
        if chat_type == ChatType.UNKNOWN:
            log.debug("person_memory_unknown_chat_type",
                      platform=platform, chat_id=chat_id)
            continue
        try:
            person = meta_repo.people.resolve_message(
                {
                    "sender_id": sender_id,
                    "sender_name": msg.get("sender_name") or "",
                    "chat_id": chat_id,
                    "chat_type": "group" if chat_type == ChatType.GROUP else "private",
                    "account_id": account_id,
                    "source": platform,
                },
                platform=platform,
                account_id=account_id,
            )
        except Exception as exc:
            log.debug("person_memory_identity_failed",
                      chat_id=chat_id, sender_id=sender_id, error=str(exc)[:120])
            continue
        if person is None:
            continue

        try:
            canonical = MessageEvent.new(
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                chat_type=chat_type,
                message_id=str(msg.get("msg_id") or ""),
                sender_id=sender_id,
                sender_name=str(msg.get("sender_name") or ""),
                kind=_kind(msg.get("msg_type_name")),
                content=content,
                created_at=_parse_dt(msg.get("created_at")),
            )
            extracted = await service.ingest_event_async(
                canonical,
                subject_person_id=person.person_id,
                speaker_person_id=person.person_id,
                evidence_id="",
            )
            written += len(extracted)
            all_extracted.extend(extracted)
            processed += 1
        except Exception as exc:
            log.warning("person_memory_extract_failed",
                        chat_id=chat_id, message_id=msg.get("msg_id"), error=str(exc)[:160])
            continue

    if all_extracted and features.enabled(features.PERSON_PROFILE_AGGREGATE):
        try:
            refreshed = service.refresh_profiles_for_observations(all_extracted)
            log.info("person_memory_profiles_refreshed",
                     platform=platform, account_id=account_id,
                     profiles=len(refreshed))
        except Exception as exc:
            log.warning("person_memory_profile_refresh_failed",
                        platform=platform, account_id=account_id, error=str(exc)[:160])

    if written:
        log.info("person_memory_processed",
                 platform=platform, account_id=account_id,
                 messages=processed, observations=written)
    return written


def record_claims_for_messages(
    messages, *, platform: str = "", account_id: str = "", repo: Any = None,
) -> dict[str, Any]:
    """**claim 台账写侧的生产入口**：摄取链的一批消息 → ``claim_ledger``。

    为什么落在这里（而不是另开一条链）：本模块就是 ``NEW_MESSAGES_SYNCED`` 的 canonical
    handler，``scheduler/main.py`` 那一个事件处理器已经把这一批消息交给它。claim 台账
    与 ``person_observations`` 吃的是**同一批消息**（同一处 identity、同一份 payload），
    所以这里是"既有 provenance 通道"上的一个写口，**不是第二套事实存储**：

        NEW_MESSAGES_SYNCED payload
          -> ClaimMessage（纯搬运：sender_id/chat_id/msg_id/is_self/msg_type_name）
          -> core.context.message_facts.record_message_claims   （判据 + 落库都在那里）

    三条纪律：

    * **开关门控在 owner 模块**（``message_facts.ledger_tracing()``）：``off`` 时本函数
      在取 repo **之前**就返回，连 ``get_meta_repo()`` 都不碰（逐字节一致、零 IO）；
    * **不认非文本、不认自己发的**：由 owner 的判据判（这里不做第二套判断）；
    * **失败不打断摄取**：任何异常只记日志——摄取链是消息进得来的唯一一条路，
      claim 记账写不进去不许把它带走（与 ``_trace_terminal_shadow`` 同一口径）。
    """
    from core.context import message_facts

    if not message_facts.ledger_tracing():
        return {"traced": False, "messages": 0, "rows": 0, "rejected": 0, "seen": 0}
    batch = [msg for msg in (messages or []) if isinstance(msg, dict)]
    if not batch:
        return {"traced": True, "messages": 0, "rows": 0, "rejected": 0, "seen": 0}
    if repo is None:
        try:
            repo = get_meta_repo()
        except Exception as exc:  # noqa: BLE001 - 取不到台账库 = 这次不记账
            log.warning("claim_ledger_repo_unavailable", error=str(exc)[:160])
            return {"traced": True, "messages": len(batch), "rows": 0,
                    "rejected": 0, "seen": 0}
    engine = getattr(repo, "engine", None)
    if engine is None:
        log.warning("claim_ledger_engine_missing")
        return {"traced": True, "messages": len(batch), "rows": 0,
                "rejected": 0, "seen": 0}

    keys = tuple(sorted({
        str(msg.get("sender_id") or "").strip()
        for msg in batch if str(msg.get("sender_id") or "").strip()
    }))
    # **一个批次一个台账句柄**，并且钉在同一个 ``engine`` 上：幂等要读回来的那一行
    # 就是刚写进去的那一行（读口与写口必须同一个库——写口的"默认库"是另一条路，
    # 见 ``record_message_claims`` 的纪律）。
    try:
        book = message_facts.ClaimLedger(meta_repo=repo)
    except Exception as exc:  # noqa: BLE001
        log.warning("claim_ledger_handle_failed", error=str(exc)[:160])
        return {"traced": True, "messages": len(batch), "rows": 0,
                "rejected": 0, "seen": 0}
    stats = {"traced": True, "messages": len(batch), "rows": 0,
             "rejected": 0, "seen": 0, "withdrawn": 0}
    #: 昵称 → 规范 id 的解析器（canonical 身份层的活）：**这一批消息的发言人身份**
    #: 就是手上唯一的规范表，所以只认它——认不出人就不登记（宁漏不误杀）。
    known = {str(msg.get("sender_id") or "").strip() for msg in batch}
    names = {
        str(msg.get("sender_name") or "").strip(): str(msg.get("sender_id") or "").strip()
        for msg in batch if str(msg.get("sender_name") or "").strip()
    }

    def _resolve_person(name: str) -> str:
        candidate = str(name or "").strip()
        if not candidate:
            return ""
        if candidate in known:
            return candidate
        return names.get(candidate, "")

    for msg in batch:
        message = message_facts.ClaimMessage(
            text=str(msg.get("content") or ""),
            chat_id=str(msg.get("chat_id") or ""),
            speaker_key=str(msg.get("sender_id") or ""),
            message_id=str(msg.get("msg_id") or msg.get("id") or ""),
            platform=str(platform or msg.get("source") or ""),
            account_id=str(account_id or msg.get("account_id") or ""),
            observed_at=msg.get("created_at"),
            kind=str(msg.get("msg_type_name") or "text"),
            is_self=bool(int(msg.get("is_self") or 0)),
            known_person_keys=keys,
            resolve_person=_resolve_person,
        )
        try:
            result = message_facts.record_message_claims(
                message, ledger=book, engine=engine)
        except Exception as exc:  # noqa: BLE001 - 一条坏消息不许带走整批
            log.warning("claim_ledger_record_failed",
                        chat_id=message.chat_id, message_id=message.message_id,
                        error=str(exc)[:160])
            continue
        stats["rows"] += int(result.rows)
        stats["rejected"] += len(result.rejected)
        stats["seen"] += 1 if result.already_seen else 0
        stats["withdrawn"] += int(result.withdrawn[0]) + int(result.withdrawn[1])
    if stats["rows"] or stats["rejected"] or stats["withdrawn"]:
        log.info("claim_ledger_recorded", platform=str(platform or ""),
                 account_id=str(account_id or ""), **stats)
    return stats


async def process_messages(messages, *, platform: str = "", account_id: str = "") -> int:
    """Canonical batch entry for tests and non-event callers.

    It keeps the legacy payload adapter as the conversion point, which is the
    same path Web and older tests already use.
    """
    from types import SimpleNamespace

    wrapper = SimpleNamespace(
        platform=str(platform or ""),
        account_id=str(account_id or ""),
        payload={"messages": list(messages or [])},
    )
    return await _process_payload_event(wrapper)


async def process_terminal_events(events) -> int:
    """Extract person observations from canonical terminal events.

    Track B's main path: events already carry platform/account/chat/sender and
    the trace metadata.  This method converts each event into the same
    platform-neutral ``MessageEvent`` contract used by the legacy adapter and
    then reuses ``PersonMemoryService`` unchanged.
    """
    if not features.enabled(features.PERSON_MEMORY):
        return 0
    event_list = [event for event in (events or []) if event is not None]
    if not event_list:
        return 0

    try:
        meta_repo = get_meta_repo()
        service, _composite = _build_service(meta_repo)
    except Exception as exc:  # pragma: no cover - bootstrap failure
        log.warning("person_memory_service_init_failed", error=str(exc)[:160])
        return 0

    max_messages = max(1, _env_int("YINYUE_PERSON_MEMORY_MAX_MESSAGES_PER_BATCH", 500))
    if features.enabled(features.PERSON_LLM):
        max_messages = max(1, _env_int("YINYUE_PERSON_LLM_MAX_MESSAGES_PER_BATCH", 20))

    written = 0
    processed = 0
    all_extracted = []
    for terminal_event in event_list:
        if processed >= max_messages:
            break
        metadata = dict(getattr(terminal_event, "metadata", {}) or {})
        if int(metadata.get("is_self") or 0):
            continue

        platform = str(getattr(terminal_event, "platform", "") or "")
        account_id = str(getattr(terminal_event, "bot_account_id", "") or "")
        chat_id = str(getattr(terminal_event, "chat_id", "") or "")
        content = str(getattr(terminal_event, "content", "") or "").strip()
        sender_id = str(
            metadata.get("sender_id")
            or getattr(terminal_event, "actor_account_id", "")
            or getattr(terminal_event, "actor_person_id", "")
        ).strip()
        sender_name = str(metadata.get("sender_name") or "").strip()
        principal = metadata.get("principal")
        if not sender_name and isinstance(principal, dict):
            sender_name = str(principal.get("display_name") or "").strip()
        if not platform or not account_id or not chat_id or not sender_id or not content:
            continue

        chat_type = _chat_type(platform, getattr(terminal_event, "chat_type", ""), chat_id)
        if chat_type == ChatType.UNKNOWN:
            log.debug(
                "person_memory_unknown_chat_type",
                platform=platform,
                chat_id=chat_id,
            )
            continue

        try:
            person = meta_repo.people.resolve_message(
                {
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "chat_id": chat_id,
                    "chat_type": "group" if chat_type == ChatType.GROUP else "private",
                    "account_id": account_id,
                    "source": platform,
                },
                platform=platform,
                account_id=account_id,
            )
        except Exception as exc:
            log.debug(
                "person_memory_identity_failed",
                chat_id=chat_id,
                sender_id=sender_id,
                error=str(exc)[:120],
            )
            continue
        if person is None:
            continue

        try:
            canonical = MessageEvent.new(
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                chat_type=chat_type,
                message_id=str(metadata.get("msg_id") or ""),
                sender_id=sender_id,
                sender_name=sender_name,
                kind=_kind(metadata.get("msg_type_name")),
                content=content,
                created_at=getattr(terminal_event, "occurred_at", None),
            )
            extracted = await service.ingest_event_async(
                canonical,
                subject_person_id=person.person_id,
                speaker_person_id=person.person_id,
                evidence_id=str(getattr(terminal_event, "event_id", "") or ""),
            )
            written += len(extracted)
            all_extracted.extend(extracted)
            processed += 1
        except Exception as exc:
            log.warning(
                "person_memory_extract_failed",
                chat_id=chat_id,
                message_id=metadata.get("msg_id"),
                error=str(exc)[:160],
            )
            continue

    if all_extracted and features.enabled(features.PERSON_PROFILE_AGGREGATE):
        try:
            refreshed = service.refresh_profiles_for_observations(all_extracted)
            log.info(
                "person_memory_profiles_refreshed",
                platform=str(getattr(event_list[0], "platform", "") or ""),
                account_id=str(getattr(event_list[0], "bot_account_id", "") or ""),
                profiles=len(refreshed),
            )
        except Exception as exc:
            log.warning(
                "person_memory_profile_refresh_failed",
                error=str(exc)[:160],
            )

    if written:
        log.info(
            "person_memory_processed",
            platform=str(getattr(event_list[0], "platform", "") or ""),
            account_id=str(getattr(event_list[0], "bot_account_id", "") or ""),
            messages=processed,
            observations=written,
            source="terminal_events",
        )
    return written


async def process_new_messages(event) -> int:
    """Canonical NEW_MESSAGES_SYNCED handler.

    The production path reads the canonical ``terminal_event_ids`` attached by
    ingestion instead of re-parsing the sync payload.  The payload fallback is
    kept for synthetic/test events that predate Track B.
    """
    if not features.enabled(features.PERSON_MEMORY):
        return 0

    payload = getattr(event, "payload", {}) or {}
    event_ids = list(payload.get("terminal_event_ids") or []) if isinstance(payload, dict) else []
    if not event_ids:
        return await _process_payload_event(event)
    try:
        from core.terminal.event_runtime import get_terminal_event_store

        event_store = get_terminal_event_store(force=True)
        if event_store is None:
            log.warning("person_memory_event_store_unavailable")
            return 0
        terminal_events = [
            event_store.get(str(event_id))
            for event_id in event_ids
        ]
        terminal_events = [item for item in terminal_events if item is not None]
    except Exception as exc:  # noqa: BLE001
        log.warning("person_memory_terminal_event_load_failed", error=str(exc)[:160])
        return 0
    return await process_terminal_events(terminal_events)

