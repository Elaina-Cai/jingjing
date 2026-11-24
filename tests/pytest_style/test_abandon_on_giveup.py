# -*- coding: utf-8 -*-
"""P2-3：推进器自己"办不下去"时，必须把这件事写成 `abandoned`。

为什么要有这个文件（方案文档 §12.5 / E-26）：
`abandoned` 这个词在写入侧（`core/jobs/store.py:551`）早就能写，但没有调用方真去写，
于是报表里"半途而废率"永远是 0——人会把 0 读成"她从不半途而废"。这里钉住三件事：

1. 预算用尽那一次 advance，必须落一条 `abandoned` 事件，且 job 行变成 `state='abandoned'`；
2. 正常跑完（`done`）、挂起等批准（`need_approval`）都**不许**写成 abandoned（不许诬告）；
3. 统一词表认得它：`from_job_state('abandoned') == ABANDONED`，且非 operator 的放弃判 `stuck`。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

from core.contracts import finish  # noqa: E402
from core.contracts.plan import MAX_TURNS_PER_JOB, Job, Step  # noqa: E402
from core.jobs import runner as job_runner  # noqa: E402
from core.jobs import store as job_store  # noqa: E402


def _store():
    engine = create_engine("sqlite://")
    job_store.ensure_schema(engine)
    return job_store.JobStore(engine, mode="on")


def _job(*steps: Step, **kw) -> Job:
    return Job(job_id="j-abandon", chat_id="c1", goal="把下周三的场地定下来", steps=list(steps), **kw)


def test_give_up_stops_are_declared() -> None:
    assert job_runner.GIVE_UP_STOPS == ("budget", "turns")
    # 停因表只许追加：这两个必须仍在表里
    assert "budget" in job_runner.STOP_REASONS and "turns" in job_runner.STOP_REASONS


def test_budget_exhausted_is_recorded_as_abandoned() -> None:
    st = _store()
    job = _job(Step(n=1, action="查一下天气"), Step(n=2, action="查一下场地"))
    st.create(job)
    ran: list[int] = []
    res = job_runner.advance(job, execute=lambda s: (ran.append(s.n), True)[1], store=st, budget=1)
    assert res.stop == "budget", res.stop
    assert ran == [1]
    # 内存里的 job 也必须是终态，否则调用方会继续拿 open 的它去推进
    assert res.job.state == "abandoned"
    assert "abandoned" in st.event_kinds(job.job_id)
    loaded = st.load(job.job_id)
    assert loaded is not None
    assert loaded.state == "abandoned"
    assert finish.from_job_state(loaded.state) == finish.ABANDONED
    assert finish.abandon_cause(loaded.state, updated_by=loaded.updated_by) == "stuck"


def test_turns_cap_is_recorded_as_abandoned() -> None:
    """回合用尽是**早退**（一步都没跑），落盘必须写在那条早退里。"""
    st = _store()
    job = _job(Step(n=1, action="查一下天气"), turns_total=MAX_TURNS_PER_JOB)
    st.create(job)
    res = job_runner.advance(job, execute=lambda s: True, store=st)
    assert res.stop == "turns", res.stop
    assert res.job.state == "abandoned"
    assert res.job.step_n == 0
    assert "abandoned" in st.event_kinds(job.job_id)
    assert st.load(job.job_id).state == "abandoned"


def test_done_is_not_abandoned() -> None:
    st = _store()
    job = _job(Step(n=1, action="查一下天气"))
    st.create(job)
    res = job_runner.advance(job, execute=lambda s: True, store=st)
    assert res.stop == "done"
    assert "abandoned" not in st.event_kinds(job.job_id)
    assert st.load(job.job_id).state == "done"


def test_need_approval_is_not_abandoned() -> None:
    st = _store()
    job = _job(Step(n=1, action="给她发一条消息", effects="external_send"))
    st.create(job)
    res = job_runner.advance(job, execute=lambda s: True, store=st)
    assert res.stop == "need_approval"
    assert "abandoned" not in st.event_kinds(job.job_id)
    # 等批准是「还在走」：账本会把它推进 awaiting（不是 abandon，也不是 open）
    assert st.load(job.job_id).state == "awaiting"


def test_store_projects_abandoned_with_actor() -> None:
    st = _store()
    job = _job(Step(n=1, action="查一下天气"))
    st.create(job)
    assert st.append(job.job_id, "abandoned", payload={"stop": "budget"}, actor="runner")
    loaded = st.load(job.job_id)
    assert loaded is not None
    assert loaded.state == "abandoned"
    assert loaded.updated_by == "runner"
