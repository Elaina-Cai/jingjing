# -*- coding: utf-8 -*-
"""⑩ 数据通道：⑧猜证的本地证据查询（批C Step C2）。

设计图口径：⑧假说 ──本地证据查询──▶ ⑩工具包 ──▶ ⑨数据层
（成员 / 备注 / 关系 / wxid）。⑩ 只做**纯通道**：只读、不写、不猜、
不调 LLM、不做业务判断；数据 owner 仍是 ⑨（people/identities/
memberships、contacts、relationships）。读不到 → 空列表；异常 → 空列表
（绝不造证据）。

* ``read_evidence``：按查询词取回 :class:`EvidenceHit` 列表
  （person / identity / membership / contact / relationship，稳定顺序，
  ``limit`` 截断）。
* ``evidence_digest``：审计 / 回放用的紧凑 JSON 载荷（不放大字段）。

"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from safelog import warn

__all__ = [
    "EVIDENCE_KINDS",
    "EvidenceHit",
    "evidence_digest",
    "read_evidence",
]

#: 证据种类及其稳定输出顺序（person → identity → membership → note → contact → relationship）。
EVIDENCE_KINDS = ("person", "identity", "membership", "note", "contact", "relationship",)

_WXID_LIKE = ("wxid", "external_id", "wechat_id")
_MAX_RELATION_CANDIDATES = 10
_NOTE_TEXT_MAX = 120


def _text(value: Any) -> str:
    return str(value or "").strip()


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        warn("core/execution/evidence_channel.py:45 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return fallback


def _call(obj: Any, name: str, *args: Any, **kwargs: Any) -> list[Any]:
    """安全调用 repo 方法：缺失 / 异常 / None 全部收敛成空列表。"""
    try:
        fn = getattr(obj, name, None)
        if fn is None:
            return []
        out = fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - 通道读不到就当没有
        warn("core/execution/evidence_channel.py:56 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
    if out is None:
        return []
    if isinstance(out, (list, tuple, set)):
        return list(out)
    return [out]


def _safe_list(fn: Any, *args: Any, **kwargs: Any) -> list[Any]:
    """安全调用独立读取函数（person_notes 等）：异常/None 收敛成空列表。"""
    if fn is None:
        return []
    try:
        out = fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - 读不到就当没有
        warn("core/execution/evidence_channel.py:71 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
    if out is None:
        return []
    if isinstance(out, (list, tuple, set)):
        return list(out)
    return [out]


def _default_notes_reader() -> Any:
    """⑨ 逐人卡备注（owner_confirmed 的成员备注）读取口。"""
    try:
        from core.context.person_notes import list_notes
    except Exception:  # noqa: BLE001 - 起不来就是读不到
        warn("core/execution/evidence_channel.py:84 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    return list_notes


def _default_repo() -> Any:
    """生产默认数据源：优先当前账号库（备注/关系），否则共享 meta 库（人/成员）。"""
    try:
        from core.bootstrap import get_account_repo

        return get_account_repo()
    except Exception:  # noqa: BLE001 - 无账号上下文（后台 tick）走 meta 库
        warn("core/execution/evidence_channel.py:95 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        from core.terminal.store_port import get_meta_repo

        return get_meta_repo()
    except Exception:  # noqa: BLE001 - 起不来就是读不到
        warn("core/execution/evidence_channel.py:101 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


@dataclass(frozen=True)
class EvidenceHit:
    """一条本地证据（只读视图；不判断真假、不给结论）。"""

    kind: str
    value: str = ""
    label: str = ""
    person_id: str = ""
    chat_id: str = ""
    identity_type: str = ""
    confidence: float = 0.0
    source: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "label": self.label,
            "person_id": self.person_id,
            "chat_id": self.chat_id,
            "identity_type": self.identity_type,
            "confidence": round(_num(self.confidence), 3),
            "source": self.source,
        }


def evidence_digest(hits: list[EvidenceHit] | tuple[EvidenceHit, ...], *, limit: int = 3) -> list[dict[str, Any]]:
    """审计 / 回放用紧凑载荷：只留可核对字段，不含 detail 大字段。"""
    cap = max(0, int(limit))
    return [hit.to_dict() for hit in list(hits)[:cap]]


def _dedup(hits: list[EvidenceHit]) -> list[EvidenceHit]:
    seen: dict[tuple[str, str, str, str], None] = {}
    out: list[EvidenceHit] = []
    for hit in hits:
        key = (hit.kind, hit.value, hit.person_id, hit.chat_id)
        if key in seen:
            continue
        seen[key] = None
        out.append(hit)
    return out


def _person_hits(repo: Any, query: str, *, chat_id: str, platform: str, account_id: str, cap: int) -> tuple[list[EvidenceHit], list[str]]:
    out: list[EvidenceHit] = []
    people = getattr(repo, "people", None)
    if people is None:
        return out, []
    for person in _call(people, "find_persons", query, platform=platform, account_id=account_id, limit=cap):
        person_id = _text(getattr(person, "person_id", ""))
        display = _text(getattr(person, "display_name", ""))
        if not person_id and not display:
            continue
        out.append(EvidenceHit(
            kind="person",
            value=display,
            label=display,
            person_id=person_id,
            confidence=1.0,
            source="person_entities",
        ))
        if not person_id:
            continue
        for identity in _call(people, "list_identities", person_id):
            value = _text(getattr(identity, "identity_value", ""))
            if not value:
                continue
            out.append(EvidenceHit(
                kind="identity",
                value=value,
                label=value,
                person_id=person_id,
                chat_id=_text(getattr(identity, "chat_id", "")),
                identity_type=_text(getattr(identity, "identity_type", "")),
                confidence=_num(getattr(identity, "confidence", 0.0)),
                source=_text(getattr(identity, "source", "")) or "person_identities",
            ))
        for membership in _call(people, "list_memberships", person_id, chat_id=chat_id):
            nickname = _text(getattr(membership, "group_nickname", ""))
            out.append(EvidenceHit(
                kind="membership",
                value=nickname or display,
                label=nickname or display,
                person_id=person_id,
                chat_id=_text(getattr(membership, "chat_id", "")),
                confidence=1.0,
                source="group_memberships",
                detail={
                    "role": _text(getattr(membership, "role", "")),
                    "message_count": int(_num(getattr(membership, "message_count", 0))),
                },
            ))
    return out, []


def _contact_hits(repo: Any, query: str, *, cap: int) -> tuple[list[EvidenceHit], list[str]]:
    out: list[EvidenceHit] = []
    wxids: list[str] = []
    contacts = getattr(repo, "contacts", None)
    if contacts is None:
        return out, wxids
    for contact in _call(contacts, "search_by_name", query, limit=cap):
        wxid = _text(getattr(contact, "wxid", ""))
        nickname = _text(getattr(contact, "nickname", ""))
        remark = _text(getattr(contact, "remark", ""))
        if not wxid and not nickname and not remark:
            continue
        if wxid:
            wxids.append(wxid)
        out.append(EvidenceHit(
            kind="contact",
            value=wxid or remark or nickname,
            label=remark or nickname or wxid,
            confidence=1.0,
            source="contacts",
            detail={"nickname": nickname, "remark": remark},
        ))
    return out, wxids


def _note_hits(reader: Any, query: str, *, chat_id: str, cap: int) -> list[EvidenceHit]:
    """逐人卡备注：内容或 person_key 命中查询词（只读、有界）。"""
    out: list[EvidenceHit] = []
    if reader is None:
        return out
    needle = query.lower()
    for row in _safe_list(reader, chat_id=chat_id)[: max(cap * 3, cap)]:
        if isinstance(row, dict):
            get = row.get
        else:
            get = lambda name, default="": getattr(row, name, default)  # noqa: E731
        content = _text(get("content", ""))
        person_key = _text(get("person_key", ""))
        if not content and not person_key:
            continue
        if needle not in content.lower() and needle not in person_key.lower():
            continue
        out.append(EvidenceHit(
            kind="note",
            value=content[:_NOTE_TEXT_MAX],
            label=person_key or "群备注",
            chat_id=_text(get("chat_id", "")),
            confidence=1.0,
            source=_text(get("source", "")) or "person_notes",
            detail={"person_key": person_key},
        ))
    return out


def _relationship_hits(repo: Any, candidates: list[str], *, persona_id: int, cap: int) -> list[EvidenceHit]:
    out: list[EvidenceHit] = []
    relationships = getattr(repo, "relationships", None)
    if relationships is None:
        return out
    seen: dict[str, None] = {}
    for target in candidates:
        if not target or target in seen:
            continue
        seen[target] = None
        if len(seen) > _MAX_RELATION_CANDIDATES:
            break
        for row in _call(relationships, "get", persona_id, target):
            notes = _text(getattr(row, "notes", ""))
            out.append(EvidenceHit(
                kind="relationship",
                value=target,
                label=_text(getattr(row, "relation_type", "")) or "relationship",
                confidence=_num(getattr(row, "intimacy", 0.0)),
                source="relationships",
                detail={
                    "relation_type": _text(getattr(row, "relation_type", "")),
                    "notes": notes[:80],
                },
            ))
    return out


def read_evidence(
    repo: Any = None,
    *,
    query: str,
    chat_id: str = "",
    platform: str = "",
    account_id: str = "",
    persona_id: int = 1,
    limit: int = 20,
    notes_reader: Any = None,
) -> list[EvidenceHit]:
    """本地证据查询：⑧ 假说词 → ⑩ 纯读通道 → ⑨ 数据层。

    ``repo`` 缺省时先取当前账号库、再退共享 meta 库；两处都没有就返回空。
    ``limit`` 同时约束每个数据源的单次查询量与最终返回条数。
    ``notes_reader``（逐人卡备注）默认只在 ``repo`` 缺省的生产路径启用；
    显式传 ``repo``（注入/测试）时不自动读备注，除非显式传 ``notes_reader``。
    """
    text = _text(query)
    if not text:
        return []
    cap = max(1, int(limit or 1))
    active = repo if repo is not None else _default_repo()
    if active is None:
        return []
    by_kind: dict[str, list[EvidenceHit]] = {kind: [] for kind in EVIDENCE_KINDS}
    person_hits, _unused = _person_hits(active, text, chat_id=chat_id, platform=platform, account_id=account_id, cap=cap)
    by_kind["person"] = [hit for hit in person_hits if hit.kind == "person"]
    by_kind["identity"] = [hit for hit in person_hits if hit.kind == "identity"]
    by_kind["membership"] = [hit for hit in person_hits if hit.kind == "membership"]
    active_notes = notes_reader
    if active_notes is None and repo is None:
        active_notes = _default_notes_reader()
    by_kind["note"] = _note_hits(active_notes, text, chat_id=chat_id, cap=cap)
    contact_hits, wxids = _contact_hits(active, text, cap=cap)
    by_kind["contact"] = contact_hits
    candidates = list(wxids)
    for hit in by_kind["identity"]:
        if hit.identity_type in _WXID_LIKE or hit.value.startswith("wxid_"):
            candidates.append(hit.value)
    by_kind["relationship"] = _relationship_hits(active, candidates, persona_id=persona_id, cap=cap)
    ordered: list[EvidenceHit] = []
    for kind in EVIDENCE_KINDS:
        ordered.extend(by_kind[kind])
    return _dedup(ordered)[:cap]
