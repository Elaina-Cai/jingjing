"""反馈样本的稳定处理身份与消费收据；源样本变化不由旧提案覆盖。

同一个 claim 模型服务多个反馈来源：
* ``correction``：memory_write 事件作收据（事件里带显式写入指纹）；
* ``roster_confirm``：system 事件作收据（效果写 person_notes，不写投影）；
两者共用版本哈希、数据库身份与 ``(platform, account, chat, person)`` 作用域。
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from os.path import normcase
from sqlalchemy import select
from sqlalchemy.orm import Session
from core.memory.legacy_audit import _hash
from db.meta_models import FeedbackSample, TerminalEvent


class FeedbackConflict(ValueError):
    pass


def database_key(engine):
    if engine.url.drivername.startswith("sqlite") and engine.url.database and engine.url.database != ":memory:":
        return normcase(str(Path(engine.url.database).resolve()))
    return "in-process:" + str(id(engine))


def sample_version(sample):
    return _hash({column.name:getattr(sample,column.name) for column in FeedbackSample.__table__.columns if column.name != "status"})


@dataclass(frozen=True)
class FeedbackClaim:
    sample_id: int
    version: str
    identity: tuple[str,str,str,str]
    created_at: datetime
    database: str
    namespace: str = "correction"

    @property
    def event_id(self):
        return "feedback-" + self.namespace + ":" + _hash([self.identity,self.sample_id])

    @property
    def binding(self):
        return {"sample_id":self.sample_id,"version":self.version,"identity":list(self.identity)}


def claim_feedback(sample, repository, *, source="correction", namespace="correction"):
    identity=tuple(str(getattr(sample,key,"") or "") for key in ("platform","bot_account_id","chat_id","person_id"))
    if not isinstance(sample,FeedbackSample) or sample.source != str(source) or not str(sample.sample_text or "").strip() or not all(identity) or not isinstance(sample.id,int) or not isinstance(sample.created_at,datetime) or sample.created_at.tzinfo is not None:
        raise FeedbackConflict("invalid_" + str(source) + "_feedback_source")
    return FeedbackClaim(sample.id,sample_version(sample),identity,sample.created_at,database_key(repository.read_engine()),str(namespace))


def check_feedback(session,claim,engine):
    if claim.database != database_key(engine):
        raise FeedbackConflict("feedback_database_conflict")
    row=session.get(FeedbackSample,claim.sample_id)
    if row is None or sample_version(row) != claim.version:
        raise FeedbackConflict("feedback_source_changed")
    return row


def matching_commit(session,claim,*,event_type="memory_write",source="user_correction"):
    event=session.execute(select(TerminalEvent).where(TerminalEvent.event_id==claim.event_id)).scalars().first()
    if event is None:return False
    import json
    try:
        metadata=json.loads(event.metadata_json or "{}")
    except (TypeError,ValueError) as exc:
        raise FeedbackConflict("feedback_commit_metadata_invalid") from exc
    if not isinstance(metadata,dict):
        raise FeedbackConflict("feedback_commit_metadata_invalid")
    if (event.event_type != str(event_type) or metadata.get("source") != str(source)
            or (str(event_type) == "memory_write" and not metadata.get("explicit_write_fingerprint"))
            or metadata.get("feedback") != claim.binding
            or (event.platform,event.bot_account_id,event.chat_id) != claim.identity[:3]
            or event.actor_person_id != claim.identity[3]):
        raise FeedbackConflict("feedback_commit_binding_conflict")
    return True


def recover_feedback(claim,repository,*,event_type="memory_write",source="user_correction"):
    with Session(repository.read_engine()) as session:
        row=check_feedback(session,claim,repository.read_engine())
        committed=matching_commit(session,claim,event_type=event_type,source=source)
        if row.status=="learned" and committed:return True
        if row.status!="new" or committed:
            raise FeedbackConflict("feedback_state_not_pending_or_unpaired")
        return False


def finish_feedback(claim,repository,status):
    if status not in {"reviewed","skipped_non_owner"}:
        raise ValueError("invalid_feedback_finish_status")
    with Session(repository.write_engine()) as session:
        connection=session.connection()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        from core.memory.legacy_write_freeze import assert_projection_writable
        assert_projection_writable(repository.read_engine(),connection=connection)
        row=check_feedback(session,claim,repository.read_engine())
        if row.status==status:return False
        if row.status!="new" or matching_commit(session,claim):
            raise FeedbackConflict("feedback_state_changed")
        row.status=status
        session.commit()
        return True
