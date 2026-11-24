"""旧库未切换时的显式记忆唯一落盘口：事件、投影、显式替代同库提交。"""
from __future__ import annotations
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session
from core.memory.legacy_audit import _hash
from core.memory.legacy_write_freeze import LegacyProjectionFrozen, assert_projection_writable
from core.terminal.event_model import EventType
from core.terminal.event_store_db import append_in_session, _event_from_row
from core.terminal.memory_projection import MemoryProjector
from db.meta_models import TerminalEvent, MemoryProjectionRow
from db.repo_memory_projections import _item_from_row


class ExplicitMemoryConflict(ValueError):
    pass


def remember_denial(principal, *, context=None, memory_type="semantic") -> str:
    from core.rules import engine as rules
    from core.memory.index_scope import context_contradicts
    if principal is None or not getattr(principal, "person_id", ""):
        return "无法确定当前身份，未写入长期记忆"
    if context_contradicts(context, principal) or not all(getattr(principal, key, "") for key in ("platform", "bot_account_id", "chat_id")):
        return "当前身份与会话范围不完整或不一致，未写入长期记忆"
    if not principal.is_owner:
        return "只有主人确认的信息才能写入长期记忆；当前发言人不是主人，不要把这句话当成事实记住"
    decision = rules.decide(rules.ActionContext(action_id="remember", actor_id=principal.person_id,
        chat_id=principal.chat_id, chat_type=str(getattr(principal.chat_type, "value", principal.chat_type)),
        platform=principal.platform, account_id=principal.bot_account_id,
        is_owner=principal.is_owner, is_group=principal.is_group, params={"memory_type":str(memory_type or "semantic")}))
    return "" if decision.allowed else decision.explanation


def projection_version(item) -> str:
    return _hash(item.as_dict())


def commit_explicit(event, *, store, repository, supersedes=(), feedback=None, receipt=None):
    frozen = False
    try:
        repository.assert_writable()
    except LegacyProjectionFrozen:
        # Background correction still needs the projection version check; the
        # frozen path here is for direct explicit remember writes.
        if feedback is not None:
            raise
        frozen = True
    other = getattr(store, "engine", None)
    engine = repository.write_engine()
    if other is not engine:
        if other is None or not engine.url.drivername.startswith("sqlite") or not other.url.drivername.startswith("sqlite") or not engine.url.database or not other.url.database or engine.url.database == ":memory:" or other.url.database == ":memory:" or Path(engine.url.database).resolve() != Path(other.url.database).resolve():
            raise ValueError("explicit_memory_requires_same_database")
    if event.event_type != EventType.MEMORY_WRITE or not event.event_id or not all((event.platform,event.bot_account_id,event.chat_id)) or not event.visibility_scope:
        raise ValueError("invalid_explicit_memory_event")
    if event.metadata.get("source") == "user_correction" and feedback is None:
        raise ValueError("correction_feedback_required")
    if feedback is not None:
        if event.event_id != feedback.event_id or (event.platform,event.bot_account_id,event.chat_id,event.actor_person_id) != feedback.identity or event.occurred_at != feedback.created_at:
            raise ValueError("correction_event_feedback_mismatch")
        event.metadata = {**event.metadata,"feedback":feedback.binding}
    plan = event.as_dict()
    plan.pop("sequence",None)
    plan["metadata"] = {key:value for key,value in plan["metadata"].items() if key != "explicit_write_fingerprint"}
    plan["supersedes"] = list(supersedes)
    fingerprint = _hash(plan)
    event.metadata = {**event.metadata, "explicit_write_fingerprint":fingerprint}
    with Session(engine) as session:
        connection = session.connection()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            assert_projection_writable(engine,connection=connection)
            frozen = False
        except LegacyProjectionFrozen:
            if feedback is not None:
                raise
            frozen = True
        sample = None
        if feedback is not None:
            from core.memory.feedback_commit import check_feedback,matching_commit,FeedbackConflict
            sample = check_feedback(session,feedback,engine)
            if sample.status != "new" and not (sample.status == "learned" and matching_commit(session,feedback)):
                raise FeedbackConflict("feedback_state_changed")
        existed = session.execute(select(TerminalEvent.event_id).where(TerminalEvent.event_id==event.event_id)).first() is not None
        event.sequence = 0
        stored = append_in_session(session,event)
        row = session.execute(select(TerminalEvent).where(TerminalEvent.event_id==stored.event_id)).scalars().one()
        stored = _event_from_row(row)
        if stored.metadata.get("explicit_write_fingerprint") != fingerprint:
            raise ExplicitMemoryConflict("explicit_memory_event_identity_conflict")
        if frozen:
            from core.memory.terminal_explicit import memory_item_from_event
            result = memory_item_from_event(stored)
            session.commit()
            if receipt is not None: receipt["duplicate"] = existed
            return result
        item = MemoryProjector.project_event(stored)
        if item is None:
            raise ValueError("explicit_memory_not_projectable")
        if existed:
            current = session.execute(select(MemoryProjectionRow).where(MemoryProjectionRow.memory_id==item.memory_id)).scalars().first()
            if current is not None:
                result = _item_from_row(current)
                if sample is not None:
                    sample.status = "learned"
                    session.commit()
                if receipt is not None: receipt["duplicate"] = True
                return result  # 重复响应不重写已经关闭或修订的投影。
        for identifier, expected in (() if existed else supersedes):
            old = session.execute(select(MemoryProjectionRow).where(MemoryProjectionRow.memory_id==identifier)).scalars().first()
            previous = _item_from_row(old) if old is not None else None
            if previous is None or (previous.platform,previous.bot_account_id,str(previous.metadata.get("chat_id") or "")) != (event.platform,event.bot_account_id,event.chat_id):
                raise ExplicitMemoryConflict("explicit_memory_supersedes_scope_conflict")
            if frozenset(previous.visibility_scope) != frozenset(item.visibility_scope) or previous.sensitivity != item.sensitivity:
                raise ExplicitMemoryConflict("explicit_memory_supersedes_visibility_conflict")
            if projection_version(previous) != expected:
                raise ExplicitMemoryConflict("explicit_memory_supersedes_version_conflict")
            if old.valid_to is None:
                old.valid_to = stored.occurred_at
        repository.upsert_in_session(session,item)
        if sample is not None: sample.status = "learned"
        session.commit()
        if receipt is not None: receipt["duplicate"] = existed
        return item
