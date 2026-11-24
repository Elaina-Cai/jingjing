"""P4 evidence-bound method experience.

Lessons are event-sourced in ``cognitive_commit`` under the ``reflect``
operation.  They are *not* a second emotion source and they never count
messages: one lesson needs explicit evidence refs, has a confidence, and can
be revised only by naming the current head event in ``supersedes``.  Replaying
the same evidence is idempotent.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from core.context import cognitive_commit as cc

SCHEMA = "reflection/v1"
OUTCOMES = frozenset({"success", "failure"})


@dataclass(frozen=True)
class Lesson:
    event_id: str
    method: str
    outcome: str
    evidence_refs: tuple[str, ...]
    lesson: str
    confidence: float = 0.5
    revision: int = 0


def _refs(values: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(str(v).strip() for v in (values or ()) if str(v).strip())


def _occurrence(subject_key: str, outcome: str, evidence: tuple[str, ...], supersedes: str) -> str:
    raw = "\x00".join([subject_key, outcome, *sorted(evidence), str(supersedes or "")])
    return "reflect:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def record_lesson(
    conn: Any,
    *,
    scope: cc.Scope,
    method: str,
    outcome: str,
    evidence_refs: Iterable[Any],
    lesson: str,
    confidence: float = 0.5,
    supersedes: str = "",
    occurrence: str = "",
    expected_revision: int | None = None,
    actor_key: str = "",
    source: str = "reflection",
) -> cc.CommitResult:
    name = str(method or "").strip()
    result = str(outcome or "").strip()
    evidence = _refs(evidence_refs)
    text = str(lesson or "").strip()
    if not name:
        raise ValueError("method_required")
    if result not in OUTCOMES:
        raise ValueError("outcome_invalid")
    if not evidence:
        raise ValueError("reflection_evidence_required")
    if not text:
        raise ValueError("lesson_required")
    score = min(1.0, max(0.0, float(confidence)))
    subject = "method:" + name

    occ = str(occurrence or "").strip() or _occurrence(subject, result, evidence, str(supersedes or ""))
    replay_id = cc.event_id_for(scope, occ, subject, "reflect")
    replay = conn.execute("SELECT 1 FROM cognitive_events WHERE event_id=?", (replay_id,)).fetchone() is not None
    expected: dict[str, int] | None = {subject: int(expected_revision)} if expected_revision is not None else None
    if supersedes and not replay:
        prior = cc.read_state(conn, subject, scope=scope)
        if (
            prior is None
            or str(prior.get("event_id") or "") != str(supersedes)
            or str(prior.get("operation") or "") != "reflect"
        ):
            return cc.CommitResult(rejected=(("", "supersedes_not_current"),))
        expected = {subject: int(prior.get("revision") or 0)}

    payload = {
        "schema": SCHEMA,
        "method": name,
        "outcome": result,
        "evidence_refs": list(evidence),
        "lesson": text,
        "confidence": round(score, 4),
        "supersedes": str(supersedes or ""),
    }
    proposal = cc.Proposal(
        proposal_id=occ,
        subject_key=subject,
        operation="reflect",
        payload=payload,
        evidence_refs=evidence,
        occurrence=occ,
        supersedes=str(supersedes or ""),
        scope=scope,
    )
    context = cc.CommitContext(
        scope=scope,
        visible_event_ids=frozenset(evidence),
        allowed_subjects=frozenset({subject}),
        require_evidence=True,
        source=str(source or "reflection"),
        actor_key=str(actor_key or ""),
    )
    return cc.commit_updates(
        conn,
        context=context,
        proposals=(proposal,),
        expected_revisions=expected,
        idempotency_key="reflection:" + subject,
    )


def current_lesson(conn: Any, *, method: str, scope: cc.Scope | None = None) -> Lesson | None:
    state = cc.read_state(conn, "method:" + str(method or ""), scope=scope)
    if not state or str(state.get("operation") or "") != "reflect":
        return None
    payload = state.get("payload") or {}
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return None
    return Lesson(
        event_id=str(state.get("event_id") or ""),
        method=str(payload.get("method") or method),
        outcome=str(payload.get("outcome") or ""),
        evidence_refs=tuple(str(x) for x in (payload.get("evidence_refs") or ())),
        lesson=str(payload.get("lesson") or ""),
        confidence=float(payload.get("confidence") or 0.0),
        revision=int(state.get("revision") or 0),
    )


def select_method(
    conn: Any,
    *,
    methods: Iterable[str],
    scope: cc.Scope | None = None,
    min_confidence: float = 0.6,
) -> str:
    """Pick a method from evidence-backed lessons; empty when none is positive."""
    candidates = sorted({str(m).strip() for m in methods if str(m).strip()})
    if not candidates:
        return ""
    floor = min(1.0, max(0.0, float(min_confidence)))
    scored: list[tuple[float, str]] = []
    for method in candidates:
        lesson = current_lesson(conn, method=method, scope=scope)
        if lesson is None or lesson.confidence < floor:
            scored.append((0.0, method))
            continue
        scored.append((lesson.confidence if lesson.outcome == "success" else -lesson.confidence, method))
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, best_method = scored[0]
    return best_method if best_score > 0 else ""


def render_lesson(lesson: Lesson | None, *, max_chars: int = 140) -> str:
    if lesson is None:
        return ""
    mark = "有效" if lesson.outcome == "success" else "受挫"
    text = f"{lesson.method}：{mark}（置信度 {lesson.confidence:.2f}）"
    if lesson.lesson:
        text += "；" + lesson.lesson
    return text[: max(1, int(max_chars))]



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


def current_lessons_for_scope(scope: cc.Scope) -> tuple[Lesson, ...]:
    """Read every current method lesson for one turn scope; never writes."""
    if not getattr(scope, "complete", False):
        return ()
    conn = _read_conn()
    if conn is None:
        return ()
    try:
        rows = conn.execute(
            "SELECT DISTINCT subject_key FROM cognitive_events WHERE operation='reflect' AND "
            + cc._SCOPE_SQL + "=? ORDER BY subject_key",
            (cc._scope_key(scope),),
        ).fetchall()
        out: list[Lesson] = []
        for (subject,) in rows:
            name = str(subject or "")
            if not name.startswith("method:"):
                continue
            lesson = current_lesson(conn, method=name[len("method:"):], scope=scope)
            if lesson is not None:
                out.append(lesson)
        return tuple(out)
    except Exception:
        return ()
    finally:
        conn.close()


def rank_methods(
    lessons: Iterable[Lesson],
    methods: Iterable[str],
    *,
    min_confidence: float = 0.6,
) -> str:
    """Rank known methods from evidence-backed lessons; empty when none wins."""
    by_method: dict[str, Lesson] = {}
    for lesson in lessons or ():
        old = by_method.get(lesson.method)
        if old is None or lesson.revision >= old.revision:
            by_method[lesson.method] = lesson
    candidates = sorted({str(m).strip() for m in methods if str(m).strip()})
    if not candidates:
        return ""
    floor = min(1.0, max(0.0, float(min_confidence)))
    scored: list[tuple[float, str]] = []
    for method in candidates:
        lesson = by_method.get(method)
        if lesson is None or lesson.confidence < floor:
            scored.append((0.0, method))
            continue
        scored.append((lesson.confidence if lesson.outcome == "success" else -lesson.confidence, method))
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, best_method = scored[0]
    return best_method if best_score > 0 else ""
