# -*- coding: utf-8 -*-
"""场景读场事实层（v2 施工图 Step 2）。

只做一件事：把一条消息的**显式归属事实**整理成 :class:`SceneFacts`。
所有判定都走现有 canonical owner，本模块**不写** @ / 引用 / 轻拍的正则。

边界：

* 纯函数、无 IO、无 LLM；
* 给不出的事实留空（空元组 / 空串），绝不回落到"最近说话人"；
* `roles` 后验属于 Step 3，不在这里猜。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from core.conversation.addressing import (
    mentioned_names,
    named_targets,
    pat_targets,
    quote_of,
)
from safelog import warn
from core.infra.envflags import text

__all__ = [
    "SceneFacts",
    "ScenePosterior",
    "SceneRead",
    "build_scene_facts",
    "facts_hash",
    "SCENE_READ_SCHEMA",
    "SCENE_READ_SUBJECTS",
    "OBSERVATION_SOURCES",
    "normalize_about",
    "about_subject",
    "about_her",
    "her_relevance",
    "scene_mode",
    "scene_question_enabled",
    "scene_shadow_enabled",
    "MEMORY_KINDS",
    "shadow_audit",
    "social_chat_scope",
]


@dataclass(frozen=True)
class SceneFacts:
    """一条消息的显式参与事实；没证据 = 空。"""

    explicit_addressee: tuple[str, ...] = ()
    mentioned: tuple[str, ...] = ()
    quote_author: str = ""
    quote_unresolved: bool = False
    at_targets: tuple[str, ...] = ()
    pat_targets: tuple[str, ...] = ()
    reply_to: str = ""
    parent_event_id: str = ""
    unit_members: tuple[str, ...] = ()
    unit_summary: str = ""


@dataclass(frozen=True)
class ScenePosterior:
    """语义后验占位（Step 3 由 N1 一次调用填充）。"""

    roles: Mapping[str, float] = field(default_factory=dict)
    mentioned_her: bool = False
    target: str = ""
    genre: str = "unknown"
    owner_state: str = "unknown"
    thread_topic: str = ""


@dataclass(frozen=True)
class SceneRead:
    facts: SceneFacts
    posterior: ScenePosterior = field(default_factory=ScenePosterior)


def _value(row: Any, *names: str, default: Any = "") -> Any:
    if row is None:
        return default
    if isinstance(row, Mapping):
        for name in names:
            if name in row and row[name] is not None:
                return row[name]
        return default
    for name in names:
        value = getattr(row, name, None)
        if value is not None:
            return value
    return default


def _content(row: Any) -> str:
    return str(_value(row, "text", "content", default="") or "")


def _refs(row: Any) -> Mapping[str, Any]:
    refs = _value(row, "refs", default=None)
    return refs if isinstance(refs, Mapping) else {}


def _reply_ref(row: Any) -> tuple[str, str]:
    refs = _refs(row)
    parent_event_id = str(
        refs.get("parent_event_id")
        or refs.get("parent_message_id")
        or _value(row, "parent_event_id", "parent_message_id", default="")
        or ""
    )
    reply_to = str(
        refs.get("reply_to")
        or refs.get("reply_to_event_id")
        or ""
    )
    return reply_to, parent_event_id


def build_scene_facts(
    current: Any,
    window_rows: Sequence[Any] = (),
    thread_result: Any = None,
    *,
    self_names: Sequence[str] = (),
) -> SceneFacts:
    """把当前行整理成 :class:`SceneFacts`。

    ``current`` / ``window_rows`` 可以是评测 dict（``speaker``/``text``/``refs``），
    也可以是带 ``speaker``/``content``/``msg_id`` 的生产行对象。
    ``thread_result`` 传 N3 的 ``FocusResult``（只有 ``topic`` 被读，供 unit_summary）。
    """
    text = _content(current)
    quoted = quote_of(text)
    quote_author = quoted.author_name if quoted.resolvable else ""
    quote_unresolved = bool(quoted.present and not quoted.resolvable)

    at_targets = tuple(mentioned_names(text))
    pat = tuple(pat_targets(text, self_names))
    mentioned = tuple(named_targets(text))

    explicit: list[str] = []
    for name in (quote_author, *at_targets, *pat):
        if name and name not in explicit:
            explicit.append(name)

    reply_to, parent_event_id = _reply_ref(current)

    unit_members: tuple[str, ...] = ()
    unit_summary = ""
    if thread_result is not None:
        unit_summary = str(_value(thread_result, "topic", default="") or "")
        members = _value(thread_result, "members", default=None)
        if members:
            unit_members = tuple(str(item) for item in members)

    return SceneFacts(
        explicit_addressee=tuple(explicit),
        mentioned=mentioned,
        quote_author=quote_author,
        quote_unresolved=quote_unresolved,
        at_targets=at_targets,
        pat_targets=pat,
        reply_to=reply_to,
        parent_event_id=parent_event_id,
        unit_members=unit_members,
        unit_summary=unit_summary,
    )


SCENE_READ_SCHEMA = 2
SCENE_READ_SUBJECTS = ("speaker", "other", "her", "room", "unknown")
OBSERVATION_SOURCES = ("facts", "model", "rule")
#: 按需记忆的 kind 闭集（按需记忆与梗设计 v1）：结果条目自带 kind 标签。
MEMORY_KINDS = ("fact", "episode", "person", "meme", "quote")

# 第 (3) 问的答案（2026-10-16 甲）：她手上有没有对得上号的事实。
MEMORY_STATES = ("have", "partial", "missing", "unknown")


def normalize_about(section: Mapping[str, Any] | None) -> dict[str, Any]:
    """scene_read.about -> {subject, her, confidence, evidence}；兼容旧字符串 about。

    契约（2026-10-15 schema v2）：about 是对象，subject 五值闭集；her 三态；
    confidence 0..1；evidence 最多 3 条。旧字符串 about 按老口径归一到 subject。
    """
    if not isinstance(section, Mapping):
        return {"subject": "unknown", "her": None, "confidence": 0.0, "evidence": ()}
    about = section.get("about")
    if isinstance(about, str):
        token = " ".join(str(about).split()).casefold()
        if token == "self":
            token = "speaker"
        if token in SCENE_READ_SUBJECTS:
            subject = token
        elif not token:
            subject = "unknown"
        else:
            subject = "other"  # 人名 = 另一个具体的人
        return {"subject": subject, "her": None, "confidence": 0.0, "evidence": ()}
    if not isinstance(about, Mapping):
        return {"subject": "unknown", "her": None, "confidence": 0.0, "evidence": ()}
    subject = str(about.get("subject") or "unknown").casefold()
    if subject not in SCENE_READ_SUBJECTS:
        subject = "unknown"
    her = about.get("her")
    if not isinstance(her, bool):
        her = None
    try:
        conf = float(about.get("confidence") or 0.0)
    except Exception:
        warn("core/context/scene_read.py:214 Exception", err=repr(__import__("sys").exc_info()[1]))
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    ev = about.get("evidence") or ()
    evidence = tuple(str(item)[:80] for item in ev if str(item).strip()) if isinstance(ev, (list, tuple)) else ()
    return {"subject": subject, "her": her, "confidence": conf, "evidence": evidence[:3]}


def about_subject(section: Any) -> str:
    """scene_read -> about.subject（五值闭集）。"""
    return str(normalize_about(section)["subject"])


def about_her(section: Any) -> bool | None:
    """scene_read -> about.her（三态）。"""
    return normalize_about(section)["her"]


def her_relevance(section: Any, *, summon_her: bool = False) -> str:
    """这轮是否需要她：her / not_her / unknown（纯派生，不猜）。

    被召唤（调用方给事实）或 subject=her 或 her=True -> her；
    her=False 或 subject in (speaker, other) -> not_her；其余 unknown。
    """
    ab = normalize_about(section)
    if summon_her or ab["subject"] == "her" or ab["her"] is True:
        return "her"
    if ab["her"] is False or ab["subject"] in ("speaker", "other"):
        return "not_her"
    return "unknown"


# ------------------------------------------------------------------ Step 5：shadow 接线

def scene_mode() -> str:
    """GROUP_SCENE 三态：off / shadow / on；默认 off。"""
    try:
        from core import features

        return features.mode(features.GROUP_SCENE, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到按 off，不阻断
        warn("core/context/scene_read.py:254 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def social_chat_scope() -> tuple[str, ...]:
    """YINYUE_GROUP_SOCIAL_CHATS 逗号分隔的作用域；空 = 不限制。"""
    raw = text("YINYUE_GROUP_SOCIAL_CHATS")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def scene_question_enabled(chat_id: Any, is_group: Any) -> bool:
    """N1 prompt 是否追加 scene_read 问句：非 off + 群聊 + 作用域内。"""
    if not is_group:
        return False
    if scene_mode() == "off":
        return False
    scope = social_chat_scope()
    if scope and str(chat_id or "") not in scope:
        return False
    return True


def scene_shadow_enabled(chat_id: Any, is_group: Any) -> bool:
    """shadow/on 且群聊且在作用域内才启用。"""
    if not is_group:
        return False
    if scene_mode() == "off":
        return False
    scope = social_chat_scope()
    if scope and str(chat_id or "") not in scope:
        return False
    return True


def facts_hash(*parts: Any) -> str:
    """审计用指纹（sha1 前 12 位）；不落原文。"""
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def shadow_audit(data: Any, section: Any) -> dict[str, Any]:
    """把 scene_read 段整理成可落审计的字段（不含模型原文）。

    2026-10-13 补 ``present`` / ``problems``：此前只有 ``ok``，把"模型没给段"
    和"段给了但不合法"混成同一个 0，收数时无法定位（当天实查 60 条里 45 条
    ``ok=false``，其中一半其实是多 JSON 片段解析失败，不是 scene 段的问题）。
    """
    present = isinstance(section, Mapping)
    ok = bool(present and section.get("ok"))
    roles = dict(section.get("roles") or {}) if ok else {}
    updates = list(section.get("updates") or ()) if ok else []
    _ma = section.get("needs_ask") if ok else None
    _ma_question = str(_ma.get("question") or "") if isinstance(_ma, Mapping) else ""
    _ma_subject = str(_ma.get("subject") or "") if isinstance(_ma, Mapping) else ""
    _ma_state = str(section.get("memory_state") or "") if ok else ""
    _ab = normalize_about(section) if ok else {}
    _ab_subject = str(_ab.get("subject") or "")
    _ab_her = _ab.get("her")
    _nm = section.get("needs_memory") if ok else None
    _nm_query = str(_nm.get("query") or "") if isinstance(_nm, Mapping) else ""
    _nm_kinds = [str(item or "") for item in (_nm.get("kinds") or ())] if isinstance(_nm, Mapping) else []
    speakers = sorted({
        str(item.get("speaker") or "").strip()
        for item in updates
        if str(item.get("speaker") or "").strip()
    })
    problems: list[str] = []
    if present and not ok:
        raw_problems = section.get("problems") or ()
        if isinstance(raw_problems, (str, bytes)):
            raw_problems = (raw_problems,)
        try:
            problems = [str(item) for item in raw_problems]
        except TypeError:
            warn("core/context/scene_read.py:327 TypeError", err=repr(__import__("sys").exc_info()[1]))
            problems = []
    return {
        "chat_id": str(getattr(data, "chat_id", "") or ""),
        "event_id": str(
            getattr(data, "current_event_id", "")
            or getattr(data, "event_id", "")
            or ""
        ),
        "present": present,
        "ok": ok,
        "problems": problems,
        "roles": roles,
        "targets": list(section.get("targets") or ()) if ok else [],
        # 谁说的（Step 16）：只记条数与说话人名字，**不记更新正文**。
        "updates_n": len(updates),
        "speakers": speakers,
        # 按需记忆（2026-10-15）：只记 query 与 kinds，不记检索结果（结果进 trace 的 recall）。
        "needs_memory": bool(_nm_query),
        "needs_memory_query": _nm_query[:80],
        "needs_memory_kinds": _nm_kinds,
        "mentioned_her": bool(section.get("mentioned_her")) if ok else False,
        "genre": str(section.get("genre") or "") if ok else "",
        "owner_state": str(section.get("owner_state") or "") if ok else "",
        "about": str(section.get("about") or "") if ok else "",
        # 甲接线（2026-10-16）：把 about 的读数摊平成可统计字段，而不是整段 str()。
        "about_subject": _ab_subject,
        "about_her": _ab_her,
        "memory_state": _ma_state,
        "needs_ask": bool(_ma_question),
        "needs_ask_question": _ma_question[:80],
        "needs_ask_subject": _ma_subject[:40],
        "thread_continue": section.get("thread_continue") if ok else None,
        "takeover_allowed": section.get("takeover_allowed") if ok else None,
        "facts_hash": facts_hash(
            getattr(data, "chat_id", ""),
            getattr(data, "sender_name", ""),
            getattr(data, "current", ""),
            roles,
        ),
    }
