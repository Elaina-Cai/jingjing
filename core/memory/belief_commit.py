"""显式 Belief 更正：回合授予的目标与证据、乐观版本、唯一认知提交口。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from core.context import cognitive_commit as cc
from core.context.situation import Belief, WorldEvent
from core.identity.platforms import is_group_chat
from core.infra.envflags import text
from core.terminal.event_model import HUMAN_SPEECH_VALUES
from safelog import warn


def fingerprint(belief: Belief) -> str:
    raw = json.dumps(asdict(belief), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def database_path() -> str:
    path = text("YINYUE_COGNITIVE_DB").strip()
    if not path:
        from db import world_db
        path = str(world_db.PATH)
    return path


def committed_facts(scope) -> list[WorldEvent]:
    identity = cc._scope_identity(scope)
    path = database_path()
    if identity is None or cc._blocked_under_tests(path) or not Path(path).is_file():
        return []
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        from core.memory.legacy_ownership import retired
        if retired(conn, cc._json(identity)):
            return []
        rows = conn.execute("SELECT payload FROM cognitive_events WHERE ((source='belief_tool' AND operation='revise') OR (source='legacy_import' AND operation='believe')) AND " + cc._SCOPE_SQL + "=? ORDER BY seq", (cc._json(identity),)).fetchall()
        events = []
        for (raw,) in rows:
            body = json.loads(raw)
            if isinstance(body.get("world_fact"), dict):
                events.append(WorldEvent("fact_asserted", str(body.get("at") or ""), body["world_fact"]))
        return events
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []
    finally:
        conn.close()


@dataclass(frozen=True)
class BeliefOffer:
    scope: cc.Scope
    belief: Belief
    subject: str
    revision: int
    event_id: str
    evidence_state: str = ""


def logical_subject(belief: Belief) -> str:
    raw = json.dumps([belief.subject_key or belief.key, belief.predicate], ensure_ascii=False)
    return "belief:" + hashlib.sha256(raw.encode()).hexdigest()


def current_belief_versions(offer: BeliefOffer) -> tuple[Belief, ...]:
    from core.context.situation_store import get_world_model
    from core.memory.beliefs import belief_evidence_quality, effective_beliefs

    model = get_world_model(offer.scope.chat_id, scope=offer.scope)
    return tuple(belief for belief in effective_beliefs(model)
                 if logical_subject(belief) == offer.subject
                 and belief_evidence_quality(belief, offer.scope) != "dead")


def offers(
    scope: cc.Scope,
    *,
    verify_evidence: bool = False,
) -> dict[str, BeliefOffer]:
    if not scope.complete:
        return {}
    from core.context.situation_store import get_world_model
    from core.memory.beliefs import belief_evidence_quality, effective_beliefs
    beliefs = effective_beliefs(get_world_model(scope.chat_id, scope=scope))
    if not beliefs:
        return {}
    path = database_path()
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) if Path(path).is_file() and not cc._blocked_under_tests(path) else None
    try:
        result = {}
        for index, belief in enumerate(beliefs, 1):
            subject = logical_subject(belief)
            quality = belief_evidence_quality(
                belief,
                scope,
                verify=verify_evidence,
            )
            try:
                rows = conn.execute("SELECT event_id FROM cognitive_events WHERE subject_key=? AND " + cc._SCOPE_SQL + "=? ORDER BY seq", (subject, cc._scope_key(scope))).fetchall() if conn else []
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                rows = []
            result["b%d" % index] = BeliefOffer(
                scope,
                belief,
                subject,
                len(rows),
                str(rows[-1][0]) if rows else "",
                quality,
            )
        return result
    finally:
        if conn is not None:
            conn.close()


def evidence(reference: str, scope: cc.Scope, visible: frozenset[str]):
    if reference not in visible:
        return None
    try:
        from core.terminal.shadow import get_default_store
        row = get_default_store().get(reference)
        if row is None or (row.platform, row.bot_account_id, row.chat_id) != scope.key:
            return None
        if str(row.event_type) not in HUMAN_SPEECH_VALUES or (row.metadata or {}).get("is_self"):
            return None
        return row
    except Exception as exc:
        warn("belief_evidence_lookup_failed", scope=scope.key, ref=reference, err=repr(exc)[:120])
        return None


def evidence_state(reference: str, scope: cc.Scope, *, store: Any = None) -> str:
    """这条证据现在什么状态：``alive`` / ``dead`` / ``unknown``。

    为什么要三态：**「行找不到」不等于「证据死了」**——事件库会轮转、会清理，
    经验却可能比它活得久。只有能**证明**矛盾的才判死（跨了会话 / 是自己说的 / 不是人话），
    查不到、查不了一律 ``unknown``，由调用方决定怎么用（当前口径：只有全部为 ``dead``
    才把经验拿掉）。

    已知边界：微信撤回不会删原来的行，事件流里只多一条 `revokemsg` 系统消息，
    采集层能把撤回行链到原消息；链不上或回查失败时仍保持 ``unknown``。
    """
    ref = str(reference or "").strip()
    if not ref or not getattr(scope, "complete", False):
        return "dead"
    try:
        if store is None:
            from core.terminal.shadow import get_default_store

            store = get_default_store()
        row = store.get(ref)
    except Exception as exc:  # noqa: BLE001 - 查不了不等于没有
        warn("belief_evidence_state_failed", ref=ref, err=repr(exc)[:120])
        return "unknown"
    if row is None:
        return "unknown"
    if (row.platform, row.bot_account_id, row.chat_id) != scope.key:
        return "dead"
    if str(getattr(row, "event_type", "")) not in HUMAN_SPEECH_VALUES:
        return "dead"
    if (row.metadata or {}).get("is_self"):
        return "dead"
    check_revoke = getattr(store, "revocation_state", None)
    if check_revoke is not None:
        try:
            state = str(check_revoke(str(getattr(row, "event_id", "") or "")) or "")
        except Exception as exc:
            warn("belief_evidence_revoke_check_failed", ref=ref, err=repr(exc)[:120])
            return "unknown"
        if state == "revoked":
            return "dead"
        if state == "unknown":
            return "unknown"
    return "alive"


def evidence_actors(event) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        actor for actor in (event.actor_person_id, event.actor_account_id) if actor
    ))


def _evidence_subject_mismatch(offer: BeliefOffer, actors: tuple[str, ...]) -> bool:
    scope, belief = offer.scope, offer.belief
    subject = belief.subject_key
    if not subject or subject in actors:
        return False
    # Later revisions retain the verified source speaker even if the source row ages out.
    if belief.key.startswith("belief-correct:") and belief.who == subject:
        return True
    refs = frozenset(belief.evidence_refs)
    for ref in refs:
        origin = evidence(ref, scope, refs)
        if origin is not None and subject in evidence_actors(origin):
            return True
    return False


def submit(offer: BeliefOffer, *, scope: cc.Scope, value: str, change_kind: str, evidence_ref: str, visible: frozenset[str], valid_from: str = "", dry_run: bool = False) -> dict:
    """模型不能传 scope/revision；这些来自程序在回合开始时授予的 offer。"""
    if scope.key != offer.scope.key or not scope.complete:
        return {"result": "rejected", "reason": "scope_mismatch"}
    if change_kind not in {"correction", "time_change"} or not str(value).strip():
        return {"result": "rejected", "reason": "invalid_change"}
    if change_kind == "time_change" and not valid_from:
        return {"result": "rejected", "reason": "valid_from_required"}
    try:
        if valid_from and datetime.fromisoformat(valid_from).tzinfo is not None:
            raise ValueError("timezone")
        proof = evidence(evidence_ref, scope, visible)
        if proof is None:
            return {"result": "rejected", "reason": "evidence_not_visible"}
        group = is_group_chat(scope.platform, proof.chat_type, scope.chat_id) or is_group_chat(
            scope.platform, scope.chat_type,
        )
        actors = evidence_actors(proof)
        if group and _evidence_subject_mismatch(offer, actors):
            return {"result": "rejected", "reason": "evidence_subject_mismatch"}
        at = proof.occurred_at.isoformat(sep=" ", timespec="seconds")
        if proof.occurred_at.tzinfo is not None:
            return {"result": "rejected", "reason": "evidence_time_not_local"}
        target = fingerprint(offer.belief)
        operation_key = "belief-correct:" + hashlib.sha256((offer.subject + evidence_ref).encode()).hexdigest()
        subject_key = offer.belief.subject_key or offer.belief.key
        speaker = subject_key if group and subject_key in actors else next(iter(actors), "")
        fact = {
            "key": operation_key,
            "value": str(value).strip(),
            "source": "chat_observed",
            "who": speaker,
            "subject_key": subject_key,
            "predicate": offer.belief.predicate,
            "scope": ":".join(scope.key),
            "supersedes": offer.belief.key,
            "supersedes_fingerprint": target,
            "change_kind": change_kind,
            "valid_from": valid_from,
            "valid_to": offer.belief.valid_to if change_kind == "correction" else "",
            "evidence_refs": [evidence_ref],
        }
        if dry_run:
            return {"result": "dry", "reason": "not_committed"}
        from core.context import situation_store as store
        from core.memory.beliefs import effective_beliefs
        with store._LOCK:
            conn = cc.connect()
            try:
                # 纯重试先交给提交口幂等判定；新提案必须仍指向活跃版本。
                previous = conn.execute("SELECT payload,supersedes FROM cognitive_events WHERE subject_key=? AND " + cc._SCOPE_SQL + "=? AND json_extract(payload,'$.world_fact.key')=?", (offer.subject, cc._scope_key(scope), operation_key)).fetchone()
                if previous is None and target not in {fingerprint(item) for item in effective_beliefs(store.get_world_model(scope.chat_id, scope=scope))}:
                    return {"result": "conflict", "reason": "belief_version_changed"}
                body = {"world_fact": fact, "at": at}
                supersedes = offer.event_id
                if previous is not None:
                    stored = json.loads(previous[0])
                    old = stored["world_fact"]
                    if (old["value"], old["change_kind"], old.get("valid_from", "")) == (str(value).strip(), change_kind, valid_from):
                        body = stored
                        supersedes = str(previous[1] or "")
                outcome = cc.commit_updates(conn, context=cc.CommitContext(scope, visible_event_ids=visible, allowed_subjects=frozenset({offer.subject}), source="belief_tool"), proposals=[cc.Proposal("correct", offer.subject, "revise", body, (evidence_ref,), occurrence=operation_key, supersedes=supersedes)], expected_revisions={offer.subject: offer.revision}, idempotency_key=operation_key)
                return {"result": "committed" if outcome.ok else "conflict", "reasons": list(outcome.rejected), "duplicates": bool(outcome.duplicates)}
            finally:
                conn.close()
    except (ValueError, TypeError) as exc:
        return {"result": "rejected", "reason": str(exc)}
    except Exception as exc:
        warn("belief_correction_failed", subject=offer.subject, scope=scope.key, err=repr(exc)[:120])
        return {"result": "error", "reason": "commit_unavailable"}
