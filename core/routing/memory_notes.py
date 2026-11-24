"""[R3] 便签落地：判这一轮给出的 0~3 条候选便签 -> 候选区。

为什么写在这里、不写在判里：

* 判只产读数，**不写库**（与「开事 / 问批准」同一条纪律：写库在出口一侧）；
* 证据要用「这一回合窗口里真正渲染过的行」，那是路由手上才有的东西；
* 三档开关与候选区同一套口径（``core.quality.memory_proposals``）。

判只说「该记什么 + 窗口里哪句原话」：让模型写 ``event_id`` 它会抄错，抄一句人话不会。
这里拿 ``quote`` 去窗口行里逐行比对，命中才换成真实 ``event_id``；一条也命中不了就不落
——宁缺毋滥，证据为空的条目在 ``memory_proposals._validate_item`` 里本来也会被丢弃。
"""

from __future__ import annotations

from typing import Any, Iterable
from safelog import warn

#: 窗口行里可能放正文的键（不同构造点用过的名字）。
_TEXT_KEYS = ("content", "text", "body", "message", "line")


def _item_of(row: Any) -> dict:
    if isinstance(row, dict):
        return row
    return dict(getattr(row, "__dict__", {}) or {})


def event_ids_of(rows: Iterable[Any]) -> list[str]:
    """窗口行里的 event_id（去重、保序、跳过空串）。"""
    out: list[str] = []
    for row in rows or ():
        eid = str(_item_of(row).get("event_id") or "")
        if eid and eid not in out:
            out.append(eid)
    return out


def _text_of(row: Any) -> str:
    item = _item_of(row)
    for key in _TEXT_KEYS:
        value = item.get(key)
        if value:
            return str(value)
    return ""


def quote_evidence(quote: str, rows: Iterable[Any]) -> list[str]:
    """哪几行的正文里真的出现了这句原话（返回这些行的 event_id）。"""
    text = str(quote or "").strip()
    if len(text) < 4:
        return []
    hits: list[str] = []
    for row in rows or ():
        if text in _text_of(row):
            eid = str(_item_of(row).get("event_id") or "")
            if eid and eid not in hits:
                hits.append(eid)
    return hits[:6]


def notes_to_items(notes: Iterable[Any], rows: Iterable[Any]) -> tuple[list[dict], int]:
    """便签 -> ``memory_proposals`` 的条目形状；返回（条目, 找不到证据的条数）。"""
    items: list[dict] = []
    unverified = 0
    for note in notes or ():
        if isinstance(note, dict):
            slot = str(note.get("slot") or "")
            content = str(note.get("content") or "")
            subject = str(note.get("subject") or "")
            quote = str(note.get("quote") or "")
        else:
            slot = str(getattr(note, "slot", "") or "")
            content = str(getattr(note, "content", "") or "")
            subject = str(getattr(note, "subject", "") or "")
            quote = str(getattr(note, "quote", "") or "")
        evidence = quote_evidence(quote, rows)
        if not evidence:
            unverified += 1
            continue
        items.append(
            {
                "slot": slot,
                "content": content,
                "subject": subject,
                "reason": "llm",
                "evidence": evidence,
                "evidence_text": quote,
            }
        )
    return items, unverified


def stage_notes(result: Any, *, chat_id: str, rows: Iterable[Any] = (), log: Any = None) -> dict:
    """把 ``result.memory`` 送进候选区（off 直接返回；shadow 只记账）。"""
    notes = tuple(getattr(result, "memory", ()) or ())
    if not notes:
        return {"notes": 0, "staged": 0, "mode": ""}
    from core.quality import memory_proposals as _mp

    mode = _mp.memory_notes_mode()
    if mode == "off":
        return {"notes": len(notes), "staged": 0, "mode": "off"}
    # 这个 chat 的「人物便签」根本没开 -> 批了也不会进 prompt（person_notes 只在
    # 开关打开且 chat 在名单里时渲染），那就别往候选区堆（那是没人会看的队列）。
    try:
        from core.context.person_notes import notes_enabled_for

        if not notes_enabled_for(str(chat_id or "")):
            if log is not None:
                log.info(
                    "memory_notes_skipped",
                    chat_id=str(chat_id or ""),
                    reason="chat_not_enabled",
                    notes=len(notes),
                )
            return {"notes": len(notes), "staged": 0, "mode": mode, "reason": "chat_not_enabled"}
    except Exception:  # noqa: BLE001 - 查不到开关就当开（宁可多一条候选，也不静默丢）
        warn("core/routing/memory_notes.py:117 Exception", err=repr(__import__("sys").exc_info()[1]))
    items, unverified = notes_to_items(notes, rows)
    outcome = _mp.stage_from_turn(
        str(chat_id or ""),
        items,
        valid_ids=set(event_ids_of(rows)),
        dry_run=(mode == "shadow"),
    )
    event = "memory_notes_shadow" if mode == "shadow" else "memory_notes_staged"
    if log is not None:
        log.info(
            event,
            chat_id=str(chat_id or ""),
            notes=len(notes),
            items=len(items),
            unverified=unverified,
            staged=int(outcome.get("staged") or 0),
        )
    return {
        "notes": len(notes),
        "items": len(items),
        "unverified": unverified,
        "staged": int(outcome.get("staged") or 0),
        "mode": mode,
    }
