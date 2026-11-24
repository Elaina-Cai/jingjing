# -*- coding: utf-8 -*-
"""D-3：运行期拿到的产物 / 队列 / 回执要能挂到目标上。

2026-11-18：证据引用原先只在 :func:`open_intention` 那一刻写死，执行器事后拿到的
``artifact:`` / ``queue:`` / ``ack:`` 没有入口，最终报告因此永远只会说
「尚未取得证据：产物保存、投递动作、本机观察、平台送达」。
"""

from __future__ import annotations

from core.context.goal_report import line, missing_classes, proof_of, status
from core.context.situation import (
    WorldModel,
    close_intention,
    note_evidence,
    open_intention,
    project,
)
from core.contracts.plan import Job, Step
from core.jobs.runner import _as_result, _refs_of, advance

KEY = "整理两篇资料"


def _model(**extra) -> WorldModel:
    model = WorldModel()
    open_intention(
        model,
        KEY,
        kind="act",
        at="2026-11-18 09:00",
        evidence="你帮我整理两篇符合条件的资料",
        desired_outcome="两篇资料都整理好、各自保存",
        done_when="两份独立结果都符合条件且已保存",
        evidence_refs=("msg:req-1",),
        scope="chat:same",
        origin="owner_request",
        **extra,
    )
    return model


def _goal(model: WorldModel):
    return project(model.events).intentions[KEY]


def test_note_evidence_appends_and_is_idempotent():
    model = _model()
    assert note_evidence(model, KEY, "artifact:1", at="2026-11-18 09:20") is True
    assert note_evidence(model, KEY, "artifact:1") is False
    goal = _goal(model)
    assert goal.evidence_refs == ("msg:req-1", "artifact:1")
    assert goal.status == "open"
    assert goal.revision == 2


def test_note_evidence_refuses_unknown_and_closed_goal():
    model = _model()
    assert note_evidence(model, "没这件事", "artifact:1") is False
    assert note_evidence(model, KEY, "   ") is False
    close_intention(model, KEY, at="2026-11-18 10:00", result="done")
    assert note_evidence(model, KEY, "artifact:2") is False
    assert _goal(model).evidence_refs == ("msg:req-1",)


def test_report_names_runtime_evidence_and_flags_the_rest():
    model = _model()
    note_evidence(model, KEY, "artifact:1", at="2026-11-18 09:20")
    note_evidence(model, KEY, "artifact:2", at="2026-11-18 09:40")
    goal = _goal(model)

    short = line(goal)
    assert "artifact:1" in short and "artifact:2" in short
    assert "尚未取得证据" in short
    assert len(short) <= 120

    full = status(goal)
    assert "artifact:2" in full
    assert missing_classes(goal.evidence_refs) == ("投递动作", "本机观察", "平台送达")
    assert proof_of("queue:q1") == "投递动作"
    assert proof_of("ack:1") == "平台送达"


def test_refs_of_keeps_order_and_drops_blanks():
    assert _refs_of("artifact:1") == ("artifact:1",)
    assert _refs_of(["artifact:1", "", "artifact:1", "queue:q1"]) == (
        "artifact:1",
        "queue:q1",
    )
    assert _refs_of(None) == ()
    assert _refs_of({"a": 1}) == ()


def test_as_result_carries_evidence_refs():
    ok, detail, extra = _as_result(
        {"ok": True, "detail": "done", "evidence_refs": ["artifact:1", "artifact:1"]}
    )
    assert (ok, detail) == (True, "done")
    assert extra["evidence_refs"] == ("artifact:1",)

    ok, detail, extra = _as_result((False, "资料站 503"))
    assert (ok, detail, extra) == (False, "资料站 503", {})


def test_advance_records_refs_on_step_run():
    job = Job(
        job_id="job-evidence",
        chat_id="same",
        goal=KEY,
        goal_id=KEY,
        steps=[Step(1, "send:群|第一篇", effects="external_send")],
    )
    seen: list[int] = []

    def execute(step: Step):
        seen.append(step.n)
        return {
            "ok": True,
            "detail": "done",
            "evidence_refs": ["artifact:1", "artifact:1"],
        }

    res = advance(job, execute=execute, allow_effects=("read", "external_send"))
    assert seen == [1]
    assert res.stop == "done"
    assert res.ran[0].refs == ("artifact:1",)
    assert res.ran[0].status == "done"
