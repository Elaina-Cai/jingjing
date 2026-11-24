"""P4 event-sourced appraisal contract.

An appraisal is not a global happiness/sentiment score.  It is one
inspectable evaluation tied to source events, goals and values, stored through
``cognitive_commit`` (the single cognitive write path).  The same experience
replayed with the same trigger refs is idempotent; revising an appraisal must
name the current head event via ``supersedes``.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

from core.context import cognitive_commit as cc
from core.infra.warn_guard import once as warn_once
from core.terminal.event_model import EventType
from safelog import warn

SCHEMA = "appraisal/v1"

# 下一步的可执行方向。tendency 保存自然语言意图，next_move 让同一拍的行动也能被约束。
NEXT_MOVES: dict[str, str] = {
    "reply": "自然回应",
    "clarify": "先问清关键事实",
    "act": "先完成必要动作",
    "wait": "这一拍先收住或等待",
    "comfort": "先接住情绪，慢点给方案",
}


@dataclass(frozen=True)
class Appraisal:
    event_id: str
    subject_key: str
    trigger_refs: tuple[str, ...]
    goal_refs: tuple[str, ...]
    value_refs: tuple[str, ...]
    meaning: str
    feeling_text: str = ""
    tendency: str = ""
    next_move: str = ""
    unresolved_condition: str = ""
    review_at: str = ""
    revision: int = 0
    topic_name: str = ""
    goal_occurrences: tuple[tuple[str, int], ...] = ()
    action_refs: tuple[str, ...] = ()
    quoted_source_ref: str = ""
    quoted_reply_ref: str = ""


def _refs(values: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(str(v).strip() for v in (values or ()) if str(v).strip())


def goal_receipt_subject(goal_id: str, occurrence: int) -> str:
    return f"appraisal:goal-receipt:{occurrence}:{goal_id}"


def _occurrence(subject_key: str, trigger_refs: tuple[str, ...], supersedes: str) -> str:
    raw = "\x00".join([subject_key, *sorted(trigger_refs), str(supersedes or "")])
    return "appraise:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def record_appraisal(
    conn: Any,
    *,
    scope: cc.Scope,
    subject_key: str,
    trigger_refs: Iterable[Any],
    meaning: str,
    topic_name: str = "",
    quoted_source_ref: str = "",
    quoted_reply_ref: str = "",
    goal_refs: Iterable[Any] = (),
    goal_occurrences: tuple[tuple[str, int], ...] = (),
    action_refs: Iterable[Any] = (),
    value_refs: Iterable[Any] = (),
    feeling_text: str = "",
    tendency: str = "",
    next_move: str = "",
    unresolved_condition: str = "",
    review_at: str = "",
    supersedes: str = "",
    occurrence: str = "",
    expected_revision: int | None = None,
    actor_key: str = "",
    source: str = "appraisal",
) -> cc.CommitResult:
    """Record one evidence-bound appraisal.

    ``trigger_refs`` are source event ids and must be non-empty.  A replay with
    the same subject/triggers/supersedes resolves to the same event id and is
    reported as a duplicate, never accumulated twice.  To change an appraisal,
    pass the current event id as ``supersedes``.
    """
    subject = str(subject_key or "").strip()
    triggers = _refs(trigger_refs)
    if not subject:
        raise ValueError("subject_key_required")
    if not triggers:
        raise ValueError("appraisal_trigger_required")
    if not str(meaning or "").strip():
        raise ValueError("appraisal_meaning_required")
    direction = str(next_move or "").strip()
    if direction and direction not in NEXT_MOVES:
        raise ValueError("appraisal_next_move_invalid")

    occ = str(occurrence or "").strip() or _occurrence(subject, triggers, str(supersedes or ""))
    # A replay of an already committed revision must reach the idempotency
    # branch, not fail the "supersedes must be current" check (the head has
    # moved on since that revision was written).
    replay_event_id = cc.event_id_for(scope, occ, subject, "appraise")
    replay = conn.execute(
        "SELECT payload FROM cognitive_events WHERE event_id=?", (replay_event_id,)
    ).fetchone()

    expected: dict[str, int] | None = {subject: int(expected_revision)} if expected_revision is not None else None
    quoted_reply_ref = str(quoted_reply_ref or "").strip()
    if replay is not None:
        stored = json.loads(replay[0])
        quoted_source_ref = str(stored.get("quoted_source_ref") or "")
        quoted_reply_ref = str(stored.get("quoted_reply_ref") or "")
    elif supersedes:
        prior = cc.read_state(conn, subject, scope=scope)
        if (
            prior is None
            or str(prior.get("event_id") or "") != str(supersedes)
            or str(prior.get("operation") or "") != "appraise"
        ):
            return cc.CommitResult(rejected=(("", "supersedes_not_current"),))
        expected = {subject: int(prior.get("revision") or 0)}
        quoted_source_ref = quoted_source_ref or str((prior.get("payload") or {}).get("quoted_source_ref") or "")
    payload = {
        "schema": SCHEMA,
        "trigger_refs": list(triggers),
        "goal_refs": list(_refs(goal_refs)),
        "action_refs": list(_refs(action_refs)),
        "value_refs": list(_refs(value_refs)),
        "meaning": str(meaning).strip(),
        "feeling_text": str(feeling_text or "").strip(),
        "tendency": str(tendency or "").strip(),
        "next_move": direction,
        "unresolved_condition": str(unresolved_condition or "").strip(),
        "review_at": str(review_at or "").strip(),
        "supersedes": str(supersedes or ""),
    }
    if topic_name:
        payload["topic_name"] = str(topic_name)
    if quoted_source_ref:
        payload["quoted_source_ref"] = str(quoted_source_ref)
    if quoted_reply_ref:
        payload["quoted_reply_ref"] = quoted_reply_ref
    if goal_occurrences:
        payload["goal_occurrences"] = dict(goal_occurrences)
    evidence_refs = triggers + ((quoted_reply_ref,) if quoted_reply_ref else ())
    proposal = cc.Proposal(
        proposal_id=occ,
        subject_key=subject,
        operation="appraise",
        payload=payload,
        evidence_refs=evidence_refs,
        occurrence=occ,
        supersedes=str(supersedes or ""),
        scope=scope,
    )
    context = cc.CommitContext(
        scope=scope,
        visible_event_ids=frozenset(evidence_refs),
        allowed_subjects=frozenset({subject}),
        require_evidence=True,
        source=str(source or "appraisal"),
        actor_key=str(actor_key or ""),
    )
    return cc.commit_updates(
        conn,
        context=context,
        proposals=(proposal,),
        expected_revisions=expected,
        idempotency_key="appraisal:" + subject,
    )


def current_appraisal(conn: Any, *, subject_key: str, scope: cc.Scope | None = None) -> Appraisal | None:
    state = cc.read_state(conn, str(subject_key or ""), scope=scope)
    if not state or str(state.get("operation") or "") != "appraise":
        return None
    payload = state.get("payload") or {}
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return None
    return _build_appraisal(str(state.get("event_id") or ""), str(subject_key), payload,
                            int(state.get("revision") or 0))


def _clip_at_boundary(value: str, cap: int) -> str:
    """Trim to a readable phrase: prefer a sentence break over a dangling half-clause."""
    text = " ".join(str(value or "").split())
    limit = max(1, int(cap))
    if len(text) <= limit:
        return text
    window = text[:limit]
    for mark in ("\u3002", "\uff01", "\uff1f", "\uff1b"):
        cut = window.rfind(mark)
        if cut >= 12:
            return window[: cut + 1]
    cut = window.rfind("\uff0c")
    if cut >= max(12, limit // 2):
        return window[: cut + 1]
    return window.rstrip() + "\u2026"


def render_appraisal(appraisal: Appraisal | None, *, max_chars: int = 160) -> str:
    if appraisal is None:
        return ""
    parts: list[str] = []
    if appraisal.meaning:
        parts.append("我的理解：" + appraisal.meaning)
    if appraisal.feeling_text:
        parts.append("我的情绪：" + appraisal.feeling_text)
    if appraisal.next_move in NEXT_MOVES:
        parts.append("此刻的方向：" + NEXT_MOVES[appraisal.next_move])
    if appraisal.tendency:
        parts.append("我的意图：" + appraisal.tendency)
    if appraisal.unresolved_condition:
        parts.append("未解：" + appraisal.unresolved_condition)
    return _clip_at_boundary("；".join(parts), max_chars)


def focus_hint(appraisal: Appraisal | None, *, max_chars: int = 80) -> str:
    """Deterministic attention hint; empty when there is no appraisal."""
    if appraisal is None:
        return ""
    if appraisal.next_move in NEXT_MOVES:
        return ("此刻：" + NEXT_MOVES[appraisal.next_move])[: max(1, int(max_chars))]
    if appraisal.unresolved_condition:
        return ("先处理：" + appraisal.unresolved_condition)[: max(1, int(max_chars))]
    if appraisal.tendency:
        return ("可尝试：" + appraisal.tendency)[: max(1, int(max_chars))]
    return ""


EARLIER_SPEAKER_DAYS = 14
EARLIER_SPEAKER_CHARS = 220
_EARLIER_CANDIDATES = 30


def _build_appraisal(event_id: str, subject_key: str, payload: Any, revision: int) -> Appraisal | None:
    """One appraisal record from a stored payload; shared by head and history reads."""
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return None
    return Appraisal(
        event_id=str(event_id or ""),
        subject_key=str(subject_key),
        trigger_refs=tuple(str(x) for x in (payload.get("trigger_refs") or ())),
        goal_refs=tuple(str(x) for x in (payload.get("goal_refs") or ())),
        value_refs=tuple(str(x) for x in (payload.get("value_refs") or ())),
        meaning=str(payload.get("meaning") or ""),
        feeling_text=str(payload.get("feeling_text") or ""),
        tendency=str(payload.get("tendency") or ""),
        next_move=str(payload.get("next_move") or ""),
        unresolved_condition=str(payload.get("unresolved_condition") or ""),
        review_at=str(payload.get("review_at") or ""),
        revision=int(revision or 0),
        topic_name=str(payload.get("topic_name") or ""),
        goal_occurrences=tuple((str(key), int(number))
                               for key, number in (payload.get("goal_occurrences") or {}).items()),
        action_refs=tuple(str(x) for x in (payload.get("action_refs") or ())),
        quoted_source_ref=str(payload.get("quoted_source_ref") or ""),
        quoted_reply_ref=str(payload.get("quoted_reply_ref") or ""),
    )


def _utc_ts(value: Any) -> float | None:
    from datetime import datetime as _dt, timezone as _tz
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = _dt.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_tz.utc)
    return moment.timestamp()


def _render_earlier_speaker(appraisal: Appraisal, sources: list) -> str:
    bits = []
    if appraisal.meaning:
        bits.append("我当时理解：" + _clip_at_boundary(appraisal.meaning, 96))
    if appraisal.feeling_text:
        bits.append("当时心情：" + _clip_at_boundary(appraisal.feeling_text, 40))
    if appraisal.unresolved_condition:
        bits.append("还没放下：" + _clip_at_boundary(appraisal.unresolved_condition, 60))
    if appraisal.next_move in NEXT_MOVES:
        bits.append("当时方向：" + NEXT_MOVES[appraisal.next_move])
    said = " ".join(str(getattr(sources[0], "content", "") or "").split())[:60]
    when = getattr(sources[0], "occurred_at", None)
    stamp = when.strftime("%m-%d") + " " if hasattr(when, "strftime") else ""
    head = "〔以前的事，不一定和眼前这句有关；有关就按新情况重看，无关别硬提〕"
    tail = (stamp + "对方当时说：" + said) if said else stamp.strip()
    body = "；".join(part for part in bits if part)
    text = head + "\n" + "\n".join(part for part in (tail, body) if part)
    if len(text) <= EARLIER_SPEAKER_CHARS:
        return text
    return text[: EARLIER_SPEAKER_CHARS - 1] + "…"


def render_earlier_for_speaker(scope: cc.Scope, *, speaker_id: str = "",
                               exclude_event_id: str = "", now: float | None = None,
                               conn: Any | None = None) -> str:
    """Read one earlier appraisal this speaker triggered, for turns whose topic has no match."""
    speaker = str(speaker_id or "").strip()
    if not getattr(scope, "complete", False) or not speaker:
        return ""
    owned = conn is None
    if owned:
        conn = _read_conn()
    if conn is None:
        return ""
    try:
        cutoff = float(now if now is not None else time.time()) - EARLIER_SPEAKER_DAYS * 86400
        rows = conn.execute(
            "SELECT event_id, subject_key, payload, revision, committed_at FROM cognitive_events "
            "WHERE operation='appraise' AND " + cc._SCOPE_SQL + "=? ORDER BY seq DESC LIMIT ?",
            (cc._scope_key(scope), _EARLIER_CANDIDATES),
        ).fetchall()
        from core.terminal import shadow
        store = shadow.get_default_store()
        seen: set[str] = set()
        for row in rows:
            subject_key = str(row["subject_key"] or "")
            if not subject_key or subject_key in seen:
                continue
            seen.add(subject_key)
            stamp = _utc_ts(row["committed_at"])
            if stamp is None or stamp < cutoff:
                continue
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError) as exc:
                warn("appraisal_earlier_bad_payload", err=repr(exc)[:80])
                continue
            head = _build_appraisal(str(row["event_id"] or ""), subject_key, payload,
                                    int(row["revision"] or 0))
            if head is None or not head.meaning:
                continue
            sources = _source_events(store, scope, head)
            if not sources or any(author(source) != speaker for source in sources):
                continue
            if exclude_event_id and any(source.event_id == exclude_event_id for source in sources):
                continue
            return _render_earlier_speaker(head, sources)
        return ""
    except Exception as exc:  # noqa: BLE001 - 读不到只是少一段，不允许带崩这一拍
        warn("appraisal_earlier_speaker_failed", err=repr(exc)[:120])
        return ""
    finally:
        if owned:
            conn.close()


def _read_conn() -> Any | None:
    from pathlib import Path
    import sqlite3

    from core.memory.belief_commit import database_path

    path = database_path()
    if not path:
        return None
    resolved = str(Path(path).resolve())
    if cc._blocked_under_tests(resolved) or not Path(resolved).is_file():
        return None
    conn = sqlite3.connect(Path(resolved).as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def current_appraisal_for(scope: cc.Scope, subject_key: str) -> Appraisal | None:
    """Read the current appraisal for one turn scope; missing DB means no block.

    The prompt convention is ``chat:<chat_id>``; callers may also use a person
    or goal key explicitly.  This is a read-only path and never creates state.
    """
    if not getattr(scope, "complete", False):
        return None
    conn = _read_conn()
    if conn is None:
        return None
    try:
        return current_appraisal(conn, subject_key=subject_key, scope=scope)
    finally:
        conn.close()


def render_appraisal_for(scope: cc.Scope, subject_key: str, *, max_chars: int = 160) -> str:
    """Render current appraisal plus its deterministic focus hint for a prompt."""
    current = current_appraisal_for(scope, subject_key)
    text = render_appraisal(current, max_chars=max_chars)
    hint = focus_hint(current)
    if hint:
        text = (text + "\n" + hint).strip() if text else hint
    return text


_TOPIC_NAMES = re.compile(r"《([^《》\r\n]{1,64})》|`([^`\r\n]{4,96})`")
_ASCII_WORD = re.compile(r"\w", re.ASCII)
_PLAIN_MENTION_MAX_CHARS = 512
_PLAIN_MENTION_MAX_CANDIDATES = 128
_PEER_TYPES = (EventType.MESSAGE, EventType.QUESTION, EventType.REQUEST, EventType.COMMAND)


@dataclass(frozen=True)
class Topic:
    subject: str
    name: str = ""
    evidence: str = ""
    by_name: bool = False
    quoted_subject: str = ""


def author(event: Any) -> str:
    return str(event.actor_account_id or event.actor_person_id or "")


def _display_author(event: Any) -> str:
    """只影响显示用称呼；身份比较仍然使用 author(event)。"""
    metadata = getattr(event, "metadata", None)
    if isinstance(metadata, dict):
        name = str(metadata.get("sender_name") or "").strip()
        if name:
            return name
        principal = metadata.get("principal")
        if isinstance(principal, dict):
            name = str(principal.get("display_name") or "").strip()
            if name:
                return name
    return author(event)


def _in_scope(event: Any, scope: cc.Scope) -> bool:
    return bool(event and (event.platform, event.bot_account_id, event.chat_id) == scope.key)


def _peer(event: Any, scope: cc.Scope) -> bool:
    return bool(_in_scope(event, scope) and event.event_type in _PEER_TYPES and author(event)
                and not (event.metadata or {}).get("is_self"))


def _named_topics(text: str) -> set[str]:
    return {(title or code).strip() for title, code in _TOPIC_NAMES.findall(text)
            if title.strip() or len(code.strip()) >= 4}


def _subject(event: Any, name: str = "", *, quoted_subject: str = "") -> str:
    if name:
        parts = ["named", author(event), name]
    elif quoted_subject:
        parts = ["quote", author(event), quoted_subject]
    else:
        parts = ["message", event.event_id]
    return "appraisal:topic:" + hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _has_index(conn: Any, name: str) -> bool:
    return bool(conn is not None and conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone())


def _binding(conn: Any, scope: cc.Scope, reference: str) -> str:
    # Legacy DBs have no topic bindings. Lookups on that schema skip the log entirely.
    if not _has_index(conn, "ix_appraisal_topic_trigger"):
        return ""
    row = conn.execute("SELECT subject_key FROM cognitive_events INDEXED BY ix_appraisal_topic_trigger WHERE "
                       + cc._TOPIC_WHERE_SQL + " AND " + cc._SCOPE_SQL + "=? AND "
                       + cc._APPRAISAL_TRIGGER_SQL + "=?", (cc._scope_key(scope), reference)).fetchone()
    return str(row[0]) if row else ""


def _mentions_name(text: str, name: str) -> bool:
    left = r"(?<![A-Za-z0-9_])" if _ASCII_WORD.fullmatch(name[0]) else ""
    right = r"(?![A-Za-z0-9_])" if _ASCII_WORD.fullmatch(name[-1]) else ""
    return re.search(left + re.escape(name) + right, text) is not None


def _plain_topic_names(conn: Any, scope: cc.Scope, text: str) -> set[str]:
    width = cc._APPRAISAL_TOPIC_PREFIX_LEN
    if len(text) < width or not _has_index(conn, "ix_appraisal_topic_name"):
        return set()
    if len(text) > _PLAIN_MENTION_MAX_CHARS:
        warn("appraisal_plain_mention_too_long", scope=scope.key, count=len(text))
        return set()
    text = _TOPIC_NAMES.sub("\n", text)
    prefixes = sorted({text[i:i + width] for i in range(len(text) - width + 1)})
    if not prefixes:
        return set()
    marks = ",".join("?" for _ in prefixes)
    rows = conn.execute(
        "SELECT " + cc._APPRAISAL_TOPIC_NAME_SQL + " FROM cognitive_state_scoped INDEXED BY ix_appraisal_topic_name WHERE "
        + cc._TOPIC_WHERE_SQL + " AND scope_key=? AND " + cc._APPRAISAL_TOPIC_PREFIX_SQL + f" IN ({marks}) LIMIT ?",
        (cc._scope_key(scope), *prefixes, _PLAIN_MENTION_MAX_CANDIDATES + 1),
    ).fetchall()
    if len(rows) > _PLAIN_MENTION_MAX_CANDIDATES:
        warn("appraisal_plain_mention_candidates_exhausted", scope=scope.key, limit=_PLAIN_MENTION_MAX_CANDIDATES)
        return set()
    return {row[0] for row in rows if _mentions_name(text, row[0])}


def _topic_from_names(conn: Any, scope: cc.Scope, message: Any) -> tuple[Topic, set[str]]:
    text = str(message.content or "")
    marked = _named_topics(text)
    names = marked | _plain_topic_names(conn, scope, text) if len(marked) < 2 else marked
    if len(names) == 1:
        name = next(iter(names))
        subject = _subject(message, name)
        head = current_appraisal(conn, scope=scope, subject_key=subject) if not marked else None
        if marked or (head and head.topic_name == name):
            return Topic(subject, name, message.event_id, by_name=True), names
    return Topic(_subject(message), evidence=message.event_id), names


def _quoted_event(store: Any, scope: cc.Scope, message: Any, external_id: str) -> Any:
    """Resolve the exact event the current message quotes, or ``None``.

    The canonical ``parent_event_id`` and the platform external quote id must
    agree when both are supplied; resolution is same-scope only and never falls
    back to display names or fuzzy matches.  The quoted event must predate the
    quoting message.
    """
    parent = store.get(message.parent_event_id) if message.parent_event_id else None
    if external_id:
        quote = store.get_by_external_msg_id(platform=scope.platform, bot_account_id=scope.account_id,
                                            chat_id=scope.chat_id, external_msg_id=external_id)
        if message.parent_event_id and (not quote or quote.event_id != message.parent_event_id):
            return None
        parent = quote
    if not _in_scope(parent, scope) or parent.event_id == message.event_id:
        return None
    try:
        if parent.occurred_at > message.occurred_at:
            return None
    except TypeError:
        return None
    return parent


def _quoted_peer(store: Any, scope: cc.Scope, message: Any, external_id: str) -> Any:
    parent = _quoted_event(store, scope, message, external_id)
    if parent is not None and parent.event_type == EventType.REPLY and parent.parent_event_id:
        parent = store.get(parent.parent_event_id)
    return parent if _peer(parent, scope) and parent.event_id != message.event_id else None


_QUOTED_SELF_MAX_CHARS = 240


def _revocation_active(store: Any, event_id: str) -> bool:
    """Only an explicit active state counts; missing/unknown evidence is absent."""
    state = getattr(store, "revocation_state", None)
    if not callable(state):
        return False
    try:
        return state(event_id) == "active"
    except Exception as exc:
        warn("appraisal_quote_revocation_check_failed", err=repr(exc)[:120])
        return False


def _account_echo(event: Any) -> bool:
    return bool(event and event.event_type in _PEER_TYPES and (event.metadata or {}).get("is_self") == 1)


def _own_speech(event: Any, scope: cc.Scope) -> bool:
    if not _in_scope(event, scope) or not str(event.content or "").strip():
        return False
    if event.event_type == EventType.REPLY and event.actor_account_id == scope.account_id:
        return True
    if not _account_echo(event) or not event.external_msg_id:
        return False
    try:
        from db import world_db

        return world_db.confirmed_self_sent(
            platform=scope.platform, account_id=scope.account_id, chat_id=scope.chat_id,
            external_msg_id=event.external_msg_id, text=event.content,
        )
    except Exception as exc:
        if warn_once("appraisal_own_speech_verify_failed"):
            warn("appraisal_own_speech_verify_failed", err=repr(exc)[:160])
        return False


def _quoted_account_speech(store: Any, scope: cc.Scope, message: Any, external_id: str) -> tuple[Any, bool]:
    if not _peer(message, scope):
        return None, False
    quoted = _quoted_event(store, scope, message, external_id)
    is_own = _own_speech(quoted, scope)
    if not is_own and not _account_echo(quoted):
        return None, False
    if not all(_revocation_active(store, event.event_id) for event in (quoted, message)):
        return None, False
    return quoted, is_own


def _quoted_self_event(store: Any, scope: cc.Scope, message: Any, external_id: str) -> Any:
    """Resolve the verified earlier bot utterance that this peer explicitly quoted."""
    quoted, is_own = _quoted_account_speech(store, scope, message, external_id)
    return quoted if is_own else None


def _render_quoted_account_speech(store: Any, scope: cc.Scope, message: Any, external_id: str) -> str:
    """Render the exact quoted words with their verified level of attribution."""
    quoted, is_own = _quoted_account_speech(store, scope, message, external_id)
    if quoted is None:
        return ""
    text = str(quoted.content or "").strip()
    if not text:
        return ""
    excerpt = json.dumps(text[:_QUOTED_SELF_MAX_CHARS], ensure_ascii=False)
    suffix = "（节选）" if len(text) > _QUOTED_SELF_MAX_CHARS else ""
    if not is_own:
        return f"[对方引用的账号原话]\n{excerpt}{suffix}\n这条由当前账号发出，具体是谁说的还没核实。"
    return f"[对方引用的我的原话]\n{excerpt}{suffix}"


def _inherited_topic(
    conn: Any, store: Any, scope: cc.Scope, message: Any, context_event_ids: tuple[str, ...],
) -> Topic | None:
    """Find an unambiguous title among verified earlier messages from the same peer."""
    from core.memory.belief_commit import evidence_state

    names: set[str] = set()
    source: Topic | None = None
    for reference in context_event_ids:
        event = store.get(reference)
        if reference == message.event_id or not _peer(event, scope) or author(event) != author(message):
            return None
        try:
            if event.occurred_at > message.occurred_at:
                return None
        except TypeError:
            return None
        if evidence_state(reference, scope, store=store) != "alive":
            return None
        topic, found = _topic_from_names(conn, scope, event)
        names |= found
        if len(names) > 1:
            return None
        if topic.by_name:
            source = topic
    return source


def resolve_topic(
    conn: Any, store: Any, scope: cc.Scope, message: Any, quoted_external_id: str = "",
    *, context_event_ids: tuple[str, ...] = (),
) -> Topic:
    """Resolve the writable subject and any cross-author topic available through an explicit quote."""
    fresh = Topic(_subject(message), evidence=message.event_id)
    if not _peer(message, scope):
        return fresh
    exact = _binding(conn, scope, message.event_id)
    if exact:
        head = current_appraisal(conn, subject_key=exact, scope=scope)
        return Topic(exact, head.topic_name if head else "", message.event_id)

    topic, names = _topic_from_names(conn, scope, message)
    if message.parent_event_id or quoted_external_id:
        parent = _quoted_peer(store, scope, message, quoted_external_id)
        if parent is None or len(names) > 1:
            return fresh
        subject = _binding(conn, scope, parent.event_id)
        if subject:
            head = current_appraisal(conn, subject_key=subject, scope=scope)
            name = head.topic_name if head else ""
        else:
            parent_topic, _ = _topic_from_names(conn, scope, parent)
            subject, name = parent_topic.subject, parent_topic.name
        # A message that explicitly names a different matter cannot rewrite the quoted topic.
        if names and names != {name}:
            return fresh
        if author(parent) != author(message):
            return Topic(_subject(message, name, quoted_subject=subject), name, parent.event_id,
                         quoted_subject=subject)
        return Topic(subject, name, parent.event_id)
    # Only a nameless, unquoted message may fall back to the earlier named work.
    if context_event_ids and not names:
        inherited = _inherited_topic(conn, store, scope, message, context_event_ids)
        if inherited is not None:
            return inherited
    return topic


def _source_events(store: Any, scope: cc.Scope, appraisal: Appraisal) -> list:
    from core.memory import belief_commit

    if appraisal.quoted_reply_ref:
        quoted = store.get(appraisal.quoted_reply_ref)
        if not _own_speech(quoted, scope) or not _revocation_active(store, appraisal.quoted_reply_ref):
            return []
    events = []
    for ref in appraisal.trigger_refs:
        event = store.get(ref)
        if not _peer(event, scope):
            continue
        try:
            state = belief_commit.evidence_state(ref, scope, store=store)
        except Exception:
            state = "unknown"
        if state != "dead":
            events.append(event)
    return events


def _matches_goal(appraisal: Appraisal, sources: list, key: str, active: tuple) -> bool:
    goal, occurrence = active
    return (key in appraisal.goal_refs and dict(appraisal.goal_occurrences).get(key, 1) == occurrence
            and all(source.event_id in goal.evidence_refs or
                    source.occurred_at.isoformat(sep=" ", timespec="seconds") >= goal.opened_at
                    for source in sources))


def _goal_subjects(conn: Any, scope: cc.Scope, key: str) -> list[str]:
    if not _has_index(conn, "ix_appraisal_topic_goal"):
        return []
    rows = conn.execute("SELECT subject_key FROM cognitive_state_scoped INDEXED BY ix_appraisal_topic_goal WHERE "
                        + cc._TOPIC_WHERE_SQL + " AND scope_key=? AND " + cc._APPRAISAL_GOAL_SQL + "=? LIMIT 2",
                        (cc._scope_key(scope), key)).fetchall()
    return [str(row[0]) for row in rows] if len(rows) == 1 else []


def _render_sourced(appraisal: Appraisal, sources: list, goal: str = "") -> str:
    origin = "；".join(f"{e.event_id}（发言人 {_display_author(e)}，{e.occurred_at:%Y-%m-%d %H:%M}）" for e in sources)
    suffix = f"；本轮正在复核目标 {goal}" if goal else ""
    excerpts, remaining = [], 240
    for source in sources:
        text = " ".join(str(source.content or "").split())
        if text and remaining > 0:
            excerpt = f"{_display_author(source)}：{text}"[:remaining]
            excerpts.append(excerpt)
            remaining -= len(excerpt)
    def field(label: str, value: str, cap: int) -> str:
        text = _clip_at_boundary(value, cap)
        return label + text if text else ""

    # 2026-11-21：「当时的…只作背景」把她的理解和情绪定了性成过期背景，等于理解形成了
    # 也不许影响这句话。这里改成「我对这件事的判断」：情绪仍算数，打算要重新判断，
    # 但不整句照搬。取消/被取代的旧评价在更上游就已经读不到了，这条不承担防重放。
    historical_parts = []
    if appraisal.meaning:
        historical_parts.append(field("我的理解：", appraisal.meaning, 120))
    if appraisal.feeling_text:
        historical_parts.append(field("我的情绪：", appraisal.feeling_text, 60))
    if appraisal.unresolved_condition:
        historical_parts.append(field("还没放下：", appraisal.unresolved_condition, 60))
    historical = "；".join(part for part in historical_parts if part)
    if appraisal.next_move in NEXT_MOVES:
        prefix = "" if not historical else "\n"
        historical += (prefix + "当时的方向：" + NEXT_MOVES[appraisal.next_move]
                       + "（那是对着当时的消息写的；现在按眼前情况重新判断）")
    if appraisal.tendency:
        historical += ("\n我当时打算怎么做：" + _clip_at_boundary(appraisal.tendency, 60)
                       + "（那是对着当时的消息写的；现在按眼前这句重新判断，别整句照搬）")
    return ("[我对这件事的判断，可随新情况修正]\n" + historical
            + f"\n触发消息：{origin}{suffix}"
            + (f"\n来源原话：{'；'.join(excerpts)}" if excerpts else "")
            + (("\n动作依据：" + "；".join(appraisal.action_refs[:2])[:160])
               if appraisal.action_refs else "")
            + "\n怎么用：这句的称呼、语气和要不要追问，按上面的理解和情绪来；"
              "还没放下的先问清或明说记着，别当成已经过去了。")


def _render_receipt_appraisal(conn: Any, scope: cc.Scope, active_goals: dict) -> str:
    if conn is None or not active_goals:
        return ""
    from core.context import situation_store
    from core.context.goal_source import goal_source_state
    from core.context.goal_wake import goal_occurrence, receipt_evidence_ref, receipt_signal
    from core.context.situation import open_intentions

    blocks = []
    with situation_store._LOCK:
        world = situation_store.get_world_model(scope.key)
        situation_store._apply_cognitive_closures(world, scope.chat_id)
        for goal in open_intentions(world):
            if goal.key not in active_goals or goal_source_state(goal, scope) == "dead":
                continue
            occurrence = goal_occurrence(world, goal.key)
            if active_goals[goal.key] != (goal, occurrence):
                continue
            head = current_appraisal(conn, scope=scope, subject_key=goal_receipt_subject(goal.key, occurrence))
            if head is None or head.goal_refs != (goal.key,) or head.goal_occurrences != ((goal.key, occurrence),):
                continue
            if len(head.trigger_refs) != 1 or not head.trigger_refs[0].startswith("goal-receipt:"):
                continue
            source = receipt_signal(world, goal.key, signal_id=head.trigger_refs[0].removeprefix("goal-receipt:"))
            if source is None or receipt_evidence_ref(source) != head.trigger_refs[0]:
                continue
            reference = str(source.payload.get("ref") or "")
            if head.action_refs != (reference,):
                continue
            latest = receipt_signal(world, goal.key)
            latest_line = ""
            if latest is not None and latest != source:
                payload = getattr(latest, "payload", None)
                payload = payload if isinstance(payload, dict) else {}
                latest_line = "\n最新回执：%s｜%s｜%s｜%s" % (
                    str(payload.get("ref") or ""), str(getattr(latest, "at", "") or ""),
                    str(payload.get("receipt_outcome") or ""), str(payload.get("detail") or "")[:120])
            blocks.append(f"[我之前对目标 {goal.key} 的看法，可随新情况修正]\n"
                          + render_appraisal(head, max_chars=240)
                          + f"\n执行回执（写这条看法时）：{reference}｜{source.at}" + latest_line)
    return "\n".join(blocks)


def _render_linked_goal(conn: Any, store: Any, scope: cc.Scope, message: Any, quote_id: str) -> str:
    """Read a receipt appraisal, or a closed goal's ledger, when the requester returns to their goal."""
    names = _named_topics(str(message.content or ""))
    quoted = bool(message.parent_event_id or quote_id)
    if len(names) > 1 or (not quoted and not names):
        return ""
    parent = _quoted_peer(store, scope, message, quote_id) if quoted else None
    speaker = author(message)
    if quoted and (parent is None or author(parent) != speaker):
        return ""

    from core.context import situation_store
    from core.context.goal_report import closed_summary
    from core.context.goal_source import goal_source_state
    from core.context.goal_wake import goal_occurrence
    from core.context.situation import project
    from core.memory.belief_commit import evidence_state

    with situation_store._LOCK:
        world = situation_store.get_world_model(scope.key)
        situation_store._apply_cognitive_closures(world, scope.chat_id)
        matched = None
        for goal in project(world.events).intentions.values():
            active = goal.status == "open" and goal.state in {"active", "waiting"}
            closed = goal.status == "closed" and goal.state in {"completed", "abandoned"}
            if not (active or closed):
                continue
            if parent is not None:
                sources = (parent,) if parent.event_id in goal.evidence_refs else ()
            else:
                sources = (store.get(ref) for ref in goal.evidence_refs)
            has_source = any(
                _peer(source, scope) and author(source) == speaker
                and (not names or _named_topics(str(source.content or "")) == names)
                and evidence_state(source.event_id, scope, store=store) == "alive"
                for source in sources
            )
            if not has_source or goal_source_state(goal, scope, store=store) == "dead":
                continue
            if matched is not None:
                return ""
            matched = goal
        if matched is None:
            return ""
        if matched.status == "closed":
            return closed_summary(matched)
        key = matched.key
        return _render_receipt_appraisal(conn, scope, {key: (matched, goal_occurrence(world, key))})


def render_relevant_appraisal_for(
    scope: cc.Scope, *, current_event_id: str = "", quoted_external_id: str = "",
    sender_id: str = "", active_goals: dict | None = None,
    context_event_ids: tuple[str, ...] = (),
    now: float | None = None, addressed: bool = True,
) -> str:
    """Use the effective appraisal head for this message, its verified quote or its exact title."""
    if not scope.complete or not (current_event_id or active_goals):
        return ""
    from core.terminal import shadow

    store = shadow.get_default_store()
    conn = _read_conn()
    try:
        legacy_subject = "chat:" + scope.chat_id
        legacy = (current_appraisal(conn, scope=scope, subject_key=legacy_subject) if conn is not None
                  else current_appraisal_for(scope, legacy_subject))
        message = store.get(current_event_id) if current_event_id else None
        quote_text = ""
        if _peer(message, scope) and (not sender_id or sender_id == author(message)):
            quote_text = _render_quoted_account_speech(store, scope, message, quoted_external_id)
            topic = resolve_topic(conn, store, scope, message, quoted_external_id,
                                  context_event_ids=context_event_ids)
            topic_text = ""
            if conn is not None and topic.quoted_subject:
                speaker_head = current_appraisal(conn, scope=scope, subject_key=topic.subject)
                if speaker_head and speaker_head.quoted_source_ref == topic.evidence:
                    sources = _source_events(store, scope, speaker_head)
                    if sources and all(author(source) == author(message) for source in sources):
                        topic_text = _render_sourced(speaker_head, sources)
            if not topic_text:
                head = current_appraisal(conn, scope=scope, subject_key=topic.quoted_subject or topic.subject) if conn is not None else None
                if head:
                    sources = _source_events(store, scope, head)
                    if sources and (not topic.by_name or all(author(source) == author(message) for source in sources)):
                        topic_text = _render_sourced(head, sources)
                elif legacy and set(legacy.trigger_refs) <= {message.event_id, topic.evidence}:
                    sources = _source_events(store, scope, legacy)
                    if sources:
                        topic_text = _render_sourced(legacy, sources)
            receipt = _render_linked_goal(conn, store, scope, message, quoted_external_id)
            if topic_text or receipt:
                return "\n".join(part for part in (quote_text, topic_text, receipt) if part)

        from_receipts = _render_receipt_appraisal(conn, scope, active_goals or {})
        if from_receipts:
            return "\n".join(part for part in (quote_text, from_receipts) if part)
        matches: dict[str, Appraisal] = {}
        for key, active in (active_goals or {}).items():
            heads = [current_appraisal(conn, scope=scope, subject_key=subject)
                     for subject in _goal_subjects(conn, scope, key)]
            for head in (*heads, legacy):
                if head is not None:
                    sources = _source_events(store, scope, head)
                    if sources and _matches_goal(head, sources, key, active):
                        matches[head.event_id] = head
        if len(matches) == 1:
            head = next(iter(matches.values()))
            sources = _source_events(store, scope, head)
            goal = next((key for key, active in (active_goals or {}).items()
                         if _matches_goal(head, sources, key, active)), "")
            return "\n".join(part for part in (quote_text, _render_sourced(head, sources, goal)) if part)
        # 只在「这一句本身没有话题锚」时才把旧判断拿过来：有目标、有串线上下文、有引用、
        # 或提到了具体名字时，宁可问清也不混用另一件事的评价（2026-11-24）。
        if (addressed and message is not None and _peer(message, scope)
                and (not sender_id or sender_id == author(message))
                and not active_goals and not context_event_ids
                and not str(getattr(message, "parent_event_id", "") or "")
                and not _named_topics(str(getattr(message, "content", "") or ""))
                and not topic.by_name and not topic.quoted_subject):
            earlier = render_earlier_for_speaker(scope, speaker_id=sender_id or author(message),
                                                 exclude_event_id=current_event_id, now=now,
                                                 conn=conn)
            return "\n".join(part for part in (quote_text, earlier) if part)
        return quote_text
    finally:
        if conn is not None:
            conn.close()
