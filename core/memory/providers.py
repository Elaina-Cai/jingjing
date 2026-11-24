"""MemoryHub providers for Phase 5 memory unification.

Every provider returns :class:`core.memory.contracts.MemoryItem` and receives a
deterministic ``PrincipalContext``.  Physical tables stay as they are; this
module is the compatibility layer that makes them comparable and permissions
consistent.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Protocol

import structlog

from core import temporal
from core.infra.envflags import text
from core.memory.contracts import MemoryItem, memory_item_from_hit
from safelog import warn

log = structlog.get_logger()

#: 信任级 → 归因标签（“他自报的”不等于“我知道的”，更不等于事实）。
# P0-2：key 必须覆盖 AUTHORITY_LEVELS
#: （2026-10-16 收口：这里原先把 key 写成 ``relayed``，而生产写侧发的是
#: ``message_facts.AUTHORITY_LEVELS`` 里的 ``relay`` —— 真流量的转述行全部落到兜底
#: ``[听说]``，**三级权威在渲染层退化成两级**。``relayed`` 留作 legacy alias：
#: 老行、既有注入式用例（``tests/test_needs_memory_wiring.py``）仍用那个键。）
_AUTHORITY_LABEL: dict[str, str] = {
    "self_report": "[他自报]",
    "relay": "[别人说]",
    "relayed": "[别人说]",
    "speculation": "[我猜的]",
}



def _span_offset(value: Any, default: int = -1) -> int:
    """偏移读法：**``0`` 是合法偏移**，不许被 ``or`` 吃掉（span 从 0 开始是常态）。"""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        warn("core/memory/providers.py:43 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return default


class MemoryProvider(Protocol):
    kind: str

    def recall(
        self,
        principal: Any,
        query: str,
        *,
        limit: int = 8,
        context: Any = None,
    ) -> list[MemoryItem]:
        ...


def _tokens(value: str) -> set[str]:
    text = str(value or "").lower()
    tokens: set[str] = set(re.findall(r"[a-z0-9_]+", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            tokens.add(run)
            continue
        for size in (2, 3):
            for index in range(0, len(run) - size + 1):
                tokens.add(run[index:index + size])
    return tokens


def _match(query: str, content: str) -> bool:
    q = str(query or "").strip().lower()
    c = str(content or "").strip().lower()
    if not q:
        return bool(c)
    if q in c:
        return True
    q_tokens = _tokens(q)
    c_tokens = _tokens(c)
    if not q_tokens or not c_tokens:
        return False
    common = q_tokens & c_tokens
    overlap = len(common)
    if overlap / max(len(q_tokens), 1) >= 0.35:
        return True
    # 短事实是「实体 = 值」形状，整句问法做分母会把它稀释到 0.0x：
    # 「晚上想煮个汤，香菜还放吗？」记不住「香菜 = 不吃」。短内容改看内容侧，
    # 长文（笔记、聊天记录）仍走 query 侧阈值，避免整库召回被放宽。
    if len(c_tokens) <= 8 and max((len(token) for token in common), default=0) >= 2:
        return True
    return False


def _aware(value: Any) -> datetime | None:
    """归一为应用时钟（naive 本地墙钟）；换算口径唯一 owner = ``temporal``。

    字符串也收：台账与部分表把时间存成 ``"2026-10-10 11:18:03"``，而
    ``MemoryItem.occurred_at`` 的契约是 ``datetime | None``——此前只处理
    ``tzinfo`` 属性，字符串会直接漏过去，直到 hub 排序时才爆
    ``AttributeError: 'str' object has no attribute 'timestamp'``。在这里一次性收口。
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return temporal.parse_datetime(value)
    if getattr(value, "tzinfo", None) is None:
        return value
    return temporal.to_naive_local(value)


def _is_owner(principal: Any) -> bool:
    return bool(getattr(principal, "is_owner", False))


def _is_group(principal: Any) -> bool:
    return bool(getattr(principal, "is_group", False))


def _visible_scope(principal: Any) -> Any:
    """Current conversation + requester, as the R-I1 recall scope.

    宪法级不变量（docs/需求契约.md §三 R-I1/R-I2/R-I3）：查记忆只允许「该动作
    发起人 + 当前会话」。scope 只能由**真实身份**（``PrincipalContext``）推出：
    没有 platform/account/发起人、或群会话没有会话键 → 返回 ``None``，调用方必须
    拒绝读取（fail closed），**不许**退回无过滤的"按 person_id 全取"。

    chat_type 缺失时按既有的 provider 口径当私聊（见 ``HistoryMemoryProvider._scope``：
    非群即私聊），私聊的"当前会话"就是发起人自己那一间。
    """
    from core.contracts.events import ChatType
    from core.people.visibility import VisibleScope

    platform = str(getattr(principal, "platform", "") or "").strip()
    account_id = str(getattr(principal, "bot_account_id", "") or "").strip()
    person_id = str(getattr(principal, "person_id", "") or "").strip()
    if not platform or not account_id or not person_id:
        return None
    raw_chat_type = getattr(principal, "chat_type", "")
    value = str(getattr(raw_chat_type, "value", raw_chat_type) or "").strip().lower()
    if _is_group(principal) or value == ChatType.GROUP.value:
        chat_type = ChatType.GROUP
    elif value == ChatType.CHANNEL.value:
        chat_type = ChatType.CHANNEL
    elif value in ("", ChatType.PRIVATE.value):
        chat_type = ChatType.PRIVATE
    else:
        # 认不出的会话类型 → 交给 VisibleScope.problem() 拒绝（不猜）。
        chat_type = ChatType.UNKNOWN
    chat_id = str(getattr(principal, "chat_id", "") or "").strip()
    if chat_type in (ChatType.GROUP, ChatType.CHANNEL) and not chat_id:
        chat_id = str(getattr(principal, "group_id", "") or "").strip()
    return VisibleScope(
        platform=platform,
        account_id=account_id,
        requester_person_id=person_id,
        chat_type=chat_type,
        chat_id=chat_id,
        viewer_is_owner=_is_owner(principal),
    )


def _log_scope_denied(*, provider: str, principal: Any, scope: Any, reason: str) -> None:
    """Never raises: a failed log must not become a fallback read."""
    try:
        log.warning(
            "memory_recall_scope_denied",
            provider=provider,
            reason=reason,
            person_id=str(getattr(principal, "person_id", "") or ""),
            platform=str(getattr(principal, "platform", "") or ""),
            account_id=str(getattr(principal, "bot_account_id", "") or ""),
            chat_type=str(
                getattr(getattr(scope, "chat_type", ""), "value", "")
                or getattr(scope, "chat_type", "")
                or ""
            ),
            chat_id=str(getattr(scope, "chat_id", "") or ""),
            invariant="R-I1/R-I3",
        )
    except Exception:  # noqa: BLE001
        warn("core/memory/providers.py:175 Exception", err=repr(__import__("sys").exc_info()[1]))


def _log_knowledge_silent_skip(*, reason: str, query: str, exc: Any = None) -> None:
    """Never raises: a failed log must not become a provider failure.

    2026-10-18 task-37/38：knowledge 这一格原先两个出口都是静默 ``return []``，
    生产里「库中有 >=0.2 分的文档、召回却 0 条」查不出原因（80 条带 by_kind 的
    日志行里 by_kind.fact 合计只有 1）。这里只补**可观测性**：不改返回值、不改
    阈值、不改排序；日志失败一律吞掉。
    """
    try:
        log.warning(
            "knowledge_recall_silent_skip",
            reason=reason,
            query=str(query or "")[:80],
            exc=repr(exc)[:200] if exc is not None else "",
        )
    except Exception:  # noqa: BLE001
        warn("core/memory/providers.py:194 Exception", err=repr(__import__("sys").exc_info()[1]))


class PersonalMemoryProvider:
    kind = "personal"

    def __init__(self, gateway: Any = None):
        self._gateway = gateway

    def recall(
        self,
        principal: Any,
        query: str,
        *,
        limit: int = 8,
        context: Any = None,
    ) -> list[MemoryItem]:
        gateway = self._gateway
        if gateway is None:
            from core.terminal.memory_gateway import get_memory_gateway

            gateway = get_memory_gateway()
        if gateway is None:
            return []
        hits = gateway.recall(principal, query=query, limit=limit)
        return [memory_item_from_hit(hit) for hit in hits]


class HistoryMemoryProvider:
    """Conversation history from account DB messages via UnifiedQuery."""

    kind = "history"

    def __init__(self, unified_query: Any = None):
        self._uq = unified_query

    @property
    def unified_query(self) -> Any:
        if self._uq is None:
            from core.terminal.store_port import get_unified_query

            self._uq = get_unified_query()
        return self._uq

    def _scope(self, principal: Any) -> tuple[str, str]:
        if _is_group(principal):
            return str(getattr(principal, "chat_id", "") or ""), "group"
        if _is_owner(principal):
            return "", ""
        return str(getattr(principal, "chat_id", "") or ""), "private"

    def search_raw(
        self,
        *,
        chat_id: str = "",
        chat_type: str = "",
        sender_id: str = "",
        since: Any = None,
        until: Any = None,
        limit: int = 50,
        platform: str = "",
        account_id: str = "",
        keyword: str = "",
    ) -> list[Any]:
        """Raw UnifiedQuery results; lets richer tools keep their formatting."""
        try:
            return list(
                self.unified_query.search_messages(
                    chat_id=chat_id or None,
                    chat_type=chat_type or None,
                    sender_id=sender_id or None,
                    since=since,
                    until=until,
                    limit=max(1, int(limit or 1)),
                    platform=platform or None,
                    account_id=account_id or None,
                    keyword=keyword or None,
                )
            )
        except Exception:
            warn("core/memory/providers.py:274 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []

    def recall(
        self,
        principal: Any,
        query: str,
        *,
        limit: int = 8,
        context: Any = None,
    ) -> list[MemoryItem]:
        if principal is None or not getattr(principal, "person_id", ""):
            return []
        chat_id, chat_type = self._scope(principal)
        platform = str(getattr(principal, "platform", "") or "")
        account_id = str(getattr(principal, "bot_account_id", "") or "")
        results = self.search_raw(
            chat_id=chat_id,
            chat_type=chat_type,
            limit=max(int(limit) * 10, 50),
            platform=platform,
            account_id=account_id,
        )
        items: list[MemoryItem] = []
        for result in results:
            message = getattr(result, "data", None)
            if message is None:
                continue
            content = str(getattr(message, "content", "") or "")
            if not _match(query, content):
                continue
            message_id = str(getattr(message, "msg_id", "") or "")
            items.append(
                MemoryItem(
                    memory_id=f"history:{message_id}",
                    kind=self.kind,
                    content=content[:600],
                    occurred_at=_aware(getattr(message, "created_at", None)),
                    platform=str(getattr(result, "platform", "") or platform),
                    bot_account_id=str(
                        getattr(result, "account_id", "") or account_id
                    ),
                    chat_id=str(getattr(message, "chat_id", "") or chat_id),
                    chat_type=str(getattr(message, "chat_type", "") or chat_type),
                    speaker=str(
                        getattr(message, "sender_name", "")
                        or getattr(message, "sender_id", "")
                        or ""
                    ),
                    source_message_ids=(message_id,) if message_id else (),
                    confidence=0.6,
                    importance=0.5,
                    metadata={"source": "account_messages"},
                )
            )
            if len(items) >= max(1, int(limit)):
                break
        return items


class TopicMemoryProvider:
    """Thread/topic memory over meta DB threads + account messages."""

    kind = "topic"

    def __init__(self, meta_repo: Any = None, unified_query: Any = None):
        self._meta = meta_repo
        self._uq = unified_query

    @property
    def meta(self) -> Any:
        if self._meta is None:
            from core.terminal.store_port import get_meta_repo

            self._meta = get_meta_repo()
        return self._meta

    @property
    def unified_query(self) -> Any:
        if self._uq is None:
            from core.terminal.store_port import get_unified_query

            self._uq = get_unified_query()
        return self._uq

    @staticmethod
    def _allowed(key, principal) -> bool:
        from core.memory.contact_scope import chat_allowed
        return chat_allowed(key, principal)

    def find_threads(self, principal, *, chat_id: str = "", limit: int = 200) -> list[Any]:
        from db.thread_scope import source_key
        if principal is None:
            return []
        current = str(getattr(principal, "chat_id", "") or "")
        target = str(chat_id or "")
        cross = _is_owner(principal) and not _is_group(principal)
        if target and target != current and not cross:
            return []
        platform = str(getattr(principal, "platform", "") or "")
        account = str(getattr(principal, "bot_account_id", "") or "")
        rows = self.meta.threads.find_scoped(platform, account, chat_id=target or ("" if cross else current), limit=max(1, int(limit)))
        return [t for t in rows if self._allowed(source_key(t), principal)
                and (not target or str(t.chat_id) == target)]

    def get_thread(self, thread_id: int, *, principal=None) -> Any:
        from db.thread_scope import source_key
        if principal is None:
            return None
        row = self.meta.threads.get_by_id(int(thread_id))
        return row if self._allowed(source_key(row), principal) else None

    def get_thread_refs(self, thread_id: int, *, principal=None, limit: int = 10, status=None) -> list[Any]:
        from db.thread_scope import source_key
        thread = self.get_thread(thread_id, principal=principal)
        if thread is None:
            return []
        key = source_key(thread)
        return [r for r in self.meta.thread_refs.get_by_thread(int(thread_id), status=status, limit=max(1, int(limit)))
                if source_key(r) == key]

    def resolve_ref(self, ref, principal=None) -> Any:
        from db.thread_scope import source_key
        key = source_key(ref)
        if not self._allowed(key, principal):
            return None
        return self.resolve_message(int(getattr(ref, "msg_id", 0) or 0), principal,
                                    platform=key[0], account_id=key[1], chat_id=key[2])

    def resolve_message(self, message_id: int, principal=None, *, platform: str = "", account_id: str = "", chat_id: str = "") -> Any:
        key = (str(platform or ""), str(account_id or ""), str(chat_id or ""))
        if not all(key) or not self._allowed(key, principal) or not message_id:
            return None
        from db.thread_scope import resolve_source_message
        return resolve_source_message(self.unified_query, message_id, key)

    def _threads(self, principal) -> list[Any]:
        return self.find_threads(principal, limit=100)

    def _resolve_message(self, ref, principal) -> Any:
        return self.resolve_ref(ref, principal)

    def recall(
        self,
        principal: Any,
        query: str,
        *,
        limit: int = 8,
        context: Any = None,
    ) -> list[MemoryItem]:
        if principal is None or not getattr(principal, "person_id", ""):
            return []
        items: list[MemoryItem] = []
        for thread in self._threads(principal):
            scope = str(getattr(thread, "scope", "") or "")
            summary = str(getattr(thread, "summary", "") or "")
            if not _match(query, scope + "\n" + summary):
                continue
            refs = self.get_thread_refs(int(thread.id), principal=principal, limit=5)
            lines = [scope] + ([summary] if summary else [])
            message_ids: list[str] = []
            for ref in refs:
                message = self._resolve_message(ref, principal)
                if message is None:
                    continue
                content = str(getattr(message, "content", "") or "")
                if content:
                    lines.append(content[:160])
                message_id = str(getattr(message, "msg_id", "") or "")
                if message_id:
                    message_ids.append(message_id)
            items.append(
                MemoryItem(
                    memory_id=f"topic:{getattr(thread, 'id', '')}",
                    kind=self.kind,
                    content="\n".join(lines)[:800],
                    occurred_at=_aware(
                        getattr(thread, "last_active_at", None)
                        or getattr(thread, "updated_at", None)
                        or getattr(thread, "created_at", None)
                    ),
                    platform=str(getattr(principal, "platform", "") or ""),
                    bot_account_id=str(
                        getattr(principal, "bot_account_id", "") or ""
                    ),
                    chat_id=str(getattr(thread, "chat_id", "") or ""),
                    chat_type="group",
                    source_message_ids=tuple(message_ids),
                    confidence=0.6,
                    importance=0.6,
                    metadata={
                        "source": "threads",
                        "thread_id": int(getattr(thread, "id", 0) or 0),
                        "scope": scope,
                    },
                )
            )
            if len(items) >= max(1, int(limit)):
                break
        return items


class CoreMemoryProvider:
    kind = "core"

    def __init__(self, meta_repo: Any = None):
        self._meta = meta_repo

    @property
    def meta(self) -> Any:
        if self._meta is None:
            from core.terminal.store_port import get_meta_repo

            self._meta = get_meta_repo()
        return self._meta

    def recall(
        self,
        principal: Any,
        query: str,
        *,
        limit: int = 8,
        context: Any = None,
    ) -> list[MemoryItem]:
        if principal is None or not _is_owner(principal):
            return []
        try:
            personas = list(self.meta.personas.list_active())
            persona_id = int(getattr(personas[0], "id", 1)) if personas else 1
            blocks = list(self.meta.core_memory.list_blocks(persona_id))
        except Exception:
            warn("core/memory/providers.py:543 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []
        items: list[MemoryItem] = []
        for block in blocks:
            content = str(getattr(block, "content", "") or "")
            if not _match(query, content):
                continue
            items.append(
                MemoryItem(
                    memory_id=f"core:{getattr(block, 'block', '')}",
                    kind=self.kind,
                    content=content[:1000],
                    platform=str(getattr(principal, "platform", "") or ""),
                    bot_account_id=str(
                        getattr(principal, "bot_account_id", "") or ""
                    ),
                    chat_id=str(getattr(principal, "chat_id", "") or ""),
                    chat_type=str(
                        getattr(getattr(principal, "chat_type", ""), "value", "")
                        or ""
                    ),
                    visibility_scope=("owner_only",),
                    confidence=0.9,
                    importance=1.0,
                    metadata={
                        "source": "core_memories",
                        "block": str(getattr(block, "block", "") or ""),
                    },
                )
            )
            if len(items) >= max(1, int(limit)):
                break
        return items


class PersonMemoryProvider:
    kind = "person"

    def __init__(self, repository: Any = None):
        self._repo = repository

    @property
    def repository(self) -> Any:
        if self._repo is None:
            from core.terminal.store_port import get_meta_repo
            from db.repo_person_memory import PersonMemoryRepository

            self._repo = PersonMemoryRepository(get_meta_repo().engine)
        return self._repo

    def recall(
        self,
        principal: Any,
        query: str,
        *,
        limit: int = 8,
        context: Any = None,
    ) -> list[MemoryItem]:
        from core import features

        # 读侧开关：写线关（PERSON_MEMORY=0）时绝不召回存量旧观察；
        # 影子提取（只写不召回）= MEMORY=1 + RECALL=0。
        if not features.enabled(features.PERSON_MEMORY) or not features.enabled(
            features.PERSON_RECALL
        ):
            return []
        person_id = str(getattr(principal, "person_id", "") or "")
        if not person_id:
            return []
        # R-I1/R-I3：没有合法 scope 就拒绝读取（空 + warning），绝不无过滤放行。
        scope = _visible_scope(principal)
        reason = "missing_scope" if scope is None else scope.problem()
        if reason:
            _log_scope_denied(
                provider=self.kind, principal=principal, scope=scope, reason=reason
            )
            return []
        try:
            observations = self.repository.list_for_person(
                person_id,
                scope=scope,
                limit=max(int(limit) * 5, 20),
            )
        except Exception:
            warn("core/memory/providers.py:626 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []
        items: list[MemoryItem] = []
        for observation in observations:
            content = str(getattr(observation, "content", "") or "")
            if not _match(query, content):
                continue
            visibility = str(
                getattr(
                    getattr(observation, "visibility", ""),
                    "value",
                    getattr(observation, "visibility", ""),
                )
                or ""
            )
            items.append(
                MemoryItem(
                    memory_id=f"person:{getattr(observation, 'observation_id', '')}",
                    kind=self.kind,
                    content=content[:600],
                    occurred_at=_aware(
                        getattr(observation, "observed_at", None)
                        or getattr(observation, "occurred_at", None)
                    ),
                    platform=str(getattr(observation, "platform", "") or ""),
                    bot_account_id=str(
                        getattr(observation, "account_id", "") or ""
                    ),
                    chat_id=str(getattr(observation, "occurred_scope_id", "") or ""),
                    chat_type=str(
                        getattr(
                            getattr(observation, "occurred_scope_type", ""),
                            "value",
                            getattr(observation, "occurred_scope_type", ""),
                        )
                        or ""
                    ),
                    speaker=str(
                        getattr(observation, "speaker_person_id", "") or ""
                    ),
                    subject_person_ids=(person_id,),
                    # 观察自己的可见性原样带上（不留空 scope：任何下游权限门
                    # 看到 private_only / owner_only 都会保守拒绝）。
                    visibility_scope=(visibility,) if visibility else (),
                    confidence=float(
                        getattr(observation, "confidence", 0.6) or 0.6
                    ),
                    importance=float(
                        getattr(observation, "importance", 0.5) or 0.5
                    ),
                    metadata={
                        "source": "person_observations",
                        "visibility": visibility,
                        "sensitivity": str(
                            getattr(observation, "sensitivity", "") or ""
                        ),
                    },
                )
            )
            if len(items) >= max(1, int(limit)):
                break
        return items


class ClaimMemoryProvider:
    """“谁在何时自报/转述/推测了什么”——claim 台账的只读回忆口。

    台账本身（``claim_ledger``）已经在写（写侧 = ``core.context.message_facts.record_message_claims``），
    但之前**没有任何召回口读它**——于是每天的自报都烂在库里。本 provider 就是这条路。

    三条口径：
    * **只读仍有效的行**（取消/被更新掉的不返回，到期与 ``resolve`` 同一口径）；
    * **信任级写进正文**（“他自报 / 别人说 / 我猜的”）——人不会把推测当事实说；
    * ``YINYUE_FEATURE_CLAIM_LEDGER`` 关（默认 off）时**逐字节不变**：这个 provider 一条都不吐。

    条目可见性：台账按 ``chat_id`` 存，因此只向**同一个会话里的人**回流（跨群不泄露）。
    """

    kind = "claim"

    def __init__(self, ledger: Any = None):
        self._ledger = ledger

    @property
    def ledger(self) -> Any:
        if self._ledger is None:
            from core.context.message_facts import ClaimLedger

            self._ledger = ClaimLedger()
        return self._ledger

    def recall(
        self,
        principal: Any,
        query: str,
        *,
        limit: int = 8,
        context: Any = None,
    ) -> list[MemoryItem]:
        from core import features

        try:
            if features.mode(features.CLAIM_LEDGER, features.MODE_OFF) == features.MODE_OFF:
                return []
        except Exception:
            warn("core/memory/providers.py:730 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []
        chat_id = str(getattr(principal, "chat_id", "") or "")
        if not chat_id:
            return []
        try:
            rows = self.ledger.claims_in_chat(
                chat_id,
                platform=str(getattr(principal, "platform", "") or ""),
                account_id=str(getattr(principal, "bot_account_id", "") or ""),
                limit=max(int(limit) * 5, 20),
            )
        except Exception:
            warn("core/memory/providers.py:742 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []
        names = self._names()
        items: list[MemoryItem] = []
        for row in rows:
            text = self._render(row, names)
            if not _match(query, text):
                continue
            items.append(
                MemoryItem(
                    memory_id=f"claim:{getattr(row, 'id', '')}",
                    kind=self.kind,
                    content=text,
                    occurred_at=_aware(str(getattr(row, "observed_at", "") or "")),
                    platform=str(getattr(row, "platform", "") or ""),
                    bot_account_id=str(getattr(row, "account_id", "") or ""),
                    chat_id=str(getattr(row, "chat_id", "") or ""),
                    speaker=str(getattr(row, "source_actor_id", "") or ""),
                    subject_person_ids=(str(getattr(row, "person_key", "") or ""),),
                    confidence=0.9 if str(getattr(row, "authority", "")) == "self_report" else 0.6,
                    importance=0.6,
                    metadata={
                        "source": "claim_ledger",
                        "field_key": str(getattr(row, "field_key", "") or ""),
                        "authority": str(getattr(row, "authority", "") or ""),
                    },
                )
            )
            if len(items) >= max(1, int(limit)):
                break
        return items

    # ---- 内部 --------------------------------------------------------------
    @staticmethod
    def _render(row: Any, names: dict[str, str]) -> str:
        who = names.get(str(getattr(row, "person_key", "") or "")) or "有人"
        field_key = str(getattr(row, "field_key", "") or "")
        value = str(getattr(row, "value", "") or "")
        authority = str(getattr(row, "authority", "") or "")
        label = _AUTHORITY_LABEL.get(authority, "[听说]")
        stamp = str(getattr(row, "observed_at", "") or "")[:10]
        tail = f"（群里 {stamp}）" if stamp else ""
        # 读侧透镜要的是**出处**：有位置就把原话那一段附上。
        # 老行（迁移 59 之前）两列为空 → 输出与旧版**逐字相同**。
        source_text = str(getattr(row, "source_text", "") or "")
        start = _span_offset(getattr(row, "evidence_start", -1))
        end = _span_offset(getattr(row, "evidence_end", -1))
        quote = ""
        if source_text and start >= 0 and end > start:
            quote = source_text[start:end][:80]
        if not quote:
            quote = str(getattr(row, "evidence_text", "") or "")[:80]
        if quote:
            tail = f"{tail}（原话：{quote}）"
        return f"{label}【{field_key}】{who} = {value}{tail}"

    def _names(self) -> dict[str, str]:
        try:
            from sqlalchemy import text as _sql

            from core.terminal.store_port import get_meta_repo

            with get_meta_repo().engine.connect() as conn:
                rows = conn.execute(
                    _sql("select id, display_name from person_entities")
                ).mappings().all()
        except Exception:
            warn("core/memory/providers.py:808 Exception", err=repr(__import__("sys").exc_info()[1]))
            return {}
        out: dict[str, str] = {}
        for row in rows:
            key = str(row.get("id") or "")
            name = str(row.get("display_name") or "")
            if key and name:
                out[key] = name
        return out


class WorkingMemoryProvider:
    """Session summaries from the meta DB, owner-visible only."""

    kind = "working"

    def __init__(self, meta_repo: Any = None):
        self._meta = meta_repo

    @property
    def meta(self) -> Any:
        if self._meta is None:
            from core.terminal.store_port import get_meta_repo

            self._meta = get_meta_repo()
        return self._meta

    def recall(
        self,
        principal: Any,
        query: str,
        *,
        limit: int = 8,
        context: Any = None,
    ) -> list[MemoryItem]:
        if principal is None or not _is_owner(principal):
            return []
        try:
            personas = list(self.meta.personas.list_active())
            persona_id = int(getattr(personas[0], "id", 1)) if personas else 1
            sessions = list(self.meta.sessions.list_by_persona(persona_id, limit=50))
        except Exception:
            warn("core/memory/providers.py:849 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []
        items: list[MemoryItem] = []
        for session in sessions:
            content = str(
                getattr(session, "summary", "")
                or getattr(session, "title", "")
                or ""
            )
            if not content or not _match(query, content):
                continue
            items.append(
                MemoryItem(
                    memory_id=f"working:{getattr(session, 'id', '')}",
                    kind=self.kind,
                    content=content[:800],
                    platform=str(getattr(principal, "platform", "") or ""),
                    bot_account_id=str(
                        getattr(principal, "bot_account_id", "") or ""
                    ),
                    visibility_scope=("owner_only",),
                    confidence=0.5,
                    importance=0.5,
                    metadata={
                        "source": "chat_sessions",
                        "session_id": int(getattr(session, "id", 0) or 0),
                    },
                )
            )
            if len(items) >= max(1, int(limit)):
                break
        return items


#: 问句里的纯疑问/虚词：参与 ``_tokens`` 但不该进 knowledge 的匹配分母。
#: 反例（实测）：query=「崩铁 4.4 版本 什么时候 上线」共 9 个 token，正文命中
#: 「4 / 版本 / 上线」=3 → 3/9=0.333 < 0.35，被 ``_match`` 判成不相关。
_QUERY_STOP = frozenset({
    "什么", "什么时", "什么时候", "么时", "么时候", "时候", "多久", "几点",
    "多少", "怎么", "怎样", "如何", "哪里", "哪儿", "哪个", "哪个时", "谁",
    "是不是", "有没有", "能不能", "可不可以", "吗", "呢", "吧", "啊", "的", "了",
    "是", "在", "有", "和", "跟", "就", "还", "也", "都", "我", "你", "他", "她",
})


def _content_query_tokens(query: str) -> set[str]:
    """query 的内容词（去掉纯疑问/虚词与单字），用于 knowledge 的覆盖率打分。"""
    return {
        token
        for token in _tokens(query)
        if token not in _QUERY_STOP and len(str(token)) >= 2
    }


def _knowledge_score(q_tokens: set[str], title: str, content: str) -> float:
    """knowledge_docs 的匹配分：标题命中权重 2、正文 1，按 query token 覆盖率归一。

    只在 ``_match`` 判过之后再算（保证「至少 35% token 覆盖」这条底线不变），
    用途是**排序**——旧实现按插入顺序取前 N 条，命中的旧文档会把新文档挤掉。
    返回 (0, 1]；0 表示没有 token 命中（调用方跳过）。
    """
    if not q_tokens:
        return 0.0
    t_tokens = _tokens(title)
    c_tokens = _tokens(content)
    covered = 0
    for token in q_tokens:
        if token in t_tokens:
            covered += 2
        elif token in c_tokens:
            covered += 1
    if covered <= 0:
        return 0.0
    return covered / (2.0 * len(q_tokens))


#: knowledge 命中的最低覆盖率（内容词）。0.2 = 4 个内容词命中 1 个即可候选，
#: 排序仍由 ``_knowledge_score`` 决定，注入侧还有 6 条/600 字上限。
_KNOWLEDGE_MIN_SCORE = 0.2


class KnowledgeMemoryProvider:
    kind = "knowledge"

    def __init__(self, meta_repo: Any = None):
        self._meta = meta_repo

    @property
    def meta(self) -> Any:
        if self._meta is None:
            from core.terminal.store_port import get_meta_repo

            self._meta = get_meta_repo()
        return self._meta

    def recall(
        self,
        principal: Any,
        query: str,
        *,
        limit: int = 8,
        context: Any = None,
    ) -> list[MemoryItem]:
        # 2026-10-17：knowledge_docs 是**全局 web 知识**（不属于某个人），旧实现要求
        # ``principal.person_id`` 才查——而群消息的 principal 没有 person_id，这条路
        # 在群里**恒空**（实测 86 行文档、0 命中）。改为只要求 principal 存在。
        if principal is None:
            _log_knowledge_silent_skip(reason="principal_none", query=query)
            return []
        try:
            docs = list(self.meta.knowledge_docs.list_all(limit=200))
        except Exception as exc:  # noqa: BLE001
            # 行为不变（仍然返回空），只把被吞掉的原因写出来（task-37 的 fact 空转）。
            _log_knowledge_silent_skip(reason="exception", query=query, exc=exc)
            return []
        q_tokens = _content_query_tokens(query)
        if not q_tokens:
            # 整句都是疑问词（「你还记得吗」）：退回旧的宽匹配，别把这条路堵死
            q_tokens = _tokens(query)
        scored: list[tuple[float, int, Any, str]] = []
        for doc in docs:
            content = str(
                getattr(doc, "content", "")
                or getattr(doc, "summary", "")
                or ""
            )
            if not content:
                continue
            title = str(getattr(doc, "title", "") or "")
            score = _knowledge_score(q_tokens, title, content)
            if score < _KNOWLEDGE_MIN_SCORE:
                continue
            scored.append((score, int(getattr(doc, "id", 0) or 0), doc, content))
        # 先按匹配分（标题命中权重 2 倍），同分再按新旧；不再「按插入顺序取前 N」。
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        items: list[MemoryItem] = []
        for score, _doc_id, doc, content in scored[: max(1, int(limit))]:
            created = _aware(getattr(doc, "created_at", None))
            items.append(
                MemoryItem(
                    memory_id=f"knowledge:{getattr(doc, 'id', '')}",
                    kind=self.kind,
                    content=content[:600],
                    occurred_at=created,
                    source_message_ids=(),
                    confidence=0.5,
                    importance=0.5,
                    metadata={
                        "source": "knowledge_docs",
                        "provenance_status": "unverified_capture",
                        "time_semantics": "captured_at",
                        "validity_status": "unknown",
                        # 抓取时间（本表 created_at 才是抓取时刻；updated_at 全为 NULL）
                        "as_of": created.isoformat() if created else "",
                        "score": round(float(score), 4),
                        "url": str(getattr(doc, "source_url", "") or ""),
                        "query": str(getattr(doc, "original_query", "") or ""),
                        "source_type": str(getattr(doc, "source_type", "") or ""),
                        "source_file": str(getattr(doc, "source_file", "") or ""),
                        "volatility": "volatile",
                    },
                )
            )
        return items


__all__ = [
    "CoreMemoryProvider",
    "HistoryMemoryProvider",
    "KnowledgeMemoryProvider",
    "MemoryProvider",
    "PersonMemoryProvider",
    "PersonalMemoryProvider",
    "TopicMemoryProvider",
    "MemeMemoryProvider",
    "WebMemeMemoryProvider",
    "WorkingMemoryProvider",
]

class MemeMemoryProvider:
    """kind='meme'：只读 ② 世界模型的 memes 投影（群体记忆/梗条目）。

    物理上不新建库——梗在 ②（meme_observed 事件），本 provider 只把投影
    翻译成 MemoryItem，供 ⑩ 的 query+kinds 召回（先②后⑨ 的②一侧）。
    """

    kind = "meme"

    _norm_re = re.compile(r"[^\w\u4e00-\u9fff]+")

    def recall(self, principal: Any, query: str = "", limit: int = 8, context: Any = None) -> list[MemoryItem]:
        chat_id = str(getattr(principal, "chat_id", "") or "")
        if not chat_id:
            return []
        try:
            from core.context.situation import project
            from core.context.situation_store import get_world_model
            from core.temporal import parse_datetime

            model = get_world_model(chat_id)
            if model is None:
                return []
            memes = project(model.events).memes
        except Exception:  # noqa: BLE001 - 召回失败按查不到处理
            warn("core/memory/providers.py:1052 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []
        q = str(query or "").strip()
        hits: list[MemoryItem] = []
        for m in memes:
            key = str(getattr(m, "key", "") or "")
            if not key:
                continue
            q_norm = self._norm_re.sub("", q)
            key_norm = self._norm_re.sub("", key)
            if q_norm and q_norm not in key_norm and key_norm not in q_norm:
                continue
            occurred_at = None
            try:
                occurred_at = parse_datetime(str(getattr(m, "last_at", "") or ""))
            except Exception:  # noqa: BLE001
                warn("core/memory/providers.py:1067 Exception", err=repr(__import__("sys").exc_info()[1]))
                occurred_at = None
            count = max(1, int(getattr(m, "count", 1) or 1))
            hits.append(MemoryItem(
                memory_id=f"meme:{chat_id}:{key}",
                kind="meme",
                content=f"[梗] {key}（群里出现过 {count} 次）",
                occurred_at=occurred_at,
                chat_id=chat_id,
                confidence=0.8,
                importance=min(0.9, 0.4 + 0.1 * count),
                metadata={
                    "count": count,
                    "evidence": str(getattr(m, "evidence", "") or "")[:120],
                    "first_at": str(getattr(m, "first_at", "") or ""),
                },
            ))
        hits.sort(key=lambda item: item.importance, reverse=True)
        return hits[: max(1, int(limit))]


class WebMemeMemoryProvider:
    """kind='meme'：读 web 梗词条索引（data/web_memes/memes_index.json），只召回 approved。

    层3（web 梗库）在⑨一侧；索引由 scripts/build_meme_index.py 生成；
    默认 pending，主人审批（scripts/approve_memes.py）后才进召回。
    """

    kind = "meme"

    # 2026-10-16：键名太短会拿通用词（“梗”“6”）撞子串命中（探针：
    # 7 条真实缺口里 5 条命中都是噪声）。键名（含别名）至少 3 字才进
    # 子串匹配。
    MIN_KEY_LEN = 3
    # 键名长于问句时（问 "疯狂星期四" 命中 "疯狂星期四文学"）
    # 要更严：短键名（"梗" "6" "yee"）不能靠这条进来。
    MIN_LONGER_KEY_LEN = 5

    def __init__(self, index_path: Any = None) -> None:
        import os
        import pathlib

        env = text("YINYUE_WEB_MEME_INDEX")
        root = pathlib.Path(__file__).resolve().parents[2]
        self._path = pathlib.Path(index_path or env or (root / "data/web_memes/memes_index.json"))
        self._cache: list[MemoryItem] = []
        self._mtime = -1.0

    @staticmethod
    def _norm(text: Any) -> str:
        return "".join(
            ch for ch in str(text or "").lower()
            if ch.isalnum() or 0x4E00 <= ord(ch) <= 0x9FFF
        )

    def _load(self) -> None:
        import json

        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            warn("core/memory/providers.py:1127 OSError", err=repr(__import__("sys").exc_info()[1]))
            self._cache, self._mtime = [], -1.0
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 索引坏了按空处理
            warn("core/memory/providers.py:1134 Exception", err=repr(__import__("sys").exc_info()[1]))
            self._cache, self._mtime = [], mtime
            return
        items: list[MemoryItem] = []
        for entry in (data.get("entries") or []):
            if not isinstance(entry, dict) or entry.get("status") != "approved":
                continue
            key = str(entry.get("key") or "").strip()
            if not key:
                continue
            aliases = [str(a).strip() for a in (entry.get("aliases") or []) if str(a).strip()]
            videos = entry.get("videos") or []
            bvid = ""
            if videos and isinstance(videos[0], dict):
                bvid = str(videos[0].get("bvid") or "")
            heat = int(entry.get("heat") or 0)
            items.append(MemoryItem(
                memory_id="web:" + self._norm(key),
                kind="meme",
                content="[梗] " + key + "：" + str(entry.get("meaning") or ""),
                confidence=0.7,
                importance=min(0.9, 0.5 + 0.05 * heat),
                metadata={
                    "source": "web",
                    "key": key,
                    "aliases": aliases,
                    "bvid": bvid,
                    "example": str(entry.get("example") or "")[:160],
                    "origin": str(entry.get("origin") or "")[:160],
                    "heat": heat,
                },
            ))
        self._cache, self._mtime = items, mtime

    def recall(self, principal: Any, query: str = "", limit: int = 8, context: Any = None) -> list[MemoryItem]:
        self._load()
        if not self._cache:
            return []
        q_norm = self._norm(query)
        if not q_norm:
            return []
        hits: list[MemoryItem] = []
        for item in self._cache:
            names = [str(item.metadata.get("key") or "")]
            names.extend(str(a) for a in (item.metadata.get("aliases") or []))
            for name in names:
                n = self._norm(name)
                # 2026-10-16：两个方向都要满足长度门槛，避开
                # 通用短词（"梗" "6" "yee"）撞子串造噪声。
                if not n:
                    continue
                if n in q_norm:
                    if len(n) < self.MIN_KEY_LEN and n != q_norm:
                        continue
                elif q_norm in n and len(n) >= self.MIN_LONGER_KEY_LEN:
                    pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）
                else:
                    continue
                hits.append(item)
                break
        hits.sort(key=lambda it: (it.importance, it.confidence), reverse=True)
        return hits[: max(1, int(limit))]
