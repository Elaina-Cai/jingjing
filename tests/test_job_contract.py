# -*- coding: utf-8 -*-
"""「在手的事」契约测试（docs/decisions/办_在手的事.md）。

跑法：python -X utf8 -u tests\\test_job_contract.py
失败会打印 RESULT n failed 并以非零码退出（run_all_tests.py 收集 tests/test_*.py）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

failed = 0


def check(name: str, cond: bool, detail: object = "") -> None:
    global failed
    if cond:
        print("PASS", name)
    else:
        failed += 1
        print("FAIL", name, "-", detail)


def main() -> int:
    # ── 1. 契约形状（只放类型，不放实现）────────────────────────────
    try:
        from core.contracts.plan import (  # noqa: F401
            APPROVAL_TTL_HOURS, MAX_JOBS_PER_CHAT, MAX_TURNS_PER_JOB,
            READOUT_MAX_CHARS, READOUT_MAX_ITEMS, TOOL_CALLS_PER_JOB,
            JOB_STATES, Approval, Artifact, Awaiting, Handoff, Job, Plan, Policy,
            Step, Trigger,
        )
        from core.contracts.delivery import STATUSES
    except Exception as exc:  # pragma: no cover
        check("可以 import core.contracts.plan / delivery", False, exc)
        print(f"RESULT {failed} failed")
        return 1

    # 2026-11-03 P2-2：五态 -> 六态。``abandoned`` 是"办不下去/没人再管"，
    # 与 ``cancelled``（主人叫停，正常收尾）必须分开，否则"半途而废率"永远算不出来
    # （`jobs` 表里的写入侧早就支持 kind="abandoned"，只是状态机漏了它，见方案文档 §12.5）。
    check("一件事的状态机六态（含 abandoned）", tuple(JOB_STATES) == ("open", "awaiting", "done",
                                                              "cancelled", "abandoned", "expired"), JOB_STATES)
    check("四槽齐全（trigger/policy/artifact/handoff）",
          all(hasattr(Job(job_id="j", chat_id="c", goal="g"), s)
              for s in ("trigger", "policy", "artifact", "handoff")))
    check("预算常量与文档一致（≤3 件 / ≤3 条 / ≤60 字 / 12 次 / 20 回合）",
          (MAX_JOBS_PER_CHAT, READOUT_MAX_ITEMS, READOUT_MAX_CHARS,
           TOOL_CALLS_PER_JOB, MAX_TURNS_PER_JOB) == (3, 3, 60, 12, 20),
          (MAX_JOBS_PER_CHAT, READOUT_MAX_ITEMS, READOUT_MAX_CHARS,
           TOOL_CALLS_PER_JOB, MAX_TURNS_PER_JOB))
    check("等批准默认 24 小时", APPROVAL_TTL_HOURS == 24, APPROVAL_TTL_HOURS)
    check("出口第六态 pending_approval 已在契约里", "pending_approval" in STATUSES, STATUSES)

    # ── 2. 安全点与推进 ────────────────────────────────────────────
    st = [Step(n=1, action="查温度", safe_point=True),
          Step(n=2, action="下单", effects="external_send", safe_point=False)]
    job = Job(job_id="j1", chat_id="c1", goal="订机票", steps=st)
    check("下一步按 n 取，不按列表位置", job.next_step().n == 1, job.next_step())
    job.step_n = 1
    check("推进一步后下一步是下单", job.next_step().n == 2)
    check("不可中断的步骤 safe_point 为假", job.next_step().safe_point is False)
    check("没有批准时步骤 approved 为假", job.next_step().approved is False)

    # ── 3. 读数 ≤60 字且带「在等什么」──────────────────────────────
    long_goal = "规划下周去杭州的行程并把高铁票和酒店都比一遍价" * 2
    j2 = Job(job_id="j2", chat_id="c1", goal=long_goal, awaiting=Awaiting(kind="approval", who="P5"))
    check("读数不超过 60 字", len(j2.readout()) <= READOUT_MAX_CHARS, len(j2.readout()))
    check("读数里写着在等什么", "等批准" in j2.readout(), j2.readout())

    # ── 4. 回合上限后不再推进 ─────────────────────────────────────
    j3 = Job(job_id="j3", chat_id="c1", goal="长任务", turns_total=MAX_TURNS_PER_JOB)
    check("到 20 回合不能再推进", j3.can_advance() is False)
    j3.state = "done"
    j3.turns_total = 0
    check("已完成的 job 不再推进", j3.can_advance() is False)

    # ── 5. 批准是一行，不是一句话 ─────────────────────────────────
    ap = Approval(job_id="j1", step_n=2, action_hash="sha256:abc", who="P5",
                  channel="wechat:private", at="2026-10-20T09:00:00")
    check("批准带 action_hash（批的是动作）", ap.action_hash.startswith("sha256:"))
    check("批准带通道（必须能证明本人）", "private" in ap.channel)

    print(f"RESULT {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
