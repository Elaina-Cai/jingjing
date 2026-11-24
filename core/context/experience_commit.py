"""Turn-granted appraisal/reflection writes, using the existing model call only.

An interpretation is not a fact, delivery receipt or permission. External source
IDs, scope, target heads and allowed methods are supplied by the program.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import math
import json
from pathlib import Path
import sqlite3

from core.context import appraisal, cognitive_commit as cc
from core.context.goal_wake import (
    GoalWake, _event_id, goal_occurrence, pending_wakes, receipt_evidence_ref, receipt_signal,
)
from core.memory import belief_commit
from safelog import warn


@dataclass(frozen=True)
class ExperienceOffer:
    scope: cc.Scope
    reference: str
    fingerprint: str
    content: str
    heads: tuple[tuple[str, str, int], ...]
    goals: frozenset[str]
    methods: frozenset[str]
    goal_occurrences: tuple[tuple[str, int], ...] = ()
    appraisal_subject: str = ""
    quoted_external_id: str = ""
    speaker: str = ""
    context_event_ids: tuple[str, ...] = ()
    receipt: GoalWake | None = None
    quoted_reply_ref: str = ""


_RECEIPT_STATUSES = frozenset({"done", "sent", "silent", "failed", "blocked"})

# 2026-11-21：线上非本人消息里 21.8%（25367 条里 5522 条）是媒体占位符。锚点落在
# 「没有描述的媒体」上时，这一拍唯一的评价名额就被占掉，真正带关系含义的那句话
# 根本没进候选 —— 她要么不评价，要么对着一张图写理解。
def _media_only(proof) -> bool:
    """这条证据没有可评价的话：空的，或只有一个没描述的媒体占位符。"""
    from core.conversation.media import is_placeholder_only

    return is_placeholder_only(str(getattr(proof, "content", "") or ""))


#: 纯应答词：单独一句时几乎没有可评价的内容。线上常见锚点就是一句「嗯嗯」，
#: 拿它当唯一候选，评价机会就被一句附和占掉了。
_FILLER = frozenset({"嗯", "嗯嗯", "哦", "哦哦", "噢", "好", "好的", "好呀",
                     "行", "可以", "收到", "是的", "对", "ok", "OK", "Ok"})


def _no_words(proof) -> bool:
    """媒体占位符，或整条只是一句应答词。"""
    text = " ".join(str(getattr(proof, "content", "") or "").split())
    return _media_only(proof) or text in _FILLER


_WORDS_WINDOW = timedelta(minutes=10)


def _near_words(anchor, proof) -> bool:
    """媒体锚点只认同一段聊天里紧邻的话；时区不可比时不猜。"""
    try:
        return abs(proof.occurred_at - anchor.occurred_at) <= _WORDS_WINDOW
    except (TypeError, AttributeError):
        return False


def _prefer_words(proofs, fallback):
    """锚点只是没描述的表情/图时，换成紧邻着它的最近一条有人话的消息。"""
    if len(proofs) == 1 and _no_words(proofs[0]):
        anchor = proofs[0]
        words = [proof for proof in fallback if not _no_words(proof) and _near_words(anchor, proof)]
        if words:
            return [max(words, key=lambda proof: proof.occurred_at)]
    return proofs


def _speaker_label(proof) -> str:
    """给模型用可读称呼，拿不到才回退账号 ID。"""
    metadata = getattr(proof, "metadata", None)
    if isinstance(metadata, dict):
        principal = metadata.get("principal")
        display_name = principal.get("display_name") if isinstance(principal, dict) else None
        for value in (metadata.get("sender_name"), display_name):
            label = str(value or "").strip()
            if label:
                return label
    return appraisal.author(proof)


def _fingerprint(proof):
    raw = [proof.event_id, proof.platform, proof.bot_account_id, proof.chat_id,
           str(proof.event_type), proof.actor_person_id, proof.actor_account_id,
           proof.content, proof.parent_event_id, proof.occurred_at.isoformat(), bool((proof.metadata or {}).get("is_self"))]
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()


def offers(scope: cc.Scope, visible: frozenset[str], anchor: tuple[str, str] | None = None,
           *, context_event_ids: tuple[str, ...] = (),
           related_refs: tuple[str, ...] = ()) -> dict[str, ExperienceOffer]:
    if not scope.complete or not visible:
        return {}
    context_refs = tuple(dict.fromkeys(context_event_ids))
    if not anchor or anchor[0] not in visible or any(ref not in visible for ref in context_refs):
        context_refs = ()
    from core.context.goal_source import goal_source_state
    from core.context.situation_store import get_world_model
    from core.terminal.shadow import get_default_store
    from core.context.situation import open_intentions
    from core.tools.catalog import read_only_in_plan
    from core.context.goal_wake import goal_occurrence

    world = get_world_model(scope.chat_id, scope=scope)
    goals = frozenset(
        goal.key
        for goal in open_intentions(world)
        if goal_source_state(goal, scope) != "dead"
    )
    goal_occurrences = tuple((key, goal_occurrence(world, key)) for key in sorted(goals))
    path = Path(belief_commit.database_path())
    if cc._blocked_under_tests(str(path)):
        return {}
    # 本轮其他明确相关的请求也要各自成为评价候选（anchor 仍排在最前）。
    if anchor and anchor[0] in visible:
        references = (anchor[0],)
        seen_refs = {anchor[0]}
        for ref in related_refs:
            if ref in visible and ref not in seen_refs:
                seen_refs.add(ref)
                references += (ref,)
    else:
        references = tuple(sorted(visible))
    proofs = [belief_commit.evidence(ref, scope, visible) for ref in references]
    proofs = [proof for proof in proofs if proof is not None]
    if not proofs:
        return {}
    if anchor and anchor[0] in visible and len(proofs) == 1 and _no_words(proofs[0]):
        rest = [belief_commit.evidence(ref, scope, visible) for ref in sorted(visible)]
        proofs = _prefer_words(proofs, [proof for proof in rest if proof is not None])
    conn = appraisal._read_conn()
    try:
        store = get_default_store()
        methods = read_only_in_plan()
        out = {}
        for proof in proofs:
            external_id = anchor[1] if anchor and anchor[0] == proof.event_id else ""
            topic = appraisal.resolve_topic(conn, store, scope, proof, external_id,
                                            context_event_ids=context_refs)
            subjects = (topic.subject, *("method:" + name for name in methods))
            heads = _offered_heads(conn, scope, subjects)
            quoted = appraisal._quoted_self_event(store, scope, proof, external_id)
            out[proof.event_id] = ExperienceOffer(scope, proof.event_id, _fingerprint(proof), str(proof.content),
                                                   heads, goals, methods, goal_occurrences, topic.subject, external_id,
                                                   _speaker_label(proof), context_refs,
                                                   quoted_reply_ref=quoted.event_id if quoted is not None else "")
        return out
    finally:
        if conn is not None:
            conn.close()


def _offered_heads(conn, scope, subjects):
    if conn is None or not cc._state_table_exists(conn):
        return ()
    marks = ",".join("?" for _ in subjects)
    return tuple((str(subject), str(event), int(revision)) for subject, event, revision in conn.execute(
        "SELECT subject_key,event_id,revision FROM cognitive_state_scoped WHERE scope_key=?"
        f" AND operation IN ('appraise','reflect') AND subject_key IN ({marks})", (cc._scope_key(scope), *subjects)))


def _occurrence(subject, reference):
    return "experience:" + hashlib.sha256((subject + "\x00" + reference).encode()).hexdigest()


def receipt_offers(scope: cc.Scope, review_items: list[dict]) -> dict[str, ExperienceOffer]:
    """Offer only the newest known receipt, provided it was shown this turn."""
    if not scope.complete or not review_items or cc._blocked_under_tests(belief_commit.database_path()):
        return {}
    from core.context import situation_store as store

    offers = {}
    conn = appraisal._read_conn()
    try:
        with store._LOCK:
            world = store.get_world_model(scope.key)
            store._apply_cognitive_closures(world, scope.chat_id)
            pending = set(pending_wakes(world))
            for item in review_items:
                candidate = GoalWake(**{**item, "scope": tuple(item["scope"])})
                if (candidate not in pending or candidate.reason != "receipt"
                        or candidate.receipt_outcome not in _RECEIPT_STATUSES or not candidate.receipt_ref):
                    continue
                latest = receipt_signal(world, candidate.goal_id)
                reference = "goal-receipt:" + candidate.event_id
                if latest is None or receipt_evidence_ref(latest) != reference:
                    continue
                occurrence = goal_occurrence(world, candidate.goal_id)
                subject = appraisal.goal_receipt_subject(candidate.goal_id, occurrence)
                offers[reference] = ExperienceOffer(
                    scope=scope, reference=reference, fingerprint=_event_id(latest), content="",
                    heads=_offered_heads(conn, scope, (subject,)),
                    goals=frozenset({candidate.goal_id}), methods=frozenset(),
                    goal_occurrences=((candidate.goal_id, occurrence),),
                    appraisal_subject=subject, receipt=candidate,
                )
        return offers
    finally:
        if conn is not None:
            conn.close()


def render(available: dict[str, ExperienceOffer]) -> str:
    if not available:
        return ""
    # 首行是线上验收入的判据：评价触发面含理解、态度、情绪、意图，但寒暄收尾不评。
    lines = ["这些是这一拍递给我看的话；旧话没有新变化就不要重新评价。",
             "当这句话改变了我对局面的理解、对某人的态度、我自己的情绪或下一步意图时用 evaluate_experience；只是寒暄或收尾就不评价。",
             "评价时分别写：meaning 是我的理解，feeling_text 是我的情绪、只写我自己，tendency 是我的意图（自然语言），next_move 是这一拍的实际方向（reply 自然回应／clarify 先问清关键事实／act 先完成必要动作／wait 这一拍先收住或等待／comfort 先接住情绪、慢点给方案），只约束这一拍，下一拍按新情况重判。",
             "后一条消息解释了新的背景时，保留仍成立的部分，并把新变化写进评价。",
             "评价是可撤回的解释，不修改事实、承诺完成或投递状态；沉默不是关系恶化的证据。",
             "这一拍决定不开口（rest）也可以先把这句话带来的变化记下来再收住；反过来，记了评价也不等于要说话。",
             "方法反思只记有明确反馈支持的经验，不能用日记、自述或记忆摘要循环作证。"]
    for ref, offer in available.items():
        if offer.receipt:
            lines.append(f"- {ref}: 目标 {offer.receipt.goal_id} 的执行回执。用于 appraisal 时填这个 goal_id。")
            continue
        speaker = json.dumps(offer.speaker or "身份未知", ensure_ascii=False)
        lines.append(f"- {ref}: 对方原话（发言人 {speaker}）" + json.dumps(offer.content, ensure_ascii=False))
    goals = sorted({key for offer in available.values() for key in offer.goals})
    methods = sorted({method for offer in available.values() for method in offer.methods})
    if goals:
        lines.append("可关联目标：" + "、".join(goals))
    if methods:
        lines.append("可关联方法：" + "、".join(methods))
    return "\n".join(lines)


def _submit_receipt(offer: ExperienceOffer, args: dict, *, scope: cc.Scope, dry_run: bool) -> dict:
    from core.context import situation_store as store

    candidate = offer.receipt
    goal = candidate.goal_id
    if args.get("mode") != "appraisal" or {"method", "outcome", "lesson", "confidence"} & args.keys():
        raise ValueError("receipt_appraisal_required")
    if args.get("goal_id") != goal or candidate.scope != scope.key:
        raise ValueError("receipt_goal_mismatch")
    if not isinstance(args.get("meaning"), str) or not args["meaning"].strip():
        raise ValueError("appraisal_meaning_required")
    for key in ("feeling_text", "tendency", "unresolved_condition"):
        if key in args and not isinstance(args[key], str):
            raise ValueError("appraisal_text_invalid")
    next_move = args.get("next_move", "")
    if not isinstance(next_move, str) or (next_move and next_move not in appraisal.NEXT_MOVES):
        raise ValueError("appraisal_next_move_invalid")

    subject = offer.appraisal_subject
    occurrence = _occurrence(subject, offer.reference)
    supersedes, revision = {key: (head, version) for key, head, version in offer.heads}.get(subject, ("", 0))
    # The scope, goal revision and current source stay checked through the write.
    with store._LOCK:
        world = store.get_world_model(scope.key)
        store._apply_cognitive_closures(world, scope.chat_id)
        latest = receipt_signal(world, goal)
        if candidate not in pending_wakes(world) or latest is None or _event_id(latest) != offer.fingerprint:
            raise ValueError("receipt_source_changed_or_closed")
        if dry_run:
            return {"result": "dry", "reason": "not_committed"}

        conn = cc.connect()
        try:
            existing = conn.execute(
                "SELECT supersedes FROM cognitive_events WHERE event_id=?",
                (cc.event_id_for(scope, occurrence, subject, "appraise"),),
            ).fetchone()
            if existing is not None:
                supersedes = str(existing[0] or "")
            result = appraisal.record_appraisal(
                conn, scope=scope, subject_key=subject, trigger_refs=(offer.reference,),
                meaning=args["meaning"], feeling_text=args.get("feeling_text", ""),
                tendency=args.get("tendency", ""), next_move=str(next_move or ""),
                unresolved_condition=args.get("unresolved_condition", ""),
                goal_refs=(goal,), goal_occurrences=offer.goal_occurrences, action_refs=(candidate.receipt_ref,),
                supersedes=supersedes, occurrence=occurrence, expected_revision=revision, source="experience_tool",
            )
            return {"result": "committed" if result.ok else "conflict",
                    "duplicates": bool(result.duplicates), "reasons": list(result.rejected)}
        finally:
            conn.close()


def submit(offer: ExperienceOffer | None, args: dict, *, scope: cc.Scope,
           visible: frozenset[str], dry_run: bool = False,
           action_refs: tuple[str, ...] = ()) -> dict:
    if offer is None or offer.scope.key != scope.key or not scope.complete:
        return {"result": "rejected", "reason": "experience_not_visible"}
    try:
        allowed = {"mode", "evidence_ref", "meaning", "feeling_text", "tendency", "next_move", "unresolved_condition",
                   "goal_id", "method", "outcome", "lesson", "confidence"}
        if set(args) - allowed or args.get("mode") not in {"appraisal", "reflection"}:
            raise ValueError("experience_arguments_invalid")
        if args.get("evidence_ref") != offer.reference:
            raise ValueError("experience_reference_changed")
        if offer.receipt:
            return _submit_receipt(offer, args, scope=scope, dry_run=dry_run)
        proof = belief_commit.evidence(offer.reference, scope, visible)
        if proof is None or _fingerprint(proof) != offer.fingerprint:
            raise ValueError("experience_source_changed_or_hidden")
        mode = args["mode"]
        goal = str(args.get("goal_id") or "")
        if goal and goal not in offer.goals:
            raise ValueError("experience_goal_not_visible")
        quoted_reply_ref = ""
        if mode == "appraisal":
            if not isinstance(args.get("meaning"), str) or not args["meaning"].strip():
                raise ValueError("appraisal_meaning_required")
            if any(ref not in visible for ref in offer.context_event_ids):
                raise ValueError("appraisal_context_no_longer_visible")
            if offer.quoted_reply_ref:
                from core.terminal.shadow import get_default_store

                quoted = appraisal._quoted_self_event(
                    get_default_store(), scope, proof, offer.quoted_external_id)
                if quoted is None or quoted.event_id != offer.quoted_reply_ref:
                    raise ValueError("experience_quoted_self_changed")
                quoted_reply_ref = quoted.event_id
            subject, operation = offer.appraisal_subject, "appraise"
        else:
            method = str(args.get("method") or "")
            if method not in offer.methods:
                raise ValueError("experience_method_not_granted")
            if args.get("outcome") not in {"success", "failure"} or not isinstance(args.get("lesson"), str) or not args["lesson"].strip():
                raise ValueError("reflection_feedback_required")
            if "next_move" in args:
                raise ValueError("appraisal_next_move_not_for_reflection")
            subject, operation = "method:" + method, "reflect"
        if mode == "reflection":
            confidence = args.get("confidence", 0.5)
            if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise ValueError("reflection_confidence_invalid")
        for key in ("feeling_text", "tendency", "unresolved_condition"):
            if key in args and not isinstance(args[key], str):
                raise ValueError("appraisal_text_invalid")
        next_move = args.get("next_move", "")
        if not isinstance(next_move, str) or (next_move and next_move not in appraisal.NEXT_MOVES):
            raise ValueError("appraisal_next_move_invalid")
        if dry_run:
            return {"result": "dry", "reason": "not_committed"}
        occurrence = _occurrence(subject, offer.reference)
        offered = {name: (head, revision) for name, head, revision in offer.heads}
        supersedes, revision = offered.get(subject, ("", 0))
        conn = cc.connect()
        try:
            topic_name = ""
            quoted_source_ref = ""
            if mode == "appraisal":
                # Replays of a pre-topic write keep the original scoped idempotency identity.
                legacy = "chat:" + scope.chat_id
                legacy_occ = _occurrence(legacy, offer.reference)
                if conn.execute("SELECT 1 FROM cognitive_events WHERE event_id=?",
                                (cc.event_id_for(scope, legacy_occ, legacy, operation),)).fetchone():
                    subject, occurrence = legacy, legacy_occ
                else:
                    from core.terminal.shadow import get_default_store

                    topic = appraisal.resolve_topic(conn, get_default_store(), scope, proof,
                                                    offer.quoted_external_id,
                                                    context_event_ids=offer.context_event_ids)
                    if topic.subject != subject:
                        return {"result": "conflict", "reason": "appraisal_topic_changed"}
                    topic_name = topic.name
                    if topic.quoted_subject:
                        quoted_source_ref = topic.evidence
            existing = conn.execute("SELECT supersedes FROM cognitive_events WHERE event_id=?",
                                    (cc.event_id_for(scope, occurrence, subject, operation),)).fetchone()
            if existing is not None:
                supersedes = str(existing[0] or "")
            from core.context import reflection
            common = dict(scope=scope, supersedes=supersedes, occurrence=occurrence,
                          expected_revision=revision, actor_key=str(proof.actor_person_id or proof.actor_account_id or ""),
                          source="experience_tool")
            if mode == "appraisal":
                result = appraisal.record_appraisal(conn, subject_key=subject, trigger_refs=(offer.reference,),
                    meaning=args["meaning"], topic_name=topic_name, feeling_text=str(args.get("feeling_text") or ""),
                    quoted_source_ref=quoted_source_ref, quoted_reply_ref=quoted_reply_ref,
                    tendency=str(args.get("tendency") or ""), next_move=str(next_move or ""),
                    unresolved_condition=str(args.get("unresolved_condition") or ""),
                    goal_refs=(goal,) if goal else (),
                    goal_occurrences=tuple(item for item in offer.goal_occurrences if item[0] == goal),
                    action_refs=tuple(str(ref) for ref in action_refs if str(ref).strip()), **common)
            else:
                result = reflection.record_lesson(conn, method=method, outcome=args["outcome"],
                    evidence_refs=(offer.reference,), lesson=args["lesson"], confidence=args.get("confidence", 0.5), **common)
            return {"result": "committed" if result.ok else "conflict", "duplicates": bool(result.duplicates),
                    "reasons": list(result.rejected)}
        finally:
            conn.close()
    except sqlite3.IntegrityError:
        return {"result": "conflict", "reason": "appraisal_source_already_bound"}
    except (ValueError, TypeError) as exc:
        return {"result": "rejected", "reason": str(exc)}
    except Exception as exc:
        warn("experience_commit_failed", scope=scope.key, err=repr(exc)[:160])
        return {"result": "error", "reason": "experience_commit_unavailable"}
