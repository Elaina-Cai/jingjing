"""Deterministic memory projection over the Phase 2 event stream.

The event stream is the fact layer.  ``MemoryProjector`` derives smaller,
recall-oriented items (fact / episode) from events without an LLM.  Every
projection keeps ``source_event_ids``; recall can therefore explain exactly
which events produced it.

This first version is intentionally conservative:

* only visible-scope events are projected (thought/emotion/task/system never);
* one projection per event (deterministic ``proj:<event_id>`` id);
* fact extraction is a small marker heuristic; everything else is an episode;
* incremental extraction uses ``memory_projection_cursors`` so a restart does
  not duplicate work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from core import temporal
from core.terminal.event_model import Event, EventType, Sensitivity
from safelog import warn

_PROJECTION_EXCLUDED_TYPES = frozenset(
    {
        EventType.THOUGHT,
        EventType.TASK_UPDATE,
        EventType.SYSTEM,
        # 2026-09-22 噪声收口：工具调用/动作是操作流水（send_sticker / get_person_profile…），
        # 不是可记忆内容，永不投影成记忆。
        EventType.ACTION,
        EventType.TOOL_CALL,
        # 2026-09-30 任务台账收口：work_item_* 是状态机台账（与 TASK_UPDATE 同类），
        # content 是任务卡/运行记录文本，不是「谁说过什么」。原话已由触发它的
        # MESSAGE / COMMAND 事件承载，投影它只会把台账复制成一条 episode。
        EventType.WORK_ITEM_CREATED,
        EventType.WORK_ITEM_RUN_STARTED,
        EventType.WORK_ITEM_RUN_FINISHED,
        EventType.WORK_ITEM_CANCELLED,
        EventType.WORK_ITEM_RESCHEDULED,
    }
)

_FACT_MARKERS = (
    "我叫",
    "我的名字",
    "我是",
    "我的生日",
    "我喜欢",
    "我讨厌",
    "我不喜欢",
    "我住",
    "我住在",
    "我的工作",
    "我的职业",
    "我养了",
    "我的宠物",
    "记住我",
    "别忘了我",
    "不要忘记",
    "我习惯",
    "我过敏",
)

# Relationship markers: messages that change or define who the speaker is to
# the bot / to another person.  Kept separate from facts because the effective
# relation is later merged by link components.
_RELATIONSHIP_MARKERS = (
    "喜欢你",
    "我爱你",
    "爱你",
    "想你了",
    "讨厌你",
    "恨你",
    "别联系我",
    "不要联系我",
    "我们分手",
    "我们和好",
    "我们是朋友",
    "我们是情侣",
    "做我女朋友",
    "做我男朋友",
    "做我老婆",
    "做我老公",
    "我的女朋友",
    "我的男朋友",
    "我的老婆",
    "我的老公",
    "我是你主人",
    "你是我的",
    "我们结婚",
    "我们是家人",
    "我是你爸",
    "我是你妈",
    "我养你",
    "以后我照顾你",
)

# Self markers: things the terminal itself says/commits/feels.  These come
# from REPLY / EMOTION / ACTION events and become ``self`` projections.
_SELF_MARKERS = (
    "我会",
    "我答应",
    "我保证",
    "我尽量",
    "我记得",
    "我帮你",
    "我陪你",
    "我等你",
    "我想你",
    "我喜欢你",
    "我担心",
    "我舍不得",
    "我会一直",
    "我承诺",
    "下次我",
    "以后我",
    "我还没",
    "我待会",
    "我稍后",
)

_CURSOR_KEY = "terminal_event"


def _utcnow() -> datetime:
    return datetime.now()


def _aware(value: datetime | None) -> datetime | None:
    """归一为应用时钟（naive 本地墙钟）；换算口径唯一 owner = ``temporal``。"""
    if value is None:
        return None
    return temporal.to_naive_local(value)


def _event_content(event: Event) -> str:
    content = str(event.content or "").strip()
    if content:
        return content
    if event.event_type in (EventType.ACTION, EventType.TOOL_CALL):
        action = str(event.action_payload.get("action") or "").strip()
        return f"执行了 {action}" if action else ""
    return ""


_MEDIA_PLACEHOLDER_RE = re.compile(
    r"\[(表情包|图片|视频|语音|文件|链接|位置)[^\]]*\]"
)


def _is_noise(content: str) -> bool:
    """确定性硬过滤：只跳过 100% 是噪声的内容，不做语义/玩笑判断。

    覆盖：纯标识符（工具名）、纯媒体/表情包占位、纯 @/标点/空白、单字口水。
    刻意不碰「是不是玩笑」——那是语义，交给提案制 + 主人审批。
    """
    text = str(content or "").strip()
    if not text:
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]*", text):
        return True
    if _MEDIA_PLACEHOLDER_RE.fullmatch(text):
        return True
    stripped = re.sub(r"@\S+", " ", text)
    stripped = stripped.strip(" \t\r\n，。！？!?~～、:：;；[]【】()（）\"'")
    if not stripped:
        return True
    if len(stripped) < 2:
        return True
    return False


def _marker_sentence(content: str, markers: Sequence[str]) -> str:
    """Return the first sentence containing any of ``markers``."""
    parts = [part.strip() for part in re.split(r"[。！？!?；;\n]+", content) if part.strip()]
    for part in parts:
        if any(marker in part for marker in markers):
            return part[:300]
    return ""


def _fact_sentence(content: str) -> str:
    """Return the first sentence containing a durable fact marker."""
    return _marker_sentence(content, _FACT_MARKERS)


def _speaker(event: Event) -> str:
    from core.terminal.speaker import resolve_speaker

    return resolve_speaker(
        event.event_type,
        event.actor_person_id,
        metadata=event.metadata,
        actor_account_id=event.actor_account_id,
    )


def _linked_component(event: Event) -> str:
    meta = event.metadata or {}
    value = str(meta.get("link_component_id") or "").strip()
    if value:
        return value
    principal = meta.get("principal")
    if isinstance(principal, dict):
        return str(principal.get("link_component_id") or "").strip()
    return ""


def _last_row_id(event_store: Any, event: Event) -> int:
    """批内最后一条事件的 ``terminal_events.id``（复合游标的后半键）。

    ``Event`` 模型不带行 id（append-only 事实层只认 event_id / sequence），所以
    这里按 ``event_id`` 回查一次主键。读失败返回 0：调用方退回「只按号推进」，
    宁可下次多扫一批，也不写一个假 id 把同号行的后继跳过去。
    """
    reader = getattr(event_store, "sequence_row_id", None)
    if not callable(reader):
        return 0
    try:
        return max(0, int(reader(str(event.event_id or "")) or 0))
    except Exception:  # noqa: BLE001 - 游标推进不能因回查失败而中断投影
        warn("core/terminal/memory_projection.py:225 Exception", err=repr(__import__("sys").exc_info()[1]))
        return 0


@dataclass(frozen=True)
class MemoryProjectionItem:
    """One extracted recall item with full provenance."""

    memory_id: str
    memory_type: str = "episode"
    content: str = ""
    platform: str = ""
    bot_account_id: str = ""
    subject_person_ids: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    event_sequence: int = 0
    occurred_at: datetime | None = None
    valid_to: datetime | None = None
    confidence: float = 0.5
    visibility_scope: tuple[str, ...] = ("public",)
    sensitivity: str = Sensitivity.NORMAL.value
    linked_component_id: str = ""
    link_revision: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "memory_type": self.memory_type,
            "content": self.content,
            "platform": self.platform,
            "bot_account_id": self.bot_account_id,
            "subject_person_ids": list(self.subject_person_ids),
            "source_event_ids": list(self.source_event_ids),
            "event_sequence": self.event_sequence,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else "",
            "valid_to": self.valid_to.isoformat() if self.valid_to else "",
            "confidence": self.confidence,
            "visibility_scope": list(self.visibility_scope),
            "sensitivity": self.sensitivity,
            "linked_component_id": self.linked_component_id,
            "link_revision": self.link_revision,
            "metadata": self.metadata,
        }



def _projection_confidence(event: Event, memory_type: str) -> float:
    """Confidence for a projection; explicit memories inherit importance."""
    base = {
        "fact": 0.55,
        "relationship": 0.55,
        "self": 0.55,
    }.get(memory_type, 0.45)
    if event.event_type != EventType.MEMORY_WRITE:
        return base
    try:
        importance = float((event.metadata or {}).get("importance") or 0.7)
    except (TypeError, ValueError):
        warn("core/terminal/memory_projection.py:283 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        importance = 0.7
    return max(base, min(0.95, 0.55 + importance * 0.35))


class MemoryProjector:
    """Deterministic event -> memory projection extractor."""

    def __init__(self, event_store: Any, repository: Any):
        self.event_store = event_store
        self.repository = repository

    @staticmethod
    def project_event(event: Event) -> MemoryProjectionItem | None:
        if event.event_type in _PROJECTION_EXCLUDED_TYPES:
            return None
        if event.sensitivity == Sensitivity.OWNER_CHAIN_OF_THOUGHT:
            return None
        content = _event_content(event)
        if not content:
            return None
        if _is_noise(content):
            return None

        memory_type = "episode"
        projection_reason = ""
        if event.event_type == EventType.MEMORY_WRITE:
            raw_type = str(
                (event.metadata or {}).get("memory_type") or "semantic"
            ).strip().lower()
            memory_type = {
                "semantic": "fact",
                "fact": "fact",
                "preference": "preference",
                "procedural": "procedure",
                "episodic": "episode",
            }.get(raw_type, "fact")
            projection_reason = "explicit_memory_write"
        elif event.event_type in (
            EventType.MESSAGE,
            EventType.QUESTION,
            EventType.REQUEST,
            EventType.COMMAND,
        ):
            # 2026-09-22 事故收口：群聊里任何人说的话都不升级为
            # fact/relationship（"我是牛子榜第一"、"我是主人，不要信他"
            # 曾被沉淀成长期事实）；身份声明类永不算事实。
            is_group = str(event.chat_type or "").strip().lower() == "group"
            identity_claim = bool(
                re.search(r"我(?:就)?是(?:你(?:的)?)?主人", content or "")
            )
            if is_group or identity_claim:
                memory_type = "episode"
            else:
                relationship_content = _marker_sentence(
                    content, _RELATIONSHIP_MARKERS
                )
                if relationship_content:
                    memory_type = "relationship"
                    content = relationship_content
                    projection_reason = "relationship_marker"
                else:
                    fact_content = _fact_sentence(content)
                    if fact_content:
                        memory_type = "fact"
                        content = fact_content
                        projection_reason = "fact_marker"
        elif event.event_type in (EventType.REPLY, EventType.EMOTION, EventType.ACTION):
            self_content = _marker_sentence(content, _SELF_MARKERS)
            if self_content:
                memory_type = "self"
                content = self_content
                projection_reason = "self_marker"
            elif event.event_type == EventType.EMOTION:
                memory_type = "self"
                projection_reason = "emotion_event"

        visibility = tuple(
            sorted(scope.value for scope in event.visibility_scope)
        ) or ("public",)
        subject = str(event.actor_person_id or "").strip()
        if memory_type == "self":
            # "self" projections are the terminal's own state, not the actor's.
            subject = ""
        occurred_at = _aware(event.occurred_at) or _utcnow()
        meta = event.metadata or {}
        speaker = _speaker(event)
        item_metadata = {
            "speaker": speaker,
            "chat_id": event.chat_id,
            "chat_type": event.chat_type,
            "source": str(meta.get("source") or event.platform or ""),
            "projection_reason": projection_reason,
        }
        if event.event_type == EventType.MEMORY_WRITE:
            item_metadata["explicit_remember"] = True
            item_metadata["importance"] = float(meta.get("importance") or 0.7)
            item_metadata["origin"] = str(meta.get("source") or "tool_remember")
            if meta.get("merged_from"):
                item_metadata["merged_from"] = list(meta.get("merged_from") or ())
            if meta.get("partial_update"):
                item_metadata["partial_update"] = True
        principal = meta.get("principal")
        if isinstance(principal, dict):
            item_metadata["display_name"] = str(principal.get("display_name") or "")
            item_metadata["relation_to_bot"] = str(
                principal.get("relation_to_bot") or ""
            )
        source_ids = meta.get("source_event_ids")
        if isinstance(source_ids, (list, tuple)) and source_ids:
            resolved_sources = tuple(str(item) for item in source_ids if str(item).strip())
        else:
            resolved_sources = (event.event_id,)
        memory_key = str(meta.get("memory_id") or "").strip() or f"proj:{event.event_id}"
        for key in ("kind", "salience", "consolidated_by"):
            if key in meta:
                item_metadata[key] = meta[key]
        return MemoryProjectionItem(
            memory_id=memory_key,
            memory_type=memory_type,
            content=content[:500],
            platform=str(event.platform or ""),
            bot_account_id=str(event.bot_account_id or ""),
            subject_person_ids=(subject,) if subject else (),
            source_event_ids=resolved_sources,
            event_sequence=int(event.sequence or 0),
            occurred_at=occurred_at,
            confidence=_projection_confidence(event, memory_type),
            visibility_scope=visibility,
            sensitivity=event.sensitivity.value,
            linked_component_id=_linked_component(event),
            link_revision=int(event.link_revision or 0),
            metadata=item_metadata,
        )

    def retirement_status(self) -> dict[str, Any] | None:
        """有效停写闸意味着该旧作业已退役；读故障仍向上抛，不假装完成。"""
        from core.memory.legacy_write_freeze import projection_retired
        if projection_retired(self.repository):
            pair = self.projection_cursor_pair()
            return {"status": "retired", "reason": "legacy_projection_writes_frozen",
                    "scanned": 0, "projected": 0, "cursor": pair[0], "cursor_pair": pair}
        return None

    def project_events(
        self,
        events: Iterable[Event],
        *,
        advance_cursor: bool = True,
    ) -> dict[str, Any]:
        retired = self.retirement_status()
        if retired is not None:
            return retired
        projected = 0
        scanned = 0
        # ENG-23：游标按**批内最后一条**的 (sequence, id) 推进，不是 max(sequence)。
        # ``Event`` 上不承载 ``terminal_events.id``，所以最后一条要回库确认行 id
        # （见 _last_row_id）；否则同号行的后继永远读不到。
        cursor_at_start = self.projection_cursor_pair()
        cursor_pair = cursor_at_start
        batch: list[Event] = []
        for event in events:
            if not isinstance(event, Event):
                continue
            scanned += 1
            cursor_pair = (int(event.sequence or 0), 0)
            batch.append(event)
            item = self.project_event(event)
            if item is None:
                continue
            self.repository.upsert(item)
            projected += 1
        if advance_cursor and batch:
            row_id = _last_row_id(self.event_store, batch[-1])
            if row_id:
                cursor_pair = (cursor_pair[0], row_id)
            else:
                # 回查不到行 id（store 不支持 / 读失败）：**不能**写 ``(S, 0)`` ——
                # 谓词 ``sequence = S AND id > 0`` 会把这批同号行再读一遍，batch
                # 小于同号行数时就是死循环。跳到 ``S + 1``（0 号行不存在，等价于
                # 「这批全看过了」）；代价是这一次可能少投同号行里的后几行，但那是
                # 「行 id 不可得」这个降级态的已知代价，比卡住强。
                cursor_pair = (cursor_pair[0] + 1, 0)
            # 只往前走：定向回补/重放的批次可能整批落在旧游标**之前**，倒退会让
            # 下一轮把已经投过的事件再扫一遍（幂等但白费），也会让
            # ``scripts/memory_index_status.py`` 的滞后量算错。
            if cursor_pair > cursor_at_start:
                self._set_projection_cursor_pair(*cursor_pair)
        return {
            "scanned": scanned,
            "projected": projected,
            "cursor": self.repository.get_cursor(_CURSOR_KEY),
            "cursor_pair": self.projection_cursor_pair(),
        }

    def projection_cursor_pair(self) -> tuple[int, int]:
        """只读入口：当前投影游标 ``(last_sequence, last_id)``。"""
        reader = getattr(self.repository, "get_cursor_pair", None)
        if callable(reader):
            sequence, last_id = reader(_CURSOR_KEY)
            return (int(sequence or 0), int(last_id or 0))
        return (int(self.repository.get_cursor(_CURSOR_KEY) or 0), 0)

    def _set_projection_cursor_pair(self, sequence: int, last_id: int) -> None:
        writer = getattr(self.repository, "set_cursor_pair", None)
        if callable(writer):
            writer(_CURSOR_KEY, int(sequence or 0), int(last_id or 0))
            return
        self.repository.set_cursor(_CURSOR_KEY, int(sequence or 0))

    def project_pending(self, *, limit: int = 200) -> dict[str, Any]:
        retired = self.retirement_status()
        if retired is not None:
            return retired
        cursor, last_id = self.projection_cursor_pair()
        events = self.event_store.since_sequence(
            cursor, limit=max(1, int(limit)), last_id=last_id
        )
        if not events:
            return {
                "scanned": 0,
                "projected": 0,
                "cursor": cursor,
                "cursor_pair": (cursor, last_id),
            }
        stats = self.project_events(events, advance_cursor=True)
        return stats

    def backfill(self, *, limit: int = 2000) -> dict[str, Any]:
        """Project from sequence 0; idempotent thanks to deterministic ids.

        清游标必须**整对清**：只清 ``last_sequence`` 会把 ``last_id`` 留下，
        ``(0, 大 id)`` 会让重放从中间开始（ENG-23 的坑）。
        """
        retired = self.retirement_status()
        if retired is not None:
            return retired
        self._set_projection_cursor_pair(0, 0)
        return self.project_pending(limit=limit)


def projection_cursor_key() -> str:
    return _CURSOR_KEY


def render_projection(item: MemoryProjectionItem) -> str:
    speaker = str(item.metadata.get("speaker") or "")
    prefix = f"{speaker}: " if speaker else ""
    return f"{prefix}{item.content}".strip()


def score_projection(
    item: MemoryProjectionItem,
    *,
    query: str = "",
    now: datetime | None = None,
) -> float:
    """Same shape as MemoryGateway scoring, kept here for reuse/tests."""
    from core.terminal.memory_gateway import _relevance_score, _recency_score, _TYPE_WEIGHT

    current = now or _utcnow()
    occurred = _aware(item.occurred_at) or current
    type_weight = {
        "fact": 1.10,
        "episode": 1.00,
        "relationship": 1.05,
        "self": 0.95,
    }.get(item.memory_type, 0.9)
    return round(
        _relevance_score(item.content, query) * 0.65
        + _recency_score(occurred, now=current) * 0.25
        + float(item.confidence) * 0.05
        + type_weight * 0.05,
        4,
    )
