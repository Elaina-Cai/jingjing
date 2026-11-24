"""Promise -> native job -> durable queue -> GUI local evidence -> feedback -> goal close.

Only the model, contact lookup and keyboard/queue transport boundary are substituted.
"""
from datetime import datetime
from types import SimpleNamespace
import asyncio
import sqlite3

import pytest
from core.terminal.event_model import Event, EventType
from core.terminal.principal import VisibilityScope
from core.terminal import shadow
from core.context import situation_store, cognitive_commit as cc
from core.context.situation import open_intentions
from core.context.goal_close import commit_close
from core.context.goal_links import ensure_schema
from core.contracts.plan import Job, Step
from core.jobs.store import JobStore, ensure_schema as job_schema
from core.jobs.runner import advance
from loop import job_bridge
from db import world_db as world
from db.repo_feedback import FeedbackRepository
from core.quality.feedback import record_feedback_signals
from test_wechat_e2e_chain import db
from test_appraisal_prompt_integration import runtime, Model, drive
from test_gui_local_observation import isolated_gui, make_account


@pytest.mark.parametrize("observed",[True,False])
def test_promise_job_local_send_feedback_and_restart(runtime,db,isolated_gui,monkeypatch,observed):
    from loop import ports
    from core.execution.gui import wechat_send as gui
    brain,tmp=runtime
    path=make_account(tmp,"a")
    monkeypatch.setattr(world,"PATH",str(tmp/"transport.db"))
    monkeypatch.setattr(world,"_READY",[False])
    monkeypatch.setattr(ports,"resolve_chat_id",lambda ref:"same" if ref=="same" else None)
    monkeypatch.setattr(ports,"resolve_say_target",lambda target:("same",""))
    source=db.store.append(Event.new(event_type=EventType.MESSAGE,platform="wechat",bot_account_id="a",
        chat_id="same",actor_person_id="person-a",external_msg_id="request",content="稍后提醒我",
        visibility_scope=frozenset({VisibilityScope.OWNER_ONLY})))
    monkeypatch.setattr(shadow,"get_default_store",lambda:db.store)
    model=Model([{"tool_calls":[{"id":"promise","name":"promise","arguments":{"what":"稍后提醒我","who":"same"}}]},
                 {"tool_calls":[],"content":""}])
    receipts=drive(brain.run_loop(str(path),source.content,llm=model,dry_run=False,platform="wechat",
        chat_id="same",sender_id="person-a",focus="same",visible_event_ids=(source.event_id,),max_steps=2))
    assert next(r for r in receipts if r.get("act")=="promise")["result"]=="ok"
    scope=cc.Scope("wechat","a","same")
    wm=situation_store.get_world_model("same",scope=scope)
    goal=open_intentions(wm,kind="promise")[0]
    assert goal.evidence=="稍后提醒我"
    job_schema(db.engine);ensure_schema(db.engine)
    store=JobStore(db.engine,mode="on")
    job=Job("joint-job","same","排队提醒",goal_id=goal.key,steps=[Step(1,"send:same|same",effects="external_send")])
    store.create(job)
    queued=[]
    async def enqueue_only(bubbles,**kwargs):
        ids=world.enqueue("joint-job", "same",[{"kind":"text","text":"same"}],account_id="a",turn_id="job-joint")
        queued.extend(ids)
        return SimpleNamespace(ok=True,data={"ack":"queued","queue_ids":ids})
    monkeypatch.setattr(ports,"say_many",enqueue_only)
    out=job_bridge.advance_once("same",store=store)
    assert out["stop"]=="done" and queued
    assert any(e.kind=="goal_signal" for e in wm.events)
    # Execution-instance completion is not completion of the original promise.
    assert any(g.key==goal.key for g in open_intentions(wm))
    item=world.claim_next(owner="isolated-test")
    presses=[]
    def press():
        presses.append(1)
        if observed:
            with sqlite3.connect(path) as conn:
                conn.execute("INSERT INTO messages VALUES(2,'same','same',1)")
        return True
    receipt=gui.send_and_verify("same","same",account_id="a",db_path=path,
        deps=SimpleNamespace(press_enter=press),polls=1)
    assert presses==[1] and receipt.status=="unconfirmed"
    assert bool(receipt.local_id)==observed
    # Unknown delivery stays unverified; no second physical attempt after restart.
    world.settle(item["id"],False,ack="unconfirmed",why="local observation only",owner="isolated-test",attempt_no=item["attempts"],not_executed=False)
    situation_store.reset_world_models()
    restarted=situation_store.get_world_model("same",scope=scope)
    assert any(g.key==goal.key for g in open_intentions(restarted))
    assert not job_bridge.advance_once("same",store=JobStore(db.engine,mode="on"))["ran"]
    assert presses==[1]
    assert world.claim_next(owner="isolated-test") is None
    assert not commit_close(restarted,goal.key,evidence_ref="missing-proof",scope=scope,state="completed")
    feedback_event=db.store.append(Event.new(event_type=EventType.MESSAGE,platform="wechat",bot_account_id="a",
        chat_id="same",actor_person_id="person-a",external_msg_id="confirmation",content="收到了，这件事完成了",
        visibility_scope=frozenset({VisibilityScope.OWNER_ONLY})))
    assert commit_close(restarted,goal.key,evidence_ref=feedback_event.event_id,scope=scope,state="completed",source="conversation")
    assert commit_close(restarted,goal.key,evidence_ref=feedback_event.event_id,scope=scope,state="completed",source="conversation")
    situation_store.reset_world_models()
    assert not open_intentions(situation_store.get_world_model("same",scope=scope),kind="promise")
    conn=cc.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_events WHERE operation='close'").fetchone()[0]==1
    finally:conn.close()


def test_unscoped_promise_fails_before_writing_ledger(runtime,monkeypatch):
    brain,_=runtime
    monkeypatch.setattr(brain.ports,"resolve_chat_id",lambda _:"same")
    def forbidden(**kw):raise AssertionError("unverified scope must not write a promise")
    monkeypatch.setattr(brain.ledger,"record_promise",forbidden)
    _,receipt=drive(brain._exec("promise",{"what":"测试","who":"same"},db="unused",dry_run=False,turn_id="t"))
    assert receipt["result"]=="failed"


def test_goal_persistence_failure_is_partial_and_retry_still_requires_persistence(runtime,monkeypatch):
    from core.context.promise_goal import ensure_promise_goal
    import core.execution.data_channels as channels
    monkeypatch.setattr(channels,"save_world",lambda *a,**kw:False)
    scope=cc.Scope("wechat","a","same")
    for _ in range(2):
        with pytest.raises(OSError,match="promise_goal_persist_failed"):
            ensure_promise_goal("same",promise_id="same-id",what="必须保存",scope=scope)


def test_promise_tool_reports_partial_instead_of_success_if_checkpoint_fails(runtime,monkeypatch):
    from loop import ports
    import core.execution.data_channels as channels
    brain,_=runtime
    monkeypatch.setattr(ports,"resolve_chat_id",lambda _:"same")
    monkeypatch.setattr(channels,"save_world",lambda *a,**kw:False)
    token=ports._READ_SCOPE.set(ports.ReadScope("wechat","a","same","","person-a"))
    try:
        observation,receipt=drive(brain._exec("promise",{"who":"same","what":"稍后处理"},
            db="unused",dry_run=False,turn_id="partial-test"))
        assert receipt["result"]=="partial" and "目标保存失败" in observation
    finally:
        ports._READ_SCOPE.reset(token)
