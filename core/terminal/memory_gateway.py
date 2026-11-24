"""Phase 2 bounded memory recall over the terminal event stream.

This is the first MemoryGateway increment.  It does not yet create a separate
memory projection table: it performs deterministic, bounded recall directly
over ``terminal_events`` and returns explainable hits (source event ids,
timestamps, visibility scope).  A later step can persist extracted/summarized
projections without changing the gateway contract.

Hard rules encoded here:

* every recall takes a :class:`PrincipalContext`;
* the event store's visibility ACL runs first;
* the principal's ``memory_scopes`` runs second (unknown identity => no hits);
* owner-only chain-of-thought events never enter memory recall;
* results are bounded by count and prompt character budget;
* no LLM call, no write -- pure read.
"""

from __future__ import annotations

import math
import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from core import features, temporal
from core.terminal.event_model import Event, EventType, Sensitivity
from core.terminal.principal import (
    Capability,
    PrincipalContext,
    VisibilityScope,
)
from safelog import warn
from core.infra.envflags import text as _text

log = logging.getLogger(__name__)

_lock = threading.RLock()
_gateway: "MemoryGateway | None" = None

_TRACE_LOCK = threading.RLock()
_TRACE_PATH = None

# ---------------------------------------------------------------- gate (批1)
# Auto-recall must clear a relevance bar before entering the prompt, and an
# empty recall on a question becomes negative knowledge instead of silence.
# The switch defaults to off; turning it off restores legacy behaviour.
_GATE_DEFAULT_MIN_SCORE = 0.45
_QUESTION_HINTS = (
    "?", "？", "还记得", "记不记得", "记得吗", "知不知道", "知道吗",
    "是谁", "哪个", "哪儿", "哪里", "什么时候", "多少", "有没有", "是不是",
    "吗",
)


def memory_gate_enabled() -> bool:
    try:
        from core import features

        return bool(features.enabled(features.MEMORY_GATE, False))
    except Exception:
        warn("core/terminal/memory_gateway.py:59 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def memory_gate_min_score() -> float:
    import os

    raw = _text("YINYUE_MEMORY_GATE_MIN_SCORE")
    try:
        return max(0.0, min(1.5, float(raw))) if raw else _GATE_DEFAULT_MIN_SCORE
    except Exception:
        warn("core/terminal/memory_gateway.py:69 Exception", err=repr(__import__("sys").exc_info()[1]))
        return _GATE_DEFAULT_MIN_SCORE


def _gate_hits(hits: Sequence["MemoryHit"]) -> list["MemoryHit"]:
    """Drop hits below the relevance bar (no-op when the gate is off)."""
    if not memory_gate_enabled():
        return list(hits)
    limit = memory_gate_min_score()
    return [hit for hit in hits if float(hit.score or 0.0) >= limit]


def looks_like_question(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return any(hint in value for hint in _QUESTION_HINTS)


def negative_knowledge_block() -> str:
    """What she is told when a question has no recall at all."""
    return (
        "[事件记忆·无]\n"
        "- 我这边没有查到与当前消息相关的记录；"
        "如果对方问的是过去的事，直接说「我这边没查到／记不清」，不要编。"
    )


# ------------------------------------------------- 索引模式（2026-10-23 主人拍板）
# 记忆「推」进提示词时只给索引，正文让模型自己按 key 取（工具 ``read_memory``）。
# 为什么改：一次 recall 6 条正文最多 1200 字，而这一轮真正用得上的通常只有一条；
# 更麻烦的是「她到底去看过那条记忆没有」在日志里无从判断。改成索引后，取值是一次
# 显式动作，看得见、也能被审计。
MEMORY_INDEX_ENV = "YINYUE_MEMORY_INDEX"
INDEX_HEADER = "[事件记忆·索引]"
INDEX_HINT = "（这里只给索引：要看某条全文就调 read_memory(keys=\"m1\")，多条用逗号）"
INDEX_PREVIEW_CHARS = 36
INDEX_MAX_ITEMS = 6


def memory_index_enabled() -> bool:
    """索引模式默认开；写 ``YINYUE_MEMORY_INDEX=off`` 回退到整段正文注入。"""
    import os

    raw = (os.environ.get(MEMORY_INDEX_ENV) or "").strip().lower()
    if not raw:
        return True
    return raw not in {"0", "off", "false", "no", "n", "关", "关掉"}


def _preview(text: str, *, limit: int = INDEX_PREVIEW_CHARS) -> str:
    """索引行里那句开头：抹掉 @ 提及与换行，截断给省略号。

    这里**不解析归属**（不判断这条消息在跟谁说话），只是把「@某人」这类纯噪声从展示里
    去掉——所以刻意不用正则去拆 ``@``，避免在仓库里多出第二个「自己解析归属」的点
    （``tests/test_message_attribution.py`` 把那种点当守卫对象）。
    """
    words = [word for word in str(text or "").split() if not word.startswith("@")]
    value = " ".join(words).strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def index_entries(hits: Sequence["MemoryHit"], *, max_items: int = INDEX_MAX_ITEMS) -> list[dict[str, Any]]:
    """recall 结果 → 「给模型看的 key + 给人复算的原文」。

    原文全留在 ``entries`` 里（只进 ``extra``，不进提示词），这样工具取正文不需要
    再查一次库：同一轮里两边看到的必然是同一份东西。
    """
    entries: list[dict[str, Any]] = []
    for hit in list(hits)[:max_items]:
        content = str(getattr(hit, "content", "") or "").strip()
        if not content:
            continue
        occurred = getattr(hit, "occurred_at", None)
        version = str(getattr(hit, "projection_version", "") or (getattr(hit, "metadata", None) or {}).get("projection_version", ""))
        metadata = getattr(hit, "metadata", None) or {}
        entries.append(
            {
                "key": "m%d" % (len(entries) + 1),
                "memory_id": str(getattr(hit, "memory_id", "") or ""),
                "content": content,
                "occurred_at": occurred.isoformat() if occurred else "",
                "speaker": str(getattr(hit, "speaker", "") or ""),
                "event_type": str(getattr(hit, "event_type", "") or ""),
                "source_event_ids": [str(x) for x in (getattr(hit, "source_event_ids", ()) or ())],
                "metadata": {
                    key: metadata[key]
                    for key in (
                        "belief_key",
                        "subject_key",
                        "predicate",
                        "conflict",
                        "evidence_state",
                    )
                    if key in metadata
                },
                "preview": _preview(content),
                "source_label": MemoryGateway._source_label(hit),
                **({"projection_version": version} if version else {}),
                **({"belief_as_of": (getattr(hit, "metadata", None) or {}).get("belief_as_of", "")} if str(getattr(hit, "memory_id", "")).startswith("belief:") else {}),
            }
        )
    return entries


def _stamp_of(value: str) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001 - 时间坏了不该影响取记忆
        warn("core/terminal/memory_gateway.py:163 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def render_index(
    entries: Sequence[dict[str, Any]],
    *,
    header: str = INDEX_HEADER,
    hint: str = INDEX_HINT,
) -> str:
    """索引块：每行 ``- m1 [10-19 20:14] 主人: 开头一句…``。"""
    if not entries:
        return ""
    lines = [header, hint]
    for entry in entries:
        stamp = _stamp_of(str(entry.get("occurred_at") or ""))
        where = "[%s] " % stamp if stamp else ""
        who = str(entry.get("speaker") or "")
        prefix = "%s: " % who if who else ""
        label = str(entry.get("source_label") or "")
        prefix = (label + " " if label else "") + prefix
        lines.append("- %s %s%s%s" % (entry.get("key"), where, prefix, entry.get("preview")))
    return "\n".join(lines)


def _trace_recall(principal: Any, *, query: str, hits: list) -> None:
    """Append one recall record per call (best-effort, bounded to 2k lines).

    The correction-learning worker uses it to resolve "which memories were in
    play when she said the sentence I am quoting".
    """
    try:
        if not hits:
            return
        global _TRACE_PATH
        import json as _json
        from pathlib import Path as _Path

        if _TRACE_PATH is None:
            import os as _os

            override = (
                _text("YINYUE_MEMORY_RECALL_TRACE_PATH")
            ).strip()
            if override:
                _TRACE_PATH = _Path(override)
            else:
                from core.config.settings import config as _config

                _TRACE_PATH = _Path(_config.paths.log_dir) / "memory_recall_trace.jsonl"
        record = {
            "at": datetime.now().isoformat(),
            "chat_id": str(getattr(principal, "chat_id", "") or ""),
            "platform": str(getattr(principal, "platform", "") or ""),
            "bot_account_id": str(getattr(principal, "bot_account_id", "") or ""),
            "query": str(query or "")[:200],
            "hits": [
                {
                    "memory_id": str(getattr(hit, "memory_id", "") or ""),
                    "score": float(getattr(hit, "score", 0.0) or 0.0),
                }
                for hit in hits[:20]
            ],
        }
        with _TRACE_LOCK:
            with _TRACE_PATH.open("a", encoding="utf-8") as handle:
                handle.write(_json.dumps(record, ensure_ascii=False))
                handle.write("\n")
            try:
                if _TRACE_PATH.stat().st_size > 4 * 1024 * 1024:
                    lines = _TRACE_PATH.read_text(encoding="utf-8").splitlines()
                    _TRACE_PATH.write_text(
                        "\n".join(lines[-2000:]) + "\n", encoding="utf-8"
                    )
            except Exception:
                warn("core/terminal/memory_gateway.py:235 Exception", err=repr(__import__("sys").exc_info()[1]))
    except Exception:
        warn("core/terminal/memory_gateway.py:237 Exception", err=repr(__import__("sys").exc_info()[1]))


def read_recall_trace(
    *,
    chat_id: str,
    around: datetime,
    window_seconds: int = 180,
    limit: int = 3,
) -> list[dict]:
    """Read recent recall records for one chat around a timestamp."""
    try:
        global _TRACE_PATH
        from pathlib import Path as _Path

        if _TRACE_PATH is None:
            import os as _os

            override = (
                _text("YINYUE_MEMORY_RECALL_TRACE_PATH")
            ).strip()
            if override:
                _TRACE_PATH = _Path(override)
            else:
                from core.config.settings import config as _config

                _TRACE_PATH = _Path(_config.paths.log_dir) / "memory_recall_trace.jsonl"
        if not _TRACE_PATH.exists():
            return []
        moment = temporal.to_naive_local(around)
        out: list[dict] = []
        with _TRACE_LOCK:
            lines = _TRACE_PATH.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            try:
                import json as _json

                record = _json.loads(line)
            except Exception:
                warn("core/terminal/memory_gateway.py:276 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
            if str(record.get("chat_id") or "") != str(chat_id or ""):
                continue
            try:
                at = datetime.fromisoformat(str(record.get("at") or ""))
            except ValueError:
                warn("core/terminal/memory_gateway.py:282 ValueError", err=repr(__import__("sys").exc_info()[1]))
                continue
            if abs((at - moment).total_seconds()) <= max(10, int(window_seconds)):
                out.append(record)
            if len(out) >= max(1, int(limit)):
                break
        return out
    except Exception:
        warn("core/terminal/memory_gateway.py:289 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
_error: str = ""

_RECALL_EXCLUDED_TYPES = frozenset(
    {
        EventType.THOUGHT,
        EventType.EMOTION,
        EventType.TASK_UPDATE,
        EventType.SYSTEM,
        # 2026-09-30：work_item_* 是台账事件（创建/开跑/跑完/取消/改期），与
        # TASK_UPDATE 同类——内容是状态机自述（任务目标、运行结果），不是人说的话。
        # 触发它的 MESSAGE / COMMAND 事件已经在流里，再召回一条台账等于把同一件事
        # 用机器口吻重复一遍，只会挤占 prompt。
        EventType.WORK_ITEM_CREATED,
        EventType.WORK_ITEM_RUN_STARTED,
        EventType.WORK_ITEM_RUN_FINISHED,
        EventType.WORK_ITEM_CANCELLED,
        EventType.WORK_ITEM_RESCHEDULED,
    }
)
_TYPE_WEIGHT = {
    EventType.MESSAGE: 1.00,
    EventType.QUESTION: 1.05,
    EventType.REQUEST: 1.08,
    EventType.COMMAND: 1.10,
    EventType.REPLY: 1.03,
    EventType.ACTION: 0.95,
    EventType.TOOL_CALL: 0.90,
    EventType.MEMORY_WRITE: 1.12,
}


@dataclass(frozen=True)
class MemoryHit:
    """One explainable recall hit, always anchored to a source event."""

    memory_id: str
    content: str
    occurred_at: datetime
    event_type: str
    speaker: str = ""
    platform: str = ""
    bot_account_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    visibility_scope: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    score: float = 0.0
    # 来源层级（2026-09-24 批 C）：把"鲸鲸认定的事实 / 某人自述 / 某人原话"区分开，
    # 渲染层据此标 [事实] / [某人自述] / [某人说过]。默认空 = 按 memory_type 保守归类。
    is_explicit_remember: bool = False
    subject_person_ids: tuple[str, ...] = ()
    projection_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else "",
            "event_type": self.event_type,
            "speaker": self.speaker,
            "platform": self.platform,
            "bot_account_id": self.bot_account_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "visibility_scope": list(self.visibility_scope),
            "source_event_ids": list(self.source_event_ids),
            "score": self.score,
            "is_explicit_remember": self.is_explicit_remember,
            "projection_version": self.projection_version,
            "subject_person_ids": list(self.subject_person_ids),
            "metadata": dict(self.metadata),
        }
def _aware(value: datetime | None) -> datetime:
    """归一为应用时钟（naive 本地墙钟）；换算口径唯一 owner = ``temporal``。"""
    if value is None:
        return datetime.now()
    return temporal.to_naive_local(value)


def _tokens(text: str) -> set[str]:
    value = str(text or "").lower()
    tokens = set(re.findall(r"[a-z0-9_]+", value))
    for run in re.findall(r"[\u4e00-\u9fff]+", value):
        if len(run) == 1:
            tokens.add(run)
            continue
        for size in (2, 3):
            for index in range(0, len(run) - size + 1):
                tokens.add(run[index:index + size])
    return tokens


#: 同会话「近窗」的流水不回灌成长期记忆素材（2026-10-10，② 召回缺口）。
#: 实测事故：22:21 私聊「讲个笑话听一下」的召回 top3 = 21:55–21:57 同会话的
#: 「讲个笑话／我让你讲个笑话／你这个笑话谁能听懂？」——这些**已经由 [最近对话] 窗口承载**，
#: 再当"记忆素材"喂回 N1/生成，只会让这一轮围着刚发生的事打转（自指元玩笑）。
#: 口径是三重限定：**同会话 + 时间窗内 + 流水类（episode）**；
#: fact / relationship / self（跨时间的认定类）与跨会话、更早的记忆一律不动——
#: 召回的增量价值正是"窗口之外"的东西。
_RECENT_SAME_CHAT_MINUTES = 30.0
_RECENT_EPISODE_EVENT_TYPES = frozenset({
    "message", "question", "request", "command", "reply",
})


def _is_recent_same_chat_episode(
    *,
    memory_type: str,
    chat_id: str,
    occurred_at: datetime | None,
    current_chat_id: str,
    now: datetime | None = None,
) -> bool:
    """同会话 + 时间窗内 + 流水（episode）→ 不作为召回素材（窗口已经带了）。"""
    if str(memory_type or "") != "episode":
        return False
    if not chat_id or not current_chat_id or str(chat_id) != str(current_chat_id):
        return False
    if occurred_at is None:
        return False
    current = _aware(now) if now is not None else _aware(None)
    age = (current - _aware(occurred_at)).total_seconds()
    return 0.0 <= age <= _RECENT_SAME_CHAT_MINUTES * 60.0


def _recency_score(occurred_at: datetime, *, now: datetime | None = None) -> float:
    current = now or datetime.now()
    age_days = max((current - _aware(occurred_at)).total_seconds() / 86400.0, 0.0)
    return 1.0 / (1.0 + age_days / 30.0)


def _relevance_score(content: str, query: str) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    content_tokens = _tokens(content)
    if not content_tokens:
        return 0.0
    overlap = len(query_tokens & content_tokens)
    return overlap / math.sqrt(max(len(query_tokens), 1) * max(len(content_tokens), 1))


def _event_content(event: Event) -> str:
    content = str(event.content or "").strip()
    if content:
        return content
    if event.event_type in (EventType.ACTION, EventType.TOOL_CALL):
        action = str(event.action_payload.get("action") or "").strip()
        return f"执行了 {action}" if action else ""
    return ""


def _event_speaker(event: Event) -> str:
    from core.terminal.speaker import resolve_speaker

    return resolve_speaker(
        event.event_type,
        event.actor_person_id,
        metadata=event.metadata,
        actor_account_id=event.actor_account_id,
    )


class MemoryGateway:
    """Deterministic bounded recall over terminal events."""

    def __init__(
        self,
        *,
        event_store: Any = None,
        identity_service: Any = None,
        authority_service: Any = None,
        projection_repository: Any = None,
        candidate_limit: int = 400,
        vector_recaller: Any = None,
        read_binding: Any = None,
        legacy_discarded: bool = False,
    ):
        self._event_store = event_store
        self.projection_repository = projection_repository
        if read_binding is not None and projection_repository is None:
            raise ValueError("legacy_binding_requires_projection_repository")
        self.read_binding = read_binding
        self.legacy_discarded = bool(legacy_discarded)
        # Phase 5 E2: optional ``(query, limit) -> {projection_id: similarity}``
        # hook.  Defaults to the query-side vector recall (feature-gated).
        self._vector_recaller = vector_recaller
        self.identity_service = identity_service
        if authority_service is None and identity_service is not None:
            try:
                from core.terminal.authority import AuthorityService

                authority_service = AuthorityService(identity_service)
            except Exception:
                warn("core/terminal/memory_gateway.py:475 Exception", err=repr(__import__("sys").exc_info()[1]))
                authority_service = None
        self.authority_service = authority_service
        self._link_revision = -1
        try:
            from core.terminal import link_runtime
            self._link_revision = link_runtime.current_link_revision()
        except Exception as exc:
            log.warning(
                "memory_gateway_link_revision_unavailable",
                extra={"err": repr(exc)[:160]},
            )
        self.candidate_limit = max(50, int(candidate_limit or 400))

    # ------------------------------------------------------------- plumbing
    @property
    def vector_recaller(self) -> Any:
        # 2026-11-01：去向量后没有实现可取；保留属性是给「调用方自己注入」留的口子。
        return self._vector_recaller

    @property
    def event_store(self) -> Any:
        if self._event_store is None:
            try:
                from core.terminal.event_runtime import get_terminal_event_store

                self._event_store = get_terminal_event_store(force=True)
            except Exception:
                warn("core/terminal/memory_gateway.py:504 Exception", err=repr(__import__("sys").exc_info()[1]))
                self._event_store = None
        return self._event_store

    def principal_from_context(
        self,
        context: Any,
        *,
        refresh: bool = False,
    ) -> PrincipalContext | None:
        if context is None:
            return None
        try:
            from core.terminal.authority import context_principal

            principal = context_principal(context)
            if principal is not None and not refresh:
                return principal
        except Exception:
            warn("core/terminal/memory_gateway.py:522 Exception", err=repr(__import__("sys").exc_info()[1]))
            principal = None
        if self.authority_service is None:
            return principal
        try:
            from core.terminal.authority import active_context_principal

            return active_context_principal(
                context, service=self.authority_service, refresh=refresh
            )
        except Exception:
            warn("core/terminal/memory_gateway.py:532 Exception", err=repr(__import__("sys").exc_info()[1]))
            return principal

    def _refresh_link_state(self) -> bool:
        """Refresh cached identity links when the durable revision changes.

        A link revocation performed outside this process must invalidate any
        bound memory index that was authorized by the old identity graph.
        """
        try:
            from core.terminal import link_runtime

            revision = link_runtime.current_link_revision()
        except Exception:
            return False
        if revision < 0 or revision == getattr(self, "_link_revision", -1):
            return False
        identity_service = getattr(self, "identity_service", None)
        if not link_runtime.refresh_identity_service_links(identity_service):
            return False
        if identity_service is not None:
            try:
                from core.terminal.authority import AuthorityService

                self.authority_service = AuthorityService(identity_service)
            except Exception as exc:
                log.warning(
                    "memory_gateway_link_refresh_failed",
                    extra={"err": repr(exc)[:240]},
                )
        self._link_revision = revision
        return True

    @staticmethod
    def _chat_type_value(value: Any) -> str:
        return str(getattr(value, "value", value) or "")

    def _event_chat_scope_ok(self, event: Event, principal: PrincipalContext) -> bool:
        """群回合只能召回本群的群内记忆，私聊/自我记忆不得进入群 prompt。"""
        if self._chat_type_value(getattr(principal, "chat_type", "")) != "group":
            return True
        scopes = set(event.visibility_scope)
        if VisibilityScope.GROUP_MEMBERS in scopes:
            event_group = str(
                (event.metadata or {}).get("group_id") or event.chat_id or ""
            )
            return bool(principal.group_id) and event_group == str(principal.group_id)
        return bool(scopes & {VisibilityScope.PUBLIC})

    def _projection_chat_scope_ok(self, item: Any, principal: PrincipalContext) -> bool:
        if self._chat_type_value(getattr(principal, "chat_type", "")) != "group":
            return True
        scopes = {str(scope) for scope in (item.visibility_scope or ())}
        if VisibilityScope.GROUP_MEMBERS.value in scopes:
            meta = getattr(item, "metadata", None) or {}
            event_group = str(meta.get("chat_id") or meta.get("group_id") or "")
            return bool(principal.group_id) and event_group == str(principal.group_id)
        return VisibilityScope.PUBLIC.value in scopes

    def _memory_allowed(self, event: Event, principal: PrincipalContext) -> bool:
        if event.event_type in _RECALL_EXCLUDED_TYPES:
            return False
        if event.sensitivity == Sensitivity.OWNER_CHAIN_OF_THOUGHT:
            return False
        if not self._event_chat_scope_ok(event, principal):
            return False
        if principal.is_owner:
            if principal.is_group:
                _private = {
                    VisibilityScope.OWNER_ONLY,
                    VisibilityScope.ISSUER_ONLY,
                    VisibilityScope.SELF,
                }
                if set(event.visibility_scope) & _private:
                    return False
            return True
        scopes = set(event.visibility_scope)
        if VisibilityScope.PUBLIC in scopes:
            return True
        if not principal.memory_scopes:
            return False
        if scopes & set(principal.memory_scopes):
            return True
        if event.actor_person_id and event.actor_person_id == principal.person_id:
            return True
        # Bot-emitted events put the counterpart in recipient_person_ids (the
        # actor slot holds the bot), so a private recipient is matched here.
        if principal.person_id and str(principal.person_id) in {
            str(item) for item in (event.recipient_person_ids or ())
        }:
            return True
        return False

    @staticmethod
    def _component_accounts(principal: PrincipalContext) -> set[tuple[str, str]]:
        """Accounts inside the principal's current link component."""
        accounts: set[tuple[str, str]] = set()
        raw = str(principal.link_component_id or "")
        for part in raw.split("|"):
            part = part.strip()
            if ":" not in part:
                continue
            platform, account = part.split(":", 1)
            if platform and account:
                accounts.add((platform, account))
        if principal.platform and principal.bot_account_id:
            accounts.add((principal.platform, principal.bot_account_id))
        return accounts

    def _projection_allowed(self, item: Any, principal: PrincipalContext) -> bool:
        if str(getattr(item, "sensitivity", "")) == Sensitivity.OWNER_CHAIN_OF_THOUGHT.value:
            return False
        if not self._projection_chat_scope_ok(item, principal):
            return False
        if principal.is_owner:
            if principal.is_group:
                _private = {
                    VisibilityScope.OWNER_ONLY.value,
                    VisibilityScope.ISSUER_ONLY.value,
                    VisibilityScope.SELF.value,
                }
                if {str(s) for s in (item.visibility_scope or ())} & _private:
                    return False
            return True
        if not principal.memory_scopes:
            return False
        scopes = set(str(scope) for scope in (item.visibility_scope or ()))
        if VisibilityScope.PUBLIC.value in scopes:
            return True
        item_account = (
            str(getattr(item, "platform", "") or ""),
            str(getattr(item, "bot_account_id", "") or ""),
        )
        accounts = self._component_accounts(principal)
        account_ok = item_account in accounts
        if not account_ok:
            account_ok = bool(
                item.linked_component_id
                and principal.link_component_id
                and str(item.linked_component_id)
                == str(principal.link_component_id)
            )
        if not account_ok:
            return False
        scope_hit = bool(scopes & {scope.value for scope in principal.memory_scopes})
        person_hit = bool(
            principal.person_id
            and principal.person_id in (item.subject_person_ids or ())
        )
        return bool(scope_hit or person_hit)

    @staticmethod
    def _projection_version(item: Any) -> str:
        raw = json.dumps(vars(item), sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _legacy_snapshot(self):
        binding = getattr(self, "read_binding", None)
        if binding is not None:
            return binding.snapshot(self.projection_repository)
        repository = getattr(self, "projection_repository", None)
        reader = getattr(repository, "read_engine", None)
        engine = reader() if callable(reader) else getattr(repository, "engine", None)
        if engine is not None:
            from core.memory.legacy_write_freeze import assert_projection_writable
            assert_projection_writable(engine)  # 已停写的旧库必须带匹配读取权，不能因配置丢失复活。
        return frozenset()

    def _projection_current(self, item: Any, principal: PrincipalContext, now: datetime, ownership=None) -> bool:
        binding = getattr(self, "read_binding", None)
        if binding is not None and binding.owns(str(item.memory_id), self._legacy_snapshot() if ownership is None else ownership):
            return False
        if not self._projection_allowed(item, principal):
            return False
        end = getattr(item, "valid_to", None)
        return end is None or _aware(end) > _aware(now)

    def _index_entry_current(self, entry: dict, principal: PrincipalContext) -> bool:
        version = str(entry.get("projection_version") or "")
        ownership = self._legacy_snapshot()
        binding = getattr(self, "read_binding", None)
        if binding is not None and binding.owns(str(entry.get("memory_id") or ""), ownership):
            return False
        if not version:
            return True  # 只有投影索引具备此版本契约；旧流水索引不冒充已核验。
        repository = self.projection_repository
        if repository is None:
            return False
        item = repository.get(str(entry.get("memory_id") or ""))
        return item is not None and self._projection_current(item, principal, datetime.now(), ownership) and self._projection_version(item) == version

    def _projection_candidates(
        self,
        principal: PrincipalContext,
        *,
        scan_limit: int,
    ) -> list[Any]:
        repository = self.projection_repository
        if repository is None:
            return []
        try:
            if principal.is_owner:
                return repository.query_candidates(limit=scan_limit)
            return repository.query_candidates(
                limit=scan_limit,
                accounts=sorted(self._component_accounts(principal)),
                component_id=principal.link_component_id,
            )
        except Exception:
            warn("core/terminal/memory_gateway.py:668 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []

    def _recall_projections(
        self,
        principal: PrincipalContext,
        *,
        query: str,
        limit: int,
        scan_limit: int,
        now: datetime | None = None,
        vector_wait: float = 0.0,
    ) -> list[MemoryHit]:
        try:
            ownership = self._legacy_snapshot()
        except Exception as exc:
            warn("legacy_read_binding_unavailable", err=type(exc).__name__)
            return []
        # Phase 5 E2: lexically narrow first, then let the vector index widen
        # the candidate pool (paraphrases).  Best-effort: any failure keeps the
        # original lexical behaviour.
        vector_scores: dict[str, float] = {}
        recaller = self.vector_recaller
        if recaller is not None and str(query or "").strip():
            try:
                try:
                    vector_scores = dict(
                        recaller(
                            str(query),
                            max(20, int(limit) * 4),
                            wait=float(vector_wait or 0.0),
                        )
                        or {}
                    )
                except TypeError:
                    # injected test recallers take (query, limit) only
                    warn("core/terminal/memory_gateway.py:697 TypeError", err=repr(__import__("sys").exc_info()[1]))
                    vector_scores = dict(
                        recaller(str(query), max(20, int(limit) * 4)) or {}
                    )
            except Exception:
                warn("core/terminal/memory_gateway.py:702 Exception", err=repr(__import__("sys").exc_info()[1]))
                vector_scores = {}

        candidates = {
            item.memory_id: item
            for item in self._projection_candidates(principal, scan_limit=scan_limit)
        }
        if vector_scores and self.projection_repository is not None:
            getter = getattr(self.projection_repository, "get", None)
            if callable(getter):
                for memory_id in vector_scores:
                    if memory_id in candidates:
                        continue
                    try:
                        item = getter(memory_id)
                    except Exception:
                        warn("core/terminal/memory_gateway.py:717 Exception", err=repr(__import__("sys").exc_info()[1]))
                        item = None
                    if item is not None:
                        candidates[memory_id] = item

        hits: list[MemoryHit] = []
        now_utc = now or datetime.now()
        for item in candidates.values():
            if not self._projection_current(item, principal, now_utc, ownership):
                continue
            content = str(getattr(item, "content", "") or "").strip()
            if not content:
                continue
            occurred_at = _aware(getattr(item, "occurred_at", None))
            projection_type = str(
                getattr(item, "memory_type", "") or "episode"
            )
            if _is_recent_same_chat_episode(
                memory_type=projection_type,
                chat_id=str(
                    (getattr(item, "metadata", None) or {}).get("chat_id") or ""
                ),
                occurred_at=occurred_at,
                current_chat_id=str(getattr(principal, "chat_id", "") or ""),
                now=now_utc,
            ):
                continue
            type_weight = {
                "fact": 1.10,
                "episode": 1.00,
                "relationship": 1.05,
                "self": 1.00,
            }.get(projection_type, 0.90)
            score = (
                _relevance_score(content, query) * 0.65
                + _recency_score(occurred_at, now=now) * 0.25
                + float(getattr(item, "confidence", 0.5) or 0.0) * 0.05
                + type_weight * 0.05
            )
            vector_similarity = vector_scores.get(
                str(getattr(item, "memory_id", "") or "")
            )
            if vector_similarity is not None:
                score += 0.40 * float(vector_similarity)
            metadata = getattr(item, "metadata", None) or {}
            hits.append(
                MemoryHit(
                    memory_id=str(getattr(item, "memory_id", "") or ""),
                    content=content,
                    occurred_at=occurred_at,
                    event_type=str(getattr(item, "memory_type", "") or "episode"),
                    speaker=str(metadata.get("speaker") or ""),
                    platform=str(getattr(item, "platform", "") or ""),
                    bot_account_id=str(getattr(item, "bot_account_id", "") or ""),
                    chat_id=str(metadata.get("chat_id") or ""),
                    chat_type=str(metadata.get("chat_type") or ""),
                    visibility_scope=tuple(
                        str(scope) for scope in (getattr(item, "visibility_scope", ()) or ())
                    ),
                    source_event_ids=tuple(
                        str(value)
                        for value in (getattr(item, "source_event_ids", ()) or ())
                    ),
                    score=round(float(score), 4),
                    is_explicit_remember=bool(metadata.get("explicit_remember")),
                    projection_version=self._projection_version(item),
                    subject_person_ids=tuple(
                        str(value)
                        for value in (getattr(item, "subject_person_ids", ()) or ())
                    ),
                    metadata=dict(metadata),
                )
            )
        hits.sort(
            key=lambda hit: (hit.score, _aware(hit.occurred_at)),
            reverse=True,
        )
        result = _gate_hits(hits[: max(1, int(limit or 1))])
        _trace_recall(principal, query=query, hits=result)
        return result

    # --------------------------------------------------------------- recall
    def recall(
        self,
        principal: PrincipalContext,
        *,
        query: str = "",
        limit: int = 6,
        max_scan: int | None = None,
        now: datetime | None = None,
        vector_wait: float = 0.0,
    ) -> list[MemoryHit]:
        if self.legacy_discarded:
            return []
        if getattr(self, "read_binding", None) is not None and self.projection_repository is None:
            warn("legacy_read_binding_repository_missing")
            return []
        scan_limit = max(20, int(max_scan or self.candidate_limit))
        if self.projection_repository is not None:
            return self._recall_projections(
                principal,
                query=query,
                limit=limit,
                scan_limit=scan_limit,
                now=now,
                vector_wait=vector_wait,
            )
        store = self.event_store
        if store is None:
            return []
        try:
            engine = getattr(store, "engine", None)
            if engine is not None:
                from core.memory.legacy_write_freeze import assert_projection_writable
                assert_projection_writable(engine)
            candidates = store.query_visible(principal, limit=scan_limit)
        except Exception:
            warn("core/terminal/memory_gateway.py:829 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []

        hits: list[MemoryHit] = []
        for event in candidates:
            if not self._memory_allowed(event, principal):
                continue
            content = _event_content(event)
            if not content:
                continue
            event_type_value = str(
                getattr(event.event_type, "value", event.event_type) or ""
            )
            if _is_recent_same_chat_episode(
                memory_type=(
                    "episode"
                    if event_type_value in _RECENT_EPISODE_EVENT_TYPES
                    else event_type_value
                ),
                chat_id=str(getattr(event, "chat_id", "") or ""),
                occurred_at=_aware(getattr(event, "occurred_at", None)),
                current_chat_id=str(getattr(principal, "chat_id", "") or ""),
                now=now,
            ):
                continue
            score = (
                _relevance_score(content, query) * 0.65
                + _recency_score(event.occurred_at, now=now) * 0.25
                + _TYPE_WEIGHT.get(event.event_type, 0.8) * 0.10
            )
            hits.append(
                MemoryHit(
                    memory_id=event.event_id,
                    content=content,
                    occurred_at=_aware(event.occurred_at),
                    event_type=event.event_type.value,
                    speaker=_event_speaker(event),
                    platform=event.platform,
                    bot_account_id=event.bot_account_id,
                    chat_id=event.chat_id,
                    chat_type=event.chat_type,
                    visibility_scope=tuple(
                        sorted(scope.value for scope in event.visibility_scope)
                    ),
                    source_event_ids=(event.event_id,),
                    score=round(float(score), 4),
                    metadata=dict(getattr(event, "metadata", None) or {}),
                )
            )
        hits.sort(
            key=lambda hit: (hit.score, _aware(hit.occurred_at)),
            reverse=True,
        )
        return _gate_hits(hits[: max(1, int(limit or 1))])

    # --------------------------------------------------------------- render
    @staticmethod
    def _source_label(hit: "MemoryHit") -> str:
        """把一条命中的来源层级映射成可读标签（2026-09-24 批 C）。

        目标：让模型明确知道「说过」只是「说过」，不是「事实」——
        用「某人说过 / 某人自述 / 事实」把原话和事实物理切开，避免把流水当真。
        """
        meta = getattr(hit, "metadata", None) or {}
        kind = str(getattr(hit, "kind", "") or "")
        if kind == "belief":
            label = "[事实版本]"
        elif kind == "knowledge":
            label = "[抓取资料·未核实]"
        elif kind == "meme":
            label = "[梗]·网络词典" if meta.get("source") == "web" else "[梗]·本群"
        else:
            explicit = bool(getattr(hit, "is_explicit_remember", False) or meta.get("explicit_remember"))
            mtype = str(getattr(hit, "event_type", "") or meta.get("memory_type") or "").strip().lower()
            if explicit or mtype == "memory_write":
                label = "[记忆摘要·未核实]"
            elif kind == "personal" and not getattr(hit, "source_event_ids", ()):
                label = "[未核实旧记录]"
            elif mtype == "self":
                label = "[我说过]"
            elif mtype in {"relationship", "fact"}:
                label = "[某人自述]"
            else:
                label = "[某人说过]"
        state = str(meta.get("evidence_state") or "")
        if state == "dead" and "依据已失效" not in label:
            label += "·依据已失效"
        elif state == "unknown" and "未核实" not in label:
            label += "·依据未核实"
        elif state == "unverified" and "未核实" not in label:
            label += "·没有可核实来源"
        return label

    @staticmethod
    def render(
        hits: Sequence[MemoryHit],
        *,
        max_chars: int = 1200,
        header: str = "[事件记忆·仅作当前对话参考]",
        source_label: str = "",
    ) -> str:
        if not hits:
            return ""
        lines: list[str] = [header]
        used = len(header)
        seen: set[str] = set()
        for hit in hits:
            raw = str(hit.content or "").strip()
            if not raw:
                continue
            # 批B：撤回系统 XML 不原样进 prompt（曾以「unknown: <?xml…」出现在
            # [事件记忆] 里）；渲染成一行系统通知。
            notice = ""
            try:
                from core.context.chat_window import revoke_notice_text

                notice = revoke_notice_text(raw).replace("（系统）", "", 1)
            except Exception:  # noqa: BLE001 - 渲染失败按原文走
                warn("core/terminal/memory_gateway.py:932 Exception", err=repr(__import__("sys").exc_info()[1]))
                notice = ""
            if notice:
                stamp = _aware(hit.occurred_at).strftime("%m-%d %H:%M")
                line = f"- [系统] [{stamp}] {notice}"
                if used + len(line) + 1 > max_chars:
                    break
                lines.append(line)
                used += len(line) + 1
                continue
            # 2026-09-22：过滤纯 @/表情/标点噪声（"@鲸鲸" 曾占满 6 条）并去重。
            stripped = re.sub(r"@\S+", " ", raw)
            stripped = stripped.strip(
                " \t\r\n，。！？!?~～、:：;；[]【】()（）\"'"
            )
            if re.fullmatch(
                r"\[(表情包|图片|视频|语音|文件|链接|位置)[^\]]*\]", raw
            ):
                continue
            if len(stripped) < 3 or stripped in seen:
                continue
            seen.add(stripped)
            label = str(source_label) if source_label else MemoryGateway._source_label(hit)
            prefix = f"{hit.speaker}: " if hit.speaker else ""
            # 2026-10-16：没有本体事件时间的条目不渲染时间戳
            # （_aware(None) 会用当前墙钟补，把 web 梗库误示成“今天刚说过”）。
            has_time = hit.occurred_at is not None
            if has_time:
                stamp = _aware(hit.occurred_at).strftime("%m-%d %H:%M")
                line = f"- {label} [{stamp}] {prefix}{raw}"
            else:
                line = f"- {label} {prefix}{raw}"
            if used + len(line) + 1 > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines) if len(lines) > 1 else ""


    def attach_to_context(
        self,
        context: Any,
        *,
        query: str = "",
        limit: int = 6,
        max_chars: int = 1200,
        refresh_principal: bool = False,
        vector_wait: float = 0.0,
    ) -> str:
        if context is None:
            return ""
        extra = getattr(context, "extra", None)
        if not isinstance(extra, dict):
            return ""
        from core.memory.index_scope import bind_index, bound_index, scope_key
        if memory_index_enabled() and extra.get("terminal_memory_attached"):
            if self._refresh_link_state():
                refresh_principal = True
        cached = (
            bound_index(context)
            if memory_index_enabled() and extra.get("terminal_memory_attached")
            else None
        )
        if cached is not None and not refresh_principal:
            if cached.is_current(context):
                return str(extra.get("terminal_memory_block") or "")
            refresh_principal = True
        if extra.get("terminal_memory_attached") and bound_index(context) is None:
            refresh_principal = True
        principal = self.principal_from_context(
            context, refresh=refresh_principal
        )
        if principal is None or scope_key(context, principal) is None:
            context.memory_index_snapshot = None
            extra["terminal_memory_attached"] = False
            extra["terminal_memory_block"] = ""
            return ""
        hits = self.recall(
            principal, query=query, limit=limit, vector_wait=vector_wait
        )
        entries = index_entries(hits) if memory_index_enabled() else []
        block = render_index(entries) if entries else self.render(hits, max_chars=max_chars)
        negative = False
        if not hits and memory_gate_enabled() and looks_like_question(query):
            block = negative_knowledge_block()
            negative = True
        extra["terminal_memory_attached"] = True
        extra["terminal_memory_block"] = block
        extra["terminal_memory_count"] = len(hits)
        extra["terminal_memory_negative"] = negative
        extra["terminal_memory_sources"] = [
            source
            for hit in hits
            for source in hit.source_event_ids
        ]
        # 正文只存在于绑定身份的 snapshot 里；不再往 extra 留一份未绑定的副本。
        bind_index(context, entries, principal, validator=lambda entry: self._index_entry_current(entry, principal))
        return block



# --------------------------------------------------------------------------
# Runtime cache / convenience integration
# --------------------------------------------------------------------------
def install_memory_gateway(gateway: MemoryGateway | None) -> None:
    global _gateway, _error
    with _lock:
        _gateway = gateway
        _error = ""


def reset_memory_gateway() -> None:
    global _gateway, _error
    with _lock:
        _gateway = None
        _error = ""




def legacy_projections_discarded() -> bool:
    """主人整体弃置旧投影库后，召回不再读取这些行。"""
    return _text("YINYUE_LEGACY_PROJECTIONS_DISCARDED").strip().lower() in {"1", "true", "yes", "on"}


def get_memory_gateway(*, force: bool = False) -> MemoryGateway | None:
    global _gateway, _error
    with _lock:
        if _gateway is not None:
            return _gateway
        if _error:
            return None
        try:
            from core.terminal.event_runtime import get_event_identity_service

            identity_service = get_event_identity_service()
            from core.terminal.store_port import get_meta_repo
            from db.repo_memory_projections import MemoryProjectionRepository

            projection_repository = MemoryProjectionRepository(get_meta_repo().engine)
            read_binding = None
            binding_path = _text("YINYUE_LEGACY_READ_BINDING").strip()
            if binding_path:
                from core.memory.legacy_read_binding import load_read_binding
                read_binding = load_read_binding(binding_path)
                read_binding.snapshot(projection_repository)
            else:
                from core.memory.legacy_write_freeze import assert_projection_writable
                assert_projection_writable(projection_repository.engine)
            _gateway = MemoryGateway(
                identity_service=identity_service,
                projection_repository=projection_repository,
                read_binding=read_binding,
                legacy_discarded=legacy_projections_discarded(),
            )
            _error = ""
        except Exception as exc:  # noqa: BLE001 - recall is best effort
            warn("core/terminal/memory_gateway.py:1079 Exception", err=repr(exc))
            _error = str(exc)[:240]
            return None
        return _gateway


def attach_terminal_memory(
    context: Any,
    *,
    query: str = "",
    limit: int = 6,
    max_chars: int = 1200,
    gateway: MemoryGateway | None = None,
    vector_wait: float = 0.35,
) -> str:
    """Integration hook for the conversational prompt path.

    Returns the rendered block (possibly empty).  ``gateway`` is injectable for
    tests; otherwise the lazy feature-flagged gateway is used.
    """
    if context is None:
        return ""
    extra = getattr(context, "extra", None)
    if not isinstance(extra, dict):
        return ""
    if gateway is None:
        gateway = get_memory_gateway()
    if gateway is None:
        return ""
    if not query:
        query = str(
            extra.get("user_message")
            or extra.get("inbound_text")
            or ""
        )
    if vector_wait and vector_wait > 0:
        try:
            import os as _os

            raw = (_text("YINYUE_MEMORY_VECTOR_WAIT")).strip()
            if raw:
                vector_wait = max(0.0, min(2.0, float(raw)))
        except Exception:
            warn("core/terminal/memory_gateway.py:1121 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        return gateway.attach_to_context(
            context,
            query=query,
            limit=limit,
            max_chars=max_chars,
            vector_wait=vector_wait,
        )
    except Exception:
        warn("core/terminal/memory_gateway.py:1131 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
