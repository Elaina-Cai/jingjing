"""目标到复核点的持久化契约；只产生候选，不执行动作或完成目标。

读取不消费；调用方真正复核后用候选 token 确认。到期与复核分别记账，
同一个已处理版本重启后不再触发，新版本或新复核点可再次触发。

唤醒来源除时间外还包括：
- ``goal_signal`` 事件（等待条件满足 / 新证据 / 动作回执变化）；
- 目标 ``evidence_refs`` 命中的事实事件；
- ``blocked_on`` / ``condition`` 命中的位置或事实事件。

所有来源都用「目标本次打开序号 + 目标版本 + 事件稳定身份」去重；读取只算候选，
只有真正复核并成功持久化 ``goal_reviewed`` 才消费。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping

from core.context.cognitive_commit import _scope_identity
from core.context.goal_source import goal_source_state
from core.context.situation import WorldModel, open_intentions
from safelog import warn

if TYPE_CHECKING:
    from core.context.situation import WorldEvent

_SIGNAL_KIND = "goal_signal"
_SIGNAL_SOURCES = {"condition", "evidence", "receipt"}
_EVIDENCE_KINDS = {
    # D-3：每步完成后补记的过程证据（artifact:/queue:），与 fact_asserted 同义「有新凭据」。
    "intention_evidence",
    "fact_asserted",
    "hypothesis_confirmed",
    "hypothesis_contradicted",
    "fact_revoked",
    "position_observed",
}


@dataclass(frozen=True)
class GoalWake:
    scope: tuple[str, str, str]
    goal_id: str
    at: str
    reason: str
    token: str
    event_id: str = ""
    evidence_state: str = ""
    receipt_ref: str = ""
    receipt_outcome: str = ""
    receipt_detail: str = ""


def _time(value: str) -> datetime | None:
    try:
        result = datetime.fromisoformat(value)
        # 世界模型使用本地墙钟；混合时区不能靠字符串排序猜先后。
        return result if result.tzinfo is None else None
    except (ValueError, TypeError):
        return None


def _hash(values: Any) -> str:
    raw = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _event_id(event: Any) -> str:
    return _hash([event.kind, event.at, event.payload])


def goal_occurrence(model: WorldModel, key: str) -> int:
    return sum(
        1
        for event in model.events
        if event.kind == "intention_opened" and str(event.payload.get("key") or "") == key
    )


def _open_index(model: WorldModel, key: str) -> int:
    found = -1
    for index, event in enumerate(model.events):
        if event.kind == "intention_opened" and str(event.payload.get("key") or "") == key:
            found = index
    return found


def _refs_of(event: Any) -> set[str]:
    raw = event.payload.get("evidence_refs") or ()
    if isinstance(raw, str):
        raw = (raw,)
    refs = {str(item) for item in raw if str(item)}
    one = str(event.payload.get("evidence_ref") or "").strip()
    if one:
        refs.add(one)
    return refs


def note_goal_signal(
    model: WorldModel,
    goal_id: str,
    *,
    source: str,
    ref: str,
    at: str = "",
    detail: str = "",
    receipt_outcome: str = "",
) -> bool:
    """记录一条目标唤醒信号；返回 False 表示目标不在办或同一信号已记过。

    这是写入侧契约：调用方只在确实观察到条件/证据/回执变化时调用。读取侧
    仍由 :func:`pending_wakes` 计算候选，二者不共享可变状态。
    """
    key = str(goal_id or "").strip()
    reference = str(ref or "").strip()
    if not key or not reference:
        return False
    if key not in {goal.key for goal in open_intentions(model)}:
        return False
    reason = str(source or "").strip()
    if reason not in _SIGNAL_SOURCES:
        reason = "evidence"
    occurrence = goal_occurrence(model, key)
    signal_id = _hash([key, reason, reference, occurrence])
    if any(
        event.kind == _SIGNAL_KIND and event.payload.get("signal_id") == signal_id
        for event in model.events
    ):
        return False
    model.add(
        _SIGNAL_KIND,
        {
            "key": key,
            "source": reason,
            "ref": reference,
            "signal_id": signal_id,
            "detail": str(detail or "")[:200],
            **({"receipt_outcome": str(receipt_outcome)} if reason == "receipt" and receipt_outcome else {}),
        },
        at=str(at or ""),
    )
    return True


def receipt_signal(
    model: WorldModel, goal_id: str, *, signal_id: str = ""
) -> WorldEvent | None:
    """返回本目标当前打开序号之后的已持久化回执信号。

    不带 ``signal_id`` 时取事件时间最新的回执；同时间按事件顺序取后者。带
    ``signal_id`` 时只精确匹配那一条，供提交前复核稳定身份。
    """
    key = str(goal_id or "").strip()
    if not key:
        return None
    start = _open_index(model, key)
    if start < 0:
        return None
    wanted = str(signal_id or "").strip()
    found = None
    found_at = None
    for event in model.events[start + 1:]:
        if event.kind != _SIGNAL_KIND:
            continue
        payload = event.payload or {}
        if str(payload.get("key") or "") != key:
            continue
        if str(payload.get("source") or "") != "receipt":
            continue
        stored = str(payload.get("signal_id") or "").strip() or _event_id(event)
        if wanted and stored != wanted:
            continue
        at = _time(str(event.at or "").strip())
        if at is None:
            continue
        if wanted:
            return event
        if found is None or at >= found_at:
            found = event
            found_at = at
    return found


def receipt_evidence_ref(event: WorldEvent) -> str:
    """回执事件的稳定证据编号；沿用载荷 signal_id，缺失时用事件身份。"""
    signal_id = str((event.payload or {}).get("signal_id") or "").strip()
    return "goal-receipt:" + (signal_id or _event_id(event))


def pending_wakes(model: WorldModel) -> tuple[GoalWake, ...]:
    identity = _scope_identity(model.scope)
    if identity is None:
        return ()
    handled = {
        event.payload.get("token")
        for event in model.events
        if event.kind == "goal_reviewed"
    }
    found: dict[str, GoalWake] = {}
    for goal in open_intentions(model):
        if goal.state in {"proposed", "paused", "completed", "abandoned"}:
            continue
        evidence_state = goal_source_state(goal, model.scope)
        if evidence_state == "dead":
            continue
        occurrence = goal_occurrence(model, goal.key)

        # 时间源：显式复核时间优先；否则到期也只要求复核，不宣称已完成。
        at = goal.review_at or goal.due_at
        if at and _time(at) is not None:
            reason = "review" if goal.review_at else "due"
            raw = json.dumps(
                [identity, asdict(goal), reason, occurrence],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            token = hashlib.sha256(raw.encode()).hexdigest()
            if token not in handled:
                found.setdefault(
                    token,
                    GoalWake(
                        scope=identity,
                        goal_id=goal.key,
                        at=at,
                        reason=reason,
                        token=token,
                        evidence_state=evidence_state,
                    ),
                )
        elif at:
            warn("goal_wake_invalid_time", scope=identity, goal=goal.key, at=at)

        # 事件源：只看本次打开之后的事件，旧版本/旧打开的信号不会复活。
        start = _open_index(model, goal.key) + 1
        for event in model.events[start:]:
            reason = ""
            event_key = str(event.payload.get("key") or "")
            if event.kind == _SIGNAL_KIND:
                if event_key != goal.key:
                    continue
                reason = str(event.payload.get("source") or "evidence")
                if reason not in _SIGNAL_SOURCES:
                    reason = "evidence"
                signal_id = str(event.payload.get("signal_id") or "").strip() or _event_id(event)
            elif event.kind in _EVIDENCE_KINDS:
                if _refs_of(event) & set(goal.evidence_refs):
                    reason = "evidence"
                elif event_key and event_key in {goal.blocked_on, goal.condition}:
                    reason = "condition"
                if not reason:
                    continue
                signal_id = _event_id(event)
            else:
                continue

            event_at = str(event.at or "").strip()
            wake_at = event_at or goal.review_at or goal.due_at
            if not wake_at:
                continue
            if _time(wake_at) is None:
                warn(
                    "goal_wake_invalid_time",
                    scope=identity,
                    goal=goal.key,
                    at=wake_at,
                    event=signal_id[:16],
                )
                continue
            token = _hash(
                [identity, goal.key, goal.revision, reason, occurrence, signal_id]
            )
            if token in handled:
                continue
            found.setdefault(
                token,
                GoalWake(
                    scope=identity,
                    goal_id=goal.key,
                    at=wake_at,
                    reason=reason,
                    token=token,
                    event_id=signal_id,
                    evidence_state=evidence_state,
                    receipt_ref=str(event.payload.get("ref") or "") if reason == "receipt" else "",
                    receipt_outcome=str(event.payload.get("receipt_outcome") or "") if reason == "receipt" else "",
                    receipt_detail=str(event.payload.get("detail") or "") if reason == "receipt" else "",
                ),
            )
    return tuple(
        sorted(
            found.values(),
            key=lambda item: (_time(item.at), item.scope, item.goal_id, item.reason, item.event_id),
        )
    )


def next_wake(models: Mapping[object, WorldModel]) -> GoalWake | None:
    """纯读取：计算最近待处理复核点，包括尚未到期的时间。"""
    candidates = [item for model in models.values() for item in pending_wakes(model)]
    return min(candidates, key=lambda item: (_time(item.at), item.scope, item.goal_id)) if candidates else None


def due_wakes(models: Mapping[object, WorldModel], *, now: str) -> tuple[GoalWake, ...]:
    stamp = _time(now)
    if stamp is None:
        raise ValueError("invalid_wake_now")
    candidates = [
        item
        for model in models.values()
        for item in pending_wakes(model)
        if _time(item.at) <= stamp
    ]
    return tuple(sorted(candidates, key=lambda item: (_time(item.at), item.scope, item.goal_id)))


def acknowledge_reviews(candidates: list[GoalWake], *, at: str, conclusion: str = "") -> int:
    """一次复核确认同一目标同一作用域的多个候选；过期、未到时间、写盘失败均不消费。

    与世界模型仓储共用进程锁，全部事件构造完成后只持久化一次；失败撤回本次插入。
    不提供跨进程文件事务保证，调度消费者应沿用现有单进程串行模型。
    """
    from core.context import situation_store as store

    if _time(at) is None:
        return 0
    unique = tuple(dict.fromkeys(candidates))
    if not unique:
        return 0
    scope = unique[0].scope
    goal_id = unique[0].goal_id
    if any(item.scope != scope or item.goal_id != goal_id for item in unique):
        return 0
    with store._LOCK:
        model = store.get_world_model(scope)
        store._apply_cognitive_closures(model, scope[2])
        due = set(due_wakes({scope: model}, now=at))
        if not due.issuperset(unique):
            return 0
        inserted = []
        for candidate in unique:
            model.add(
                "goal_reviewed",
                {
                    "key": candidate.goal_id,
                    "token": candidate.token,
                    "conclusion": str(conclusion),
                },
                at=at,
            )
            inserted.append(model.events[-1])
        try:
            if store.persist_world_model(scope):
                return len(unique)
        except Exception as exc:
            warn(
                "goal_review_save_failed",
                scope=scope,
                goal=goal_id,
                err=repr(exc)[:120],
            )
        for event in inserted:
            if event in model.events:
                model.events.remove(event)
        warn("goal_review_not_persisted", scope=scope, goal=goal_id)
        return 0


def acknowledge_review(candidate: GoalWake, *, at: str, conclusion: str = "") -> bool:
    """单候选兼容包装；见 :func:`acknowledge_reviews`。"""
    return acknowledge_reviews([candidate], at=at, conclusion=conclusion) > 0
