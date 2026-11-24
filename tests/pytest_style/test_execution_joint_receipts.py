"""Real task receipt transaction and goal signals against one isolated SQLite DB."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from core import features
from core.tasks import store as tasks
from core.context import situation_store
from core.context.situation import open_intention
from core.context.cognitive_commit import Scope
from core.context.goal_links import ensure_schema, link_goal
from db.meta_models import WorkItemRow, WorkItemRunRow
from db.models import Reminder
from test_wechat_e2e_chain import db


@pytest.fixture()
def task(db, tmp_path, monkeypatch):
    for key in (features.WORK_ITEM_LEDGER, features.WORK_ITEM_RUNS):
        monkeypatch.setenv(features.env_key(key), "on")
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    situation_store.reset_world_models()
    WorkItemRow.__table__.create(db.engine)
    WorkItemRunRow.__table__.create(db.engine)
    ensure_schema(db.engine)
    scope=Scope("wechat","a","same")
    model=situation_store.get_world_model("same",scope=scope)
    open_intention(model,"goal-1",kind="act",at="2026-11-08 12:00:00")
    assert situation_store.persist_world_model("same",scope=scope)
    row=Reminder(persona_id=7, content="提醒资料",remind_at=datetime(2026,11,8,12),status="pending",
        repeat_rule="daily",source_platform="wechat",source_account_id="a",source_chat_id="same",
        source_chat_type="private",requester_id="person-a",requester_name="A",requester_is_owner=1,
        purpose="remind",attempts=0,created_at=datetime(2026,11,8,11))
    row.id=11
    repo=SimpleNamespace(meta_engine=db.engine)
    wid=tasks.record_created(repo,row)
    assert wid
    # The native event supplies scope; the normalized association supplies goal identity.
    link_goal(db.engine,subject_kind="work_item",subject_id=wid,goal_id="goal-1")
    run=tasks.record_run_started(repo,work_item_id=wid,now=datetime(2026,11,8,12))
    assert run
    yield SimpleNamespace(db=db,repo=repo,wid=wid,run=run,scope=scope)
    situation_store.reset_world_models()


def finish(task, **kw):
    return tasks.record_run_finished(task.repo,task.run,outcome="sent",reply_text="提醒资料",
        repeat_continues=True,repeat_due_at=datetime(2026,11,9,12),**kw)


def counts(task):
    with task.db.engine.connect() as conn:
        return (conn.execute(text("SELECT occurrences_done FROM work_items")).scalar(),
                conn.execute(text("SELECT COUNT(*) FROM terminal_events WHERE event_type='work_item_run_finished'")).scalar())


def test_receipt_replay_after_restart_is_one_event_one_effect_and_recovers_wake(task, monkeypatch):
    from core.context import goal_receipts
    from core.context.goal_wake import pending_wakes
    original=goal_receipts.emit_work_item_receipt
    with monkeypatch.context() as patch:
        patch.setattr(goal_receipts,"emit_work_item_receipt",lambda *a,**kw:False)
        first=finish(task)
    assert counts(task)==(1,1)
    situation_store.reset_world_models()
    second=finish(task,now=datetime(2026,11,8,13))
    assert first==second
    assert counts(task)==(1,1)
    model=situation_store.get_world_model("same",scope=task.scope)
    signals=[e for e in model.events if e.kind=="goal_signal"]
    assert len(signals)==1 and task.run["run_id"] in signals[0].payload["ref"]
    wake, = pending_wakes(model)
    assert (wake.receipt_ref, wake.receipt_outcome, wake.receipt_detail) == (
        signals[0].payload["ref"], "sent", "提醒资料",
    )


def test_two_receipt_consumers_cannot_double_apply(task):
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies=list(pool.map(lambda _:finish(task),range(2)))
    assert replies[0]==replies[1] and counts(task)==(1,1)


def test_same_run_different_result_is_conflict_without_rewriting_history(task):
    first=finish(task)
    with pytest.raises(ValueError,match="receipt_conflict"):
        tasks.record_run_finished(task.repo,task.run,outcome="failed",error_message="late conflicting result")
    assert counts(task)==(1,1)
    assert first["outcome"]=="sent"


def test_late_receipt_preserves_fact_without_reviving_cancelled_task(task):
    with Session(task.db.engine) as session:
        item=session.query(WorkItemRow).filter_by(work_item_id=task.wid).one()
        item.status="cancelled"
        session.commit()
    finish(task)
    with Session(task.db.engine) as session:
        item=session.query(WorkItemRow).filter_by(work_item_id=task.wid).one()
        assert item.status=="cancelled" and item.occurrences_done==0
    assert counts(task)==(0,1)


@pytest.mark.parametrize("raises",[False,True])
def test_failed_goal_signal_checkpoint_is_not_consumed_in_memory(task,monkeypatch,raises):
    from core.context import goal_receipts
    def fail(*a,**kw):
        if raises:raise OSError("checkpoint unavailable")
        return False
    with monkeypatch.context() as patch:
        patch.setattr(goal_receipts,"persist_world_model",fail)
        finish(task)
        model=situation_store.get_world_model("same",scope=task.scope)
        assert not [e for e in model.events if e.kind=="goal_signal"]
    finish(task)
    model=situation_store.get_world_model("same",scope=task.scope)
    assert len([e for e in model.events if e.kind=="goal_signal"])==1
    assert counts(task)==(1,1)


def test_failed_run_wake_keeps_error_message_when_no_reply_text(task):
    from core.context.goal_wake import pending_wakes

    result = tasks.record_run_finished(task.repo, task.run, outcome="failed", reply_text="",
                                       error_message="目标会话不存在")
    assert result["outcome"] == "failed"
    situation_store.reset_world_models()
    model = situation_store.get_world_model("same", scope=task.scope)
    wake, = pending_wakes(model)
    assert wake.receipt_outcome == "failed"
    assert "目标会话不存在" in wake.receipt_detail
