# -*- coding: utf-8 -*-
"""D-2：取消要挡住实际动作，不是只改一行状态。

三种情形分别钉住：

1. 台账里这件事已经被叫停，而这一拍手里还是取消前读出来的那一行；
2. 台账行看着还开着，但它关联的目标已经关闭（主人说不用了）；
3. 第一步刚跑完、第二步之前被取消——已经发生的事实算数，后面的动作不许再发。

正向对照同时在场：目标还开着时，动作照常执行。否则「挡住了」可能只是执行器没接上。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from core.context import situation
from core.context.situation_store import get_world_model, reset_world_models
from core.contracts.plan import Job, Step
from core.execution.data_channels import get_world, save_world
from loop import job_bridge

SCOPE = {"platform": "wechat", "account_id": "bot", "chat_id": "c1", "chat_type": "private"}
GOAL = "整理两篇资料"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    reset_world_models()
    yield
    reset_world_models()


def _goal(state: str = "", *, evidence_refs=()) -> None:
    model = get_world("c1", scope=SCOPE)
    situation.open_intention(
        model, GOAL, kind="act", at="2026-11-18 09:00",
        evidence="你帮我整理两篇资料",
        desired_outcome="两篇资料都整理好、各自保存",
        done_when="两份独立结果都符合条件且已保存",
        next_action="先找第一篇", review_at="2026-11-18 10:00",
        scope="chat:c1", origin="owner_request",
        evidence_refs=evidence_refs,
    )
    if state:
        situation.close_intention(model, GOAL, at="2026-11-18 09:30",
                                  result="他说不用了", state=state)
    assert save_world("c1", scope=model.scope)


class _Store:
    """只实现 advance / advance_once 真正用到的那几个方法。"""

    def __init__(self, job: Job):
        self.job = job
        self.appended: list[tuple[str, str, dict]] = []

    def open_jobs(self, chat_id: str):
        # 模拟「这一拍开始时读到的那一页」：取消发生在读之后，这里仍是旧快照。
        return [self.job]

    def load(self, job_id: str):
        return self.job

    def append(self, job_id: str, kind: str, *, payload=None, actor=""):
        self.appended.append((job_id, kind, dict(payload or {})))

    def is_approved(self, job_id: str, step_n: int, action_hash: str) -> bool:
        return False


def _job(goal_id: str = "", steps: int = 1) -> Job:
    return Job(
        job_id="j1", chat_id="c1", goal=GOAL, goal_id=goal_id,
        steps=[Step(n=i + 1, action="send:群|第%d篇" % (i + 1)) for i in range(steps)],
    )


class _Spy:
    def __init__(self, *, on_call=None):
        self.calls: list[str] = []
        self.on_call = on_call

    def __call__(self, step):
        self.calls.append(step.action)
        if self.on_call is not None:
            self.on_call(step)
        return {"ok": True, "detail": "done"}


def test_cancelled_row_blocks_action_even_though_turn_loaded_it_earlier():
    """这一拍手里是取消前的那一行；取消已经落盘，动手前必须复查。"""
    job = _job()
    store = _Store(replace(job, state="cancelled"))
    spy = _Spy()

    out = job_bridge.advance_once("c1", store=store, execute=spy)

    assert spy.calls == [], "已取消的事不得再发任何动作"
    assert out["stop"] == "state"
    assert not [row for row in store.appended if row[1] == "step_done"], "不许留下办完的假账"


def test_closed_goal_blocks_action_even_when_job_row_is_still_open():
    """目标被主人取消（abandoned），台账行还没跟着关——执行前复查目标状态。"""
    _goal(state="abandoned")
    job = _job(goal_id=GOAL)
    store = _Store(job)
    spy = _Spy()

    out = job_bridge.advance_once("c1", store=store, execute=spy)

    assert spy.calls == [], "目标已取消，动作不许提交"
    assert out["stop"] == "goal_closed"


def test_open_goal_still_executes():
    """正向对照：闸门只挡已取消，不能把正常的推进也挡掉。"""
    _goal()
    job = _job(goal_id=GOAL)
    store = _Store(job)
    spy = _Spy()

    out = job_bridge.advance_once("c1", store=store, execute=spy)

    assert spy.calls == ["send:群|第1篇"]
    assert out["stop"] in ("done", "safe_point")


def test_closed_job_leaves_the_next_job_visible_and_runnable(monkeypatch):
    _goal(state="abandoned")
    stopped = _job(GOAL, steps=2)
    next_job = Job(job_id="j2", chat_id="c1", goal="核对日期",
                   steps=[Step(n=1, action="send:群|日期已核对")])
    queue = _Store(stopped)
    monkeypatch.setattr(queue, "open_jobs", lambda chat_id: [stopped, next_job])
    monkeypatch.setattr(queue, "load", {stopped.job_id: stopped, next_job.job_id: next_job}.get)
    spy = _Spy()

    reply_context = job_bridge.readout("c1", store=queue)
    advanced = job_bridge.advance_once("c1", store=queue, execute=spy, scope=SCOPE)
    assert "核对日期" in reply_context and GOAL not in reply_context
    assert advanced["job_id"] == "j2" and spy.calls == ["send:群|日期已核对"]


@pytest.mark.parametrize("mode", ["requester", "other-speaker", "temporary", "unknown-quote", "question", "before-jobs"])
def test_quoted_cancellation_changes_the_next_real_job_step(tmp_path, monkeypatch, mode):
    from datetime import datetime
    from types import SimpleNamespace

    from core.context import cognitive_commit, situation_store
    from core.terminal import shadow
    from core.terminal.event_model import Event
    from loop import run

    scope = cognitive_commit.Scope("wechat", "bot", "c1")
    _goal(evidence_refs=("request",))
    world = get_world_model(scope.key)
    stale = situation.WorldModel(events=list(world.events), scope=world.scope.copy())
    request = Event("request", datetime(2026, 11, 18, 9), platform=scope.platform,
                    bot_account_id=scope.account_id, chat_id=scope.chat_id,
                    actor_account_id="requester", content="帮我整理两篇资料。",
                    external_msg_id="2001")
    actor = "someone-else" if mode == "other-speaker" else "requester"
    directive = "先别查，我想跟你说件事。" if mode == "temporary" else "这件事不用查了。"
    if mode == "question":
        directive = "这件事不用查了？"
    followup = Event("followup", datetime(2026, 11, 19, 9), platform=scope.platform,
                     bot_account_id=scope.account_id, chat_id=scope.chat_id,
                     actor_account_id=actor, content=directive)
    quote_id = "unknown" if mode == "unknown-quote" else request.external_msg_id
    events = {event.event_id: event for event in (request, followup)}

    def lookup(**coordinates):
        return request if coordinates == {
            "platform": scope.platform, "bot_account_id": scope.account_id,
            "chat_id": scope.chat_id, "external_msg_id": request.external_msg_id,
        } else None

    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(
        get=events.get, get_by_external_msg_id=lookup,
    ))
    row = SimpleNamespace(event_id=followup.event_id, msg_id=22, quote_id=quote_id,
                          at="2026-11-19 09:00:00", sender_id=actor, speaker="同名",
                          is_self=False, chat_id=scope.chat_id, text=directive)
    cell = {"platform": scope.platform, "chat_type": "private", "chat_id": scope.chat_id,
            "after_mine": [row]}
    db = str(tmp_path / "wechat" / "bot" / "account.db")
    if mode == "before-jobs":
        pending = replace(_job(GOAL, steps=2), step_n=1)
        next_job = Job(job_id="j2", chat_id=scope.chat_id, goal="核对日期",
                       steps=[Step(n=1, action="send:群|日期已核对")])
        queue = _Store(pending)
        monkeypatch.setattr(queue, "open_jobs", lambda chat_id: [pending, next_job])
        monkeypatch.setattr(queue, "load", {pending.job_id: pending, next_job.job_id: next_job}.get)
        spy = _Spy()
        monkeypatch.setattr(run, "_is_owner_chat", lambda chat_id: chat_id == scope.chat_id)
        monkeypatch.setattr(job_bridge, "_store", lambda store=None: queue)
        monkeypatch.setattr(job_bridge, "_execute", spy)
        monkeypatch.setattr(job_bridge, "open_from_owner", lambda *a, **kw: {})

        snap = {"cells": [cell]}
        assert run._advance_snapshot_jobs(snap, db, dry_run=True) == ({}, "", ())
        assert situation.project(world.events).intentions[GOAL].status == "open"
        assert spy.calls == []
        advanced, readout, job_scope = run._advance_snapshot_jobs(snap, db, dry_run=False)
        assert spy.calls == ["send:群|日期已核对"] and advanced["job_id"] == "j2"
        assert "核对日期" in readout and GOAL not in readout
        assert job_scope == scope.key
        assert cognitive_commit.closed_states_for_scope(scope) == {GOAL: "abandoned"}
        return

    monkeypatch.setenv("YINYUE_FEATURE_OPEN_LOOP_CLOSE", "off")
    monkeypatch.setattr(run.ports, "pick_db", lambda: db)
    monkeypatch.setattr(run.snapshot, "build_snapshot", lambda db: {"cells": [cell]})
    monkeypatch.setattr(run.snapshot, "render_text", lambda *a, **kw: directive)
    monkeypatch.setattr(run.snapshot, "render_turns", lambda *a, **kw: directive)
    monkeypatch.setattr(run, "pick_next", lambda *a, **kw: ("", {}) if kw["taken"] else (scope.chat_id, {}))
    monkeypatch.setattr(run, "_hb_beat_once", lambda: None)
    monkeypatch.setattr(run, "_write_heartbeat", lambda *a: None)
    monkeypatch.setattr(run, "_feedback_rows", lambda rows: [])
    monkeypatch.setattr(run.direct_send, "owner_ids", lambda: [])
    monkeypatch.setattr(run.direct_send, "check_deliveries", lambda: {})

    async def no_send(*args, **kwargs):
        return {}

    async def wait_for(coro, **kwargs):
        return await coro

    monkeypatch.setattr(run.direct_send, "run", no_send)
    monkeypatch.setattr(run.asyncio, "wait_for", wait_for)
    sent = _Spy()
    job = _Store(replace(_job(GOAL, steps=2), step_n=1))
    outcome = {}

    async def run_loop(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(situation_store, "get_world_model", lambda *a, **kw: stale)
            outcome.update(job_bridge.advance_once(scope.chat_id, store=job, execute=sent, scope=scope))
        return []

    monkeypatch.setattr(run.brain, "run_loop", run_loop)
    coro = run.one_tick(dry_run=False, llm=None)
    try:
        with pytest.raises(StopIteration):
            coro.send(None)
    finally:
        coro.close()

    goal = situation.project(world.events).intentions[GOAL]
    if mode == "requester":
        assert sent.calls == [] and outcome["stop"] == "goal_closed"
        assert goal.status == "closed" and goal.state == "abandoned"
        assert cognitive_commit.closed_states_for_scope(scope) == {GOAL: "abandoned"}
    else:
        assert sent.calls == ["send:群|第2篇"]
        assert goal.status == "open"


def test_revoked_goal_source_blocks_action(monkeypatch):
    from core.memory import belief_commit

    _goal(evidence_refs=("revoked-request",))
    job = _job(goal_id=GOAL)
    store = _Store(job)
    spy = _Spy()
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "dead" if ref == "revoked-request" else "alive",
    )

    out = job_bridge.advance_once("c1", store=store, execute=spy)

    assert spy.calls == []
    assert out["stop"] == "goal_source_revoked"
    assert [row[:2] for row in store.appended] == [("j1", "cancelled")]
    goal = situation.project(get_world_model("c1").events).intentions[GOAL]
    assert goal.status == "closed" and goal.state == "abandoned"


def test_promise_goal_keeps_the_request_event_for_revoke_guard(monkeypatch):
    from core.context.promise_goal import ensure_promise_goal
    from core.memory import belief_commit

    key = ensure_promise_goal(
        "c1",
        promise_id="p1",
        what="整理两篇资料",
        scope=SCOPE,
        source_refs=("revoked-request",),
    )
    job = _job(goal_id=key)
    store = _Store(job)
    spy = _Spy()
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope: "dead" if ref == "revoked-request" else "unknown",
    )

    out = job_bridge.advance_once("c1", store=store, execute=spy, scope=SCOPE)

    assert key == "promise:p1"
    assert spy.calls == []
    assert out["stop"] == "goal_source_revoked"
    assert [row[:2] for row in store.appended] == [("j1", "cancelled")]
    goal = situation.project(get_world_model("c1").events).intentions[key]
    assert goal.status == "closed" and goal.state == "abandoned"


def test_cancel_between_two_steps_stops_the_second():
    """第一步已经发生（事实算数）；下一拍再推进时已被取消，第二步不许再发。

    `yield_at_safe_point` 决定一次 advance 只在安全点走一步，所以「两步之间」
    就是两次 `advance_once`——中间那次取消必须被第二拍看见。
    """
    job = _job(steps=2)
    store = _Store(job)
    spy = _Spy()

    first = job_bridge.advance_once("c1", store=store, execute=spy)
    assert spy.calls == ["send:群|第1篇"]
    assert first["stop"] == "safe_point"

    store.job = replace(store.job, state="cancelled", step_n=1)
    second = job_bridge.advance_once("c1", store=store, execute=spy)

    assert spy.calls == ["send:群|第1篇"], "取消之后不得继续发第二步"
    assert second["stop"] == "state"


def test_goal_closed_between_two_steps_stops_the_second():
    _goal()
    model = get_world_model("c1")
    job = _job(goal_id=GOAL, steps=2)
    store = _Store(job)
    spy = _Spy()

    first = job_bridge.advance_once("c1", store=store, execute=spy)
    assert spy.calls == ["send:群|第1篇"]
    assert first["stop"] == "safe_point"

    situation.close_intention(model, GOAL, at="2026-11-18 09:40",
                              result="他说不用了", state="abandoned")
    assert save_world("c1", scope=model.scope)
    store.job = replace(store.job, step_n=1)

    second = job_bridge.advance_once("c1", store=store, execute=spy)

    assert spy.calls == ["send:群|第1篇"]
    assert second["stop"] == "goal_closed"


def test_late_receipt_after_cancel_is_ignored_by_goal_signal():
    """迟到回执不复活目标（既有契约）；这里只确认它不会让 job 继续推进。"""
    from core.context.goal_wake import note_goal_signal

    _goal(state="abandoned")
    model = get_world_model("c1")
    assert note_goal_signal(model, GOAL, source="receipt", ref="job:j1:done",
                            at="2026-11-18 10:00") is False
    assert not situation.open_intentions(model)
