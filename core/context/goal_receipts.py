"""把 work_item 执行回执转成目标唤醒信号（best-effort，不阻断任务收尾）。"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

from sqlalchemy import text

from core.context.cognitive_commit import Scope
from core.context.goal_links import goal_for
from core.context.goal_wake import note_goal_signal
from core.context.situation import open_intentions
from core.context.situation_store import (
    all_world_models,
    get_world_model,
    persist_world_model,
)
from safelog import warn

_CREATED = "work_item_created"


def _event_payload(engine: Any, work_item_id: str) -> dict[str, Any] | None:
    sql = (
        "SELECT action_payload_json, platform, bot_account_id, chat_id, chat_type, occurred_at "
        "FROM terminal_events "
        "WHERE event_type=:kind AND json_extract(action_payload_json, '$.work_item_id')=:wid "
        "ORDER BY id DESC LIMIT 1"
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(text(sql), {"kind": _CREATED, "wid": work_item_id}).mappings().first()
        if row:
            payload = json.loads(row["action_payload_json"] or "{}")
            return {
                "goal_id": str(payload.get("goal_id") or ""),
                "platform": str(row["platform"] or ""),
                "account_id": str(row["bot_account_id"] or ""),
                "chat_id": str(row["chat_id"] or ""),
                "chat_type": str(row["chat_type"] or "private"),
                "at": str(row["occurred_at"] or ""),
            }
    except Exception as exc:
        # JSON1 不可用或表结构异常时退回扫描最近事件。
        log.warning(
            "goal_receipt_json1_unavailable",
            extra={"err": repr(exc)[:160]},
        )
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT action_payload_json, platform, bot_account_id, chat_id, chat_type, occurred_at "
                    "FROM terminal_events WHERE event_type=:kind ORDER BY id DESC LIMIT 500"
                ),
                {"kind": _CREATED},
            ).mappings()
            for row in rows:
                try:
                    payload = json.loads(row["action_payload_json"] or "{}")
                except ValueError as exc:
                    log.warning(
                        "goal_receipt_bad_payload",
                        extra={"err": repr(exc)[:160]},
                    )
                    continue
                if str(payload.get("work_item_id") or "") != work_item_id:
                    continue
                return {
                    "goal_id": str(payload.get("goal_id") or ""),
                    "platform": str(row["platform"] or ""),
                    "account_id": str(row["bot_account_id"] or ""),
                    "chat_id": str(row["chat_id"] or ""),
                    "chat_type": str(row["chat_type"] or "private"),
                    "at": str(row["occurred_at"] or ""),
                }
    except Exception as exc:
        log.warning(
            "goal_receipt_event_lookup_failed",
            extra={"err": repr(exc)[:160]},
        )
        return None
    return None


def _persist_receipt_signal(scope, goal_id, ref, at, detail, *, outcome):
    """Failed persistence cannot leave an in-memory consumed signal behind."""
    from core.context.situation_store import _LOCK
    with _LOCK:
        model = get_world_model(scope.chat_id, scope=scope)
        before = list(model.events)
        if not note_goal_signal(model, goal_id, source="receipt", ref=ref,
                                at=str(at or ""), detail=detail, receipt_outcome=outcome):
            return False
        try:
            saved = persist_world_model(scope.chat_id, scope=scope)
        except Exception:
            model.events[:] = before
            raise
        if not saved:
            model.events[:] = before
        return bool(saved)


def work_item_goal(engine: Any, work_item_id: str) -> tuple[str, dict[str, Any]] | None:
    """关联表提供目标身份；原生创建事件提供不可猜测的完整会话坐标。"""
    wid = str(work_item_id or "").strip()
    if not wid:
        return None
    goal = goal_for(engine, subject_kind="work_item", subject_id=wid)
    payload = _event_payload(engine, wid)
    if not payload:
        return None
    original = str(payload.get("goal_id") or "")
    if goal and original and goal != original:
        log.warning("work_item_goal_link_conflict", extra={"work_item_id": wid})
        return None
    goal = goal or original
    return (goal, payload) if goal else None


def emit_work_item_receipt(
    engine: Any,
    *,
    work_item_id: str,
    run_id: str,
    outcome: str,
    at: str,
    detail: str = "",
) -> bool:
    """写一条 ``goal_signal(source=receipt)``；无关联、目标关闭或重复时不写。"""
    linked = work_item_goal(engine, work_item_id)
    if not linked:
        return False
    goal_id, meta = linked
    scope = Scope(
        platform=str(meta.get("platform") or ""),
        account_id=str(meta.get("account_id") or ""),
        chat_id=str(meta.get("chat_id") or ""),
        chat_type=str(meta.get("chat_type") or "private"),
    )
    if not scope.complete:
        return False
    try:
        ref = (
            f"work_item:{run_id}:{outcome}"
            if str(run_id or "").strip()
            else f"work_item:{work_item_id}:{outcome}:{at}"
        )
        return _persist_receipt_signal(scope, goal_id, ref, at, detail, outcome=outcome)
    except Exception as exc:  # noqa: BLE001 - 回执是 best-effort，不能阻断任务收尾
        warn("work_item_goal_signal_failed", work_item_id=str(work_item_id), error=repr(exc)[:160])
        return False


def job_goal(engine: Any, job_id: str) -> tuple[str, dict[str, Any]] | None:
    """查 job 的 Intention 关联；缺失时退回 job_events.created。"""
    jid = str(job_id or "").strip()
    if not jid:
        return None
    meta: dict[str, Any] = {}
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT payload, chat_id, ts FROM job_events "
                    "WHERE job_id=:j AND kind='created' ORDER BY seq LIMIT 1"
                ),
                {"j": jid},
            ).mappings().first()
    except Exception as exc:
        log.warning(
            "goal_receipt_job_event_failed",
            extra={"err": repr(exc)[:160]},
        )
        row = None
    if row:
        meta = {"chat_id": str(row["chat_id"] or ""), "at": str(row["ts"] or "")}
    goal = goal_for(engine, subject_kind="job", subject_id=jid)
    if goal:
        return goal, meta
    if not row:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        return None
    goal_id = str(payload.get("goal_id") or "")
    return (goal_id, meta) if goal_id else None


def _job_scope(chat_id: str, goal_id: str) -> Scope | None:
    """job_events 只有 chat_id；只在该会话下唯一匹配到一个目标时采用。"""
    matches: list[Scope] = []
    for model in all_world_models().values():
        scope = dict(getattr(model, "scope", {}) or {})
        if str(scope.get("chat_id") or "") != chat_id:
            continue
        if not any(goal.key == goal_id for goal in open_intentions(model)):
            continue
        candidate = Scope(
            platform=str(scope.get("platform") or ""),
            account_id=str(scope.get("account_id") or ""),
            chat_id=chat_id,
            chat_type=str(scope.get("chat_type") or "private"),
        )
        if candidate.complete:
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def emit_job_receipt(
    engine: Any,
    *,
    job_id: str,
    outcome: str,
    at: str,
    detail: str = "",
) -> bool:
    """写一条 job 完成/失败回执；没有唯一 scope 时安全跳过。"""
    linked = job_goal(engine, job_id)
    if not linked:
        return False
    goal_id, meta = linked
    scope = _job_scope(str(meta.get("chat_id") or ""), goal_id)
    if scope is None:
        return False
    try:
        return _persist_receipt_signal(scope, goal_id, f"job:{job_id}:{outcome}", at, detail, outcome=outcome)
    except Exception as exc:  # noqa: BLE001 - best-effort
        warn("job_goal_signal_failed", job_id=str(job_id), error=repr(exc)[:160])
        return False
