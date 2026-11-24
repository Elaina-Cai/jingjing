"""「全 C」的多步行动链：开单只认主人私聊，推进的每一步都真的发出去。

这些用例都在行为层：不读源码文本，只喂输入、看输出和副作用。
"""
from __future__ import annotations

import asyncio

import pytest

from loop import job_bridge


class _Step:
    def __init__(self, action: str, n: int = 1, effects: str = "external_send") -> None:
        self.action = action
        self.n = n
        self.effects = effects
        self.safe_point = True
        self.approved = False


class _Job:
    def __init__(self, goal: str, steps=None, *, step_n: int = 0, job_id: str = "j1") -> None:
        self.job_id = job_id
        self.chat_id = "wxid_owner"
        self.goal = goal
        self.steps = list(steps or [_Step("send:银月的测试群|今天下雨记得带伞")])
        self.step_n = step_n
        self.state = "open"
        self.turns_total = 0
        self.awaiting = None


class _Store:
    def __init__(self, jobs=(), *, boom: bool = False) -> None:
        self._jobs = list(jobs)
        self._boom = boom
        self.created: list[str] = []

    def open_jobs(self, chat_id: str):
        if self._boom:
            raise RuntimeError("台账读不到")
        return list(self._jobs)


class _Res:
    def __init__(self, job, *, ran: bool = True, stop: str = "done") -> None:
        self.job = job
        self._ran = ran
        self.stop = stop

    @property
    def progressed(self) -> bool:
        return self._ran

    def summary(self) -> str:
        return "job %s stop=%s" % (self.job.job_id, self.stop)


@pytest.fixture()
def owner(monkeypatch):
    """默认：这个会话是主人私聊，开单成功返回 j-new。"""
    import core.jobs.ask as ask
    import core.jobs.direct as direct

    monkeypatch.setattr(ask, "is_owner_private", lambda chat_id, **kw: True)
    monkeypatch.setattr(
        direct,
        "open_direct",
        lambda text, *, chat_id, store=None, trace_id="", goal_id="": "j-new",
    )
    monkeypatch.setattr(
        job_bridge,
        "_ensure_direct_goal",
        lambda *args, goal_id="", **kwargs: goal_id,
    )


@pytest.fixture()
def sourced_world(tmp_path, monkeypatch):
    from core.context.cognitive_commit import Scope
    from core.context.situation_store import reset_world_models

    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    reset_world_models()
    try:
        yield Scope("wechat", "bot", "wxid_owner")
    finally:
        reset_world_models()


def test_an_owner_order_opens_one_job(owner):
    got = job_bridge.open_from_owner(
        "wxid_owner", "在银月的测试群说一句：今天下雨记得带伞", msg_id="m1", store=_Store())
    assert got["ok"] is True
    assert got["job_id"] == "j-new"
    assert got["target"] == "银月的测试群"
    assert got["body"] == "今天下雨记得带伞"


def test_an_owner_order_binds_the_job_to_a_sourced_goal(sourced_world, monkeypatch):
    import core.jobs.ask as ask
    import core.jobs.direct as direct
    from core.context import situation
    from core.context.situation_store import get_world_model

    monkeypatch.setattr(ask, "is_owner_private", lambda chat_id, **kw: True)
    seen: dict = {}

    def _open(text, *, chat_id, store=None, trace_id="", goal_id=""):
        seen["goal_id"] = goal_id
        return "j-sourced"

    monkeypatch.setattr(direct, "open_direct", _open)
    got = job_bridge.open_from_owner(
        "wxid_owner",
        "在银月的测试群说一句：今天下雨记得带伞",
        msg_id="m-source",
        source_ref="event-request",
        scope=sourced_world,
        at="2026-11-20 09:00",
        store=_Store(),
    )

    assert got["ok"] is True
    assert got["job_id"] == "j-sourced"
    assert got["goal_id"] == seen["goal_id"]
    model = get_world_model("wxid_owner", scope=sourced_world)
    goal = situation.project(model.events).intentions[got["goal_id"]]
    assert goal.evidence_refs == ("event-request",)


def test_goal_bind_failure_cancels_the_new_job(tmp_path, monkeypatch):
    import core.jobs.ask as ask
    import core.jobs.direct as direct

    monkeypatch.setattr(ask, "is_owner_private", lambda chat_id, **kw: True)
    monkeypatch.setattr(
        direct,
        "open_direct",
        lambda text, *, chat_id, store=None, trace_id="", goal_id="": "j-new",
    )
    monkeypatch.setattr(
        job_bridge,
        "_ensure_direct_goal",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("persist failed")),
    )
    store = _Store()
    store.appended = []
    store.append = lambda job_id, kind, **kw: store.appended.append(
        (job_id, kind, dict(kw.get("payload") or {}))
    )

    got = job_bridge.open_from_owner(
        "wxid_owner",
        "在银月的测试群说一句：今天下雨记得带伞",
        msg_id="m-bind-fail",
        store=store,
    )

    assert got["ok"] is False
    assert store.appended == [
        ("j-new", "cancelled", {"stop": "goal_bind_failed"})
    ]


def test_ingest_persists_the_goal_link_on_the_job(monkeypatch):
    from sqlalchemy import create_engine

    from core import features
    from core.jobs import ingest
    from core.jobs.store import JobStore, ensure_schema

    monkeypatch.setattr(ingest, "_mode", lambda: features.MODE_ON)
    engine = create_engine("sqlite://")
    ensure_schema(engine)
    store = JobStore(engine, mode=features.MODE_ON)
    job = ingest.ingest_plan(
        {
            "plan": {
                "goal": "往「银月的测试群」说一句：今天下雨记得带伞",
                "steps": [{"action": "send:银月的测试群|今天下雨记得带伞"}],
            }
        },
        chat_id="wxid_owner",
        store=store,
        trace_id="owner-m1",
        goal_id="direct:g1",
    )

    assert job is not None
    assert job.goal_id == "direct:g1"
    assert (store.load(job.job_id) or job).goal_id == "direct:g1"


def test_without_a_message_id_nothing_opens(owner, monkeypatch):
    import core.jobs.direct as direct

    called = []
    monkeypatch.setattr(direct, "open_direct", lambda *a, **k: called.append(1) or "j-x")
    got = job_bridge.open_from_owner(
        "wxid_owner", "在银月的测试群说一句：今天下雨记得带伞", msg_id="", store=_Store())
    assert got["ok"] is False
    assert got["why"] == "no_proof"
    assert called == []


def test_a_group_chat_is_never_owner_private(monkeypatch):
    import core.jobs.ask as ask

    monkeypatch.setattr(ask, "is_owner_private", lambda chat_id, **kw: False)
    got = job_bridge.open_from_owner(
        "10000000001@chatroom", "在银月的测试群说一句：今天下雨记得带伞", msg_id="m2", store=_Store())
    assert got["ok"] is False
    assert got["why"] == "not_owner_private"


def test_a_sentence_that_is_not_an_order_opens_nothing(owner):
    got = job_bridge.open_from_owner("wxid_owner", "今天天气不错啊", msg_id="m3", store=_Store())
    assert got["ok"] is False
    assert got["why"] == "not_an_order"


def test_the_same_goal_is_not_opened_twice(owner):
    same = _Job("往「银月的测试群」说一句：今天下雨记得带伞")
    got = job_bridge.open_from_owner(
        "wxid_owner", "在银月的测试群说一句：今天下雨记得带伞。", msg_id="m4", store=_Store([same]))
    assert got["ok"] is False
    assert got["why"] == "already_open"
    assert got["job_id"] == "j1"


def test_a_broken_store_never_opens_a_duplicate():
    import core.jobs.ask as ask

    ask_ok = lambda chat_id, **kw: True  # noqa: E731
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(ask, "is_owner_private", ask_ok)
    try:
        got = job_bridge.open_from_owner(
            "wxid_owner", "在银月的测试群说一句：今天下雨记得带伞", msg_id="m5", store=_Store(boom=True))
    finally:
        mp.undo()
    assert got["ok"] is False
    assert got["why"] == "store_unreadable"


def test_a_send_step_really_lands_in_the_outbox(monkeypatch):
    from loop import ports

    seen: dict = {}

    def _resolve(target):
        return ("10000000001@chatroom", "exact")

    async def _say_many(parts, **kw):
        seen["parts"] = list(parts)
        seen.update(kw)

        class _R:
            data = {"ack": "queued"}

        return _R()

    monkeypatch.setattr(ports, "resolve_say_target", _resolve)
    monkeypatch.setattr(ports, "say_many", _say_many)
    out = job_bridge._execute(_Step("send:银月的测试群|今天下雨记得带伞"))
    assert out["ok"] is True
    assert seen["chat_id"] == "10000000001@chatroom"
    assert seen["reason"] == "job"
    assert seen["dry_run"] is False
    assert seen["parts"] == ["今天下雨记得带伞"]


def test_a_job_send_is_also_paced_like_proactive_speech():
    from loop import outbound_gate as gate

    assert "job" in gate.PROACTIVE_REASONS
    gate.reset()
    ok1, why1 = gate.check("c1", "场地定了吗", reason="job", now=1000.0)
    gate.remember("c1", "场地定了吗", reason="job", now=1000.0)
    ok2, why2 = gate.check("c1", "水已经订了", reason="job", now=1000.0 + 10)
    ok3, why3 = gate.check("c1", "水已经订了", reason="job", now=1000.0 + gate.PROACTIVE_GAP_S)
    gate.reset()
    assert (ok1, why1) == (True, "ok")
    assert (ok2, why2) == (False, "too_soon")
    assert (ok3, why3) == (True, "ok")


def test_a_target_that_cannot_be_resolved_is_not_sent(monkeypatch):
    from loop import ports

    monkeypatch.setattr(ports, "resolve_say_target", lambda target: ("", "not_found"))
    monkeypatch.setattr(
        ports, "say_many",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("认不出目标就不许发")))
    out = job_bridge._execute(_Step("send:没这个人|喂"))
    assert out["ok"] is False
    assert str(out["detail"]).startswith("target_rejected")


def test_advance_once_runs_with_external_send_allowed(monkeypatch):
    import core.jobs.runner as runner

    seen: dict = {}

    def _advance(job, **kw):
        seen.update(kw)
        seen["job"] = job
        return _Res(job)

    monkeypatch.setattr(runner, "advance", _advance)
    job = _Job("往「银月的测试群」说一句：今天下雨记得带伞")
    got = job_bridge.advance_once("wxid_owner", store=_Store([job]))
    assert got["job_id"] == "j1"
    assert "external_send" in seen["allow_effects"]
    assert seen["store"] is not None
    assert callable(seen["execute"])
    assert seen["yield_at_safe_point"] is True


def test_no_open_job_means_nothing_is_pushed():
    import core.jobs.runner as runner

    called = []
    runner_advance = runner.advance
    runner.advance = lambda *a, **k: called.append(1)
    try:
        got = job_bridge.advance_once("wxid_owner", store=_Store([]))
    finally:
        runner.advance = runner_advance
    assert got["ran"] is False
    assert got["why"] == "no_job"
    assert called == []


def test_an_unknown_action_is_not_executed():
    out = job_bridge._execute(_Step("write_note:随便写点"))
    assert out["ok"] is False
    assert out["detail"] == "unknown_action"


def test_the_readout_names_the_job_and_the_next_step():
    job = _Job("往「银月的测试群」说一句：今天下雨记得带伞")
    line = job_bridge.readout("wxid_owner", store=_Store([job]))
    assert line.startswith("[我在办的事]")
    assert "银月的测试群" in line
    assert "第 1/1 步" in line


def test_the_wake_block_says_what_is_in_hand():
    from loop import brain

    text = brain._wake_block({"job_line": "[我在办的事] 往「银月的测试群」说一句：今天下雨记得带伞（第 1/1 步：send:银月的测试群|今天下雨记得带伞）",
                              "job": {"job_id": "j1", "ran": False, "step_n": 0, "steps": 1}})
    assert "[我在办的事]" in text
    assert "银月的测试群" in text


def test_a_finished_step_says_it_moved_forward():
    from loop import brain

    text = brain._wake_block({"job_line": "[我在办的事] 往「银月的测试群」说一句：今天下雨记得带伞（第 1/1 步：send:银月的测试群|今天下雨记得带伞）",
                              "job": {"job_id": "j1", "ran": True, "step_n": 1, "steps": 1}})
    assert "推进了一步" in text
    assert "第 1/1 步" in text


def test_without_any_job_no_line_is_added():
    from loop import brain

    assert brain._job_line({}) == ""
    text = brain._wake_block({"peers": [{"who": "P5", "text": "在吗"}]})
    assert "[我在办的事]" not in text


def test_advance_once_yields_after_one_safe_step_and_keeps_the_next():
    from sqlalchemy import create_engine

    from core.contracts.plan import Job, Step
    from core.jobs import store as job_store
    from core import features

    engine = create_engine("sqlite://")
    job_store.ensure_schema(engine)
    store = job_store.JobStore(engine, mode=features.MODE_ON)
    job = Job(
        job_id="j-safe",
        chat_id="c1",
        goal="把两件事办完",
        steps=[Step(1, "查第一件"), Step(2, "查第二件")],
    )
    store.create(job)
    first = job_bridge.advance_once("c1", store=store, execute=lambda s: True)
    assert first["stop"] == "safe_point"
    assert first["step_n"] == 1
    assert (store.load("j-safe") or job).state == "open"
    second = job_bridge.advance_once("c1", store=store, execute=lambda s: True)
    assert second["stop"] == "done"
    assert second["step_n"] == 2
