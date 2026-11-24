# -*- coding: utf-8 -*-
"""「在手的事」推进器（U-10）的验收：只读先行、批准闸、预算、安全点、回合上限。

钉住四条硬线：
- 没有批准，`effects != "read"` 的步骤一步都不许跑（`stop="need_approval"`）；
- 工具预算是**一件事**的累计（`TOOL_CALLS_PER_JOB`），不是单回合；
- 回合上限到点就停（`MAX_TURNS_PER_JOB`），不靠模型自觉；
- 失败停在安全点上，`step_n` 不动——不许出现「跑了一半以为跑完了」。

跑法：`.venv_it\\Scripts\\python.exe -X utf8 -u tests\\test_job_runner.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

from core import features  # noqa: E402
from core.contracts.plan import (  # noqa: E402
    MAX_TURNS_PER_JOB,
    TOOL_CALLS_PER_JOB,
    Approval,
    Awaiting,
    Job,
    Step,
)
from core.jobs import runner as job_runner  # noqa: E402
from core.jobs import store as job_store  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f"  [{detail}]" if detail else ""))


def _job(*steps: Step, **kw) -> Job:
    return Job(job_id="j1", chat_id="c1", goal="规划下周去杭州", steps=list(steps), **kw)


def _read_steps(n: int = 2) -> list[Step]:
    return [Step(n=i + 1, action=f"查第{i + 1}样") for i in range(n)]


def _store(mode: str = features.MODE_ON):
    engine = create_engine("sqlite://")
    job_store.ensure_schema(engine)
    return job_store.JobStore(engine, mode=mode)


def test_readonly_runs_to_done() -> None:
    st = _store()
    job = _job(*_read_steps(2))
    st.create(job)
    calls: list[int] = []
    res = job_runner.advance(job, execute=lambda s: (calls.append(s.n), True)[1], store=st)
    check("只读两跑到底 stop=done", res.stop == "done", res.stop)
    check("两步都跑了", [r.status for r in res.ran] == ["done", "done"], str(res.ran))
    check("step_n 推到 2", res.job.step_n == 2, str(res.job.step_n))
    check("tool_calls 记 2", res.job.tool_calls_total == 2, str(res.job.tool_calls_total))
    check("回合数 +1", res.job.turns_total == 1, str(res.job.turns_total))
    kinds = st.event_kinds("j1")
    check("台账：created + step_done×2 + done + turn",
          kinds == ["created", "step_done", "step_done", "done", "turn"], str(kinds))
    check("投影与返回值一致", (st.load("j1") or res.job).state == "done")


def test_needs_approval_before_external_send() -> None:
    st = _store()
    steps = [Step(n=1, action="查票价"), Step(n=2, action="下单", effects="external_send")]
    job = _job(*steps)
    st.create(job)
    ran: list[int] = []
    res = job_runner.advance(job, execute=lambda s: (ran.append(s.n), True)[1], store=st)
    check("停在 need_approval", res.stop == "need_approval", res.stop)
    check("只跑了第 1 步", ran == [1], str(ran))
    check("第 2 步没执行", res.job.step_n == 1, str(res.job.step_n))
    check("状态转 awaiting", res.job.state == "awaiting", res.job.state)
    check("等的是批准", res.job.awaiting.kind == "approval", res.job.awaiting.kind)
    check("台账有 awaiting 事件", "awaiting" in st.event_kinds("j1"), str(st.event_kinds("j1")))


def test_approval_unblocks() -> None:
    st = _store()
    steps = [Step(n=1, action="下单", effects="external_send")]
    job = _job(*steps)
    st.create(job)
    res = job_runner.advance(job, execute=lambda s: True, store=st,
                             approved=lambda s: True)
    check("批了就跑（stop=done）", res.stop == "done", res.stop)
    check("跑到第 1 步", res.job.step_n == 1, str(res.job.step_n))


def test_approval_from_store() -> None:
    st = _store()
    steps = [Step(n=1, action="下单", effects="external_send")]
    job = _job(*steps)
    st.create(job)
    st.approve(Approval(job_id="j1", step_n=1,
                        action_hash=job_runner.step_action_hash("j1", steps[0]),
                        who="P5", channel="wechat", at="2026-10-20 10:00:00"))
    res = job_runner.advance(job, execute=lambda s: True, store=st)
    check("台账里的批准也放行", res.stop == "done", res.stop)


def test_budget_per_job() -> None:
    st = _store()
    job = _job(*_read_steps(3))
    st.create(job)
    res = job_runner.advance(job, execute=lambda s: True, store=st, budget=2)
    check("预算 2 → 只跑 2 步", res.tool_calls == 2, str(res.tool_calls))
    check("停在 budget", res.stop == "budget", res.stop)
    check("第 3 步没跑", res.job.step_n == 2, str(res.job.step_n))
    again = job_runner.advance(res.job, execute=lambda s: True, store=st, budget=2)
    check("再推一次预算已耗尽", again.stop == "budget" and again.tool_calls == 0,
          f"{again.stop}/{again.tool_calls}")


def test_default_budget_is_per_job() -> None:
    st = _store()
    job = _job(*_read_steps(TOOL_CALLS_PER_JOB + 1))
    st.create(job)
    res = job_runner.advance(job, execute=lambda s: True, store=st)
    check("默认预算 = TOOL_CALLS_PER_JOB", res.tool_calls == TOOL_CALLS_PER_JOB, str(res.tool_calls))
    check("超出预算停在 budget", res.stop == "budget", res.stop)


def test_turns_cap() -> None:
    st = _store()
    job = _job(*_read_steps(1), turns_total=MAX_TURNS_PER_JOB)
    st.create(job)
    res = job_runner.advance(job, execute=lambda s: True, store=st)
    check("回合用完停在 turns", res.stop == "turns", res.stop)
    check("一步都没跑", res.job.step_n == 0, str(res.job.step_n))


def test_awaiting_blocks() -> None:
    st = _store()
    job = _job(*_read_steps(1), state="awaiting", awaiting=Awaiting(kind="input"))
    st.create(job)
    res = job_runner.advance(job, execute=lambda s: True, store=st)
    check("等人输入时不动", res.stop == "awaiting", res.stop)


def test_failure_keeps_step() -> None:
    st = _store()
    job = _job(*_read_steps(2))
    st.create(job)
    res = job_runner.advance(job, execute=lambda s: (False, "timeout"), store=st)
    check("失败停在 failed", res.stop == "failed", res.stop)
    check("step_n 不动", res.job.step_n == 0, str(res.job.step_n))
    check("失败原因带回来", res.ran[0].detail == "timeout", res.ran[0].detail)


def test_terminal_state_and_no_executor() -> None:
    st = _store()
    job = _job(*_read_steps(1), state="done")
    st.create(job)
    check("已完成不再推进", job_runner.advance(job, execute=lambda s: True, store=st).stop == "state")
    fresh = _job(*_read_steps(1))
    check("没给 execute 就空转", job_runner.advance(fresh).stop == "no_executor")


def test_action_hash() -> None:
    a = Step(n=1, action="下单 杭州→深圳")
    b = Step(n=1, action="下单 杭州→深圳")
    c = Step(n=2, action="下单 杭州→深圳")
    ha = job_runner.step_action_hash("j1", a)
    check("同一动作 hash 稳定", ha == job_runner.step_action_hash("j1", b))
    check("步不同 hash 不同", ha != job_runner.step_action_hash("j1", c))
    check("长度 16", len(ha) == 16, str(len(ha)))


def test_stop_reasons_closed() -> None:
    check("停因是封闭集合", set(job_runner.STOP_REASONS) == {
        "done", "need_approval", "awaiting", "budget", "turns", "state",
        "failed", "no_executor", "shadow", "safe_point",
        # 2026-11-18：执行前复查目标是否已被取消/依据被撤回，两种都要有自己的停因
        "goal_closed", "goal_source_revoked",
    }, str(job_runner.STOP_REASONS))


def test_safe_point_yields_and_resumes_next_tick() -> None:
    st = _store()
    job = _job(*_read_steps(2))
    st.create(job)
    first = job_runner.advance(
        job, execute=lambda s: True, store=st, yield_at_safe_point=True
    )
    check("安全点停", first.stop == "safe_point", first.stop)
    check("只跑第 1 步", [r.n for r in first.ran] == [1], str(first.ran))
    check("下一步保留", first.job.next_step().n == 2, str(first.job.step_n))
    check("仍开着", first.job.state == "open", first.job.state)
    second = job_runner.advance(
        first.job, execute=lambda s: True, store=st, yield_at_safe_point=True
    )
    check("下一拍跑完", second.stop == "done", second.stop)
    check("step_n 推到 2", second.job.step_n == 2, str(second.job.step_n))


def test_non_safe_step_is_not_cut_and_runs_to_next_safe_point() -> None:
    st = _store()
    steps = [
        Step(n=1, action="扣款", safe_point=False),
        Step(n=2, action="查结果", safe_point=True),
        Step(n=3, action="写总结", safe_point=True),
    ]
    job = _job(*steps)
    st.create(job)
    res = job_runner.advance(
        job, execute=lambda s: True, store=st, yield_at_safe_point=True
    )
    check("不安全步不切", [r.n for r in res.ran] == [1, 2], str(res.ran))
    check("停在下一个安全点", res.stop == "safe_point", res.stop)
    check("下一步是 3", res.job.next_step().n == 3, str(res.job.step_n))


TESTS = [
    ("readonly", test_readonly_runs_to_done),
    ("approval_gate", test_needs_approval_before_external_send),
    ("approval_override", test_approval_unblocks),
    ("approval_store", test_approval_from_store),
    ("budget", test_budget_per_job),
    ("budget_default", test_default_budget_is_per_job),
    ("turns", test_turns_cap),
    ("awaiting", test_awaiting_blocks),
    ("failure", test_failure_keeps_step),
    ("state", test_terminal_state_and_no_executor),
    ("hash", test_action_hash),
    ("safe_point", test_safe_point_yields_and_resumes_next_tick),
    ("safe_after_unsafe", test_non_safe_step_is_not_cut_and_runs_to_next_safe_point),
    ("stops", test_stop_reasons_closed),
]


def main() -> int:
    for name, fn in TESTS:
        print(f"--- {name} ---")
        fn()
    print(f"RESULT {FAILED} failed / {PASSED} passed")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
