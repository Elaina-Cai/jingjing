# -*- coding: utf-8 -*-
"""状态差异式回执：写操作「怎么才算办成了」。

跑法：``python -X utf8 -u tests/test_job_state_diff.py``

写操作没有幂等键（App 上点重了就是两单），所以只有一种证据能算数：
**动手前长什么样、动手后长什么样**。本用例钉三件事：
1. `changed` 为空 = `no_change`，执行器不许当成功；
2. `changed` 非空时状态差异要一路带进台账（`step_done` 载荷）；
3. 差的那一半（`state_before` / `state_after`）不许丢。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts.plan import Job, Plan, Step  # noqa: E402
from core.jobs.executor import write_executor  # noqa: E402
from core.jobs.runner import advance  # noqa: E402
from core.tools.registry import ToolResult  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name} :: {detail}")
        FAILED.append(name)


class FakeRegistry:
    """按剧本返回 ToolResult，并记录被调用了几次。"""

    def __init__(self, results: list[ToolResult]) -> None:
        self._results = list(results)
        self.observe_calls = 0
        self.act_calls = 0

    def observe(self, *, target: str = "", what: str = "", params=None) -> ToolResult:
        self.observe_calls += 1
        return self._results.pop(0) if self._results else ToolResult(status="no_result")

    def act(self, *, target: str = "", action: str = "", params=None) -> ToolResult:
        self.act_calls += 1
        return self._results.pop(0) if self._results else ToolResult(status="no_result")


class FakeStore:
    def __init__(self, *, approved: bool = True) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self._approved = approved

    def append(self, job_id: str, kind: str, payload=None, *, actor: str = "") -> int:
        self.events.append((job_id, kind, dict(payload or {})))
        return len(self.events)

    def is_approved(self, job_id: str, step_n: int, action_hash: str) -> bool:
        return self._approved

    def kinds(self) -> list[str]:
        return [k for _, k, _ in self.events]


def _step(n: int, action: str, effects: str) -> Step:
    return Step(n=n, action=action, effects=effects)


def _job(*steps: Step) -> Job:
    return Job(job_id="job-1", chat_id="chat-1", goal="把方案写成文件",
               steps=list(Plan(goal="g", steps=list(steps)).steps))


def main() -> int:
    # ---- 1. 什么都没变 → no_change，不算办成 ----
    reg = FakeRegistry([ToolResult(status="ok", content="内容未变", state_before="abc", state_after="abc")])
    run = write_executor(reg, turn_id="job-1", target="chat-1", shadow=False)
    out = run(_step(1, "write_note:杭州三日", "write_local"))
    check("changed 为空时执行器判失败", out["ok"] is False, repr(out))
    check("失败原因是 no_change", str(out["detail"]).startswith("no_change:"), out["detail"])
    check("前后哈希仍然带回来（好查为什么没变）",
          out.get("state_before") == "abc" and out.get("state_after") == "abc", repr(out))

    # ---- 1b. 状态压根采不到 -> no_state，更不许当办成 ----
    out_ns = write_executor(FakeRegistry([ToolResult(status="ok", content="点了一下")]),
                            turn_id="job-1", shadow=False)(_step(1, "note:写", "write_local"))
    check("采不到状态时判 no_state",
          out_ns["ok"] is False and str(out_ns["detail"]).startswith("no_state:"), repr(out_ns))

    # ---- 2. 变了 → ok，且差异三样都在 ----
    changed = {"path": "data/workspace/notes/杭州三日.md", "before": "", "after": "def"}
    reg2 = FakeRegistry([ToolResult(status="ok", content="已写", state_before="", state_after="def", changed=changed)])
    run2 = write_executor(reg2, turn_id="job-1", target="chat-1", shadow=False)
    out2 = run2(_step(1, "write_note:杭州三日", "write_local"))
    check("changed 非空时判成功", out2["ok"] is True, repr(out2))
    check("状态差异随结果带回", out2.get("changed") == changed, repr(out2.get("changed")))
    check("走的是 act 不是 observe", reg2.act_calls == 1 and reg2.observe_calls == 0,
          f"act={reg2.act_calls} observe={reg2.observe_calls}")

    # ---- 3. 台账里要留状态差异 ----
    step = _step(1, "write_note:杭州三日", "write_local")
    store = FakeStore()
    run2b = write_executor(
        FakeRegistry([ToolResult(status="ok", content="已写", state_before="", state_after="def", changed=changed)]),
        turn_id="job-1", target="chat-1", shadow=False,
    )
    res = advance(_job(step), execute=run2b, store=store, allow_effects=("read",))
    check("批准后写步真的跑了（stop=done）", res.stop == "done", res.stop)
    step_events = [p for _, k, p in store.events if k == "step_done"]
    check("写了 step_done", len(step_events) == 1, str(len(step_events)))
    if step_events:
        payload = step_events[0]
        check("step_done 带 state_before", payload.get("state_before") == "", repr(payload))
        check("step_done 带 state_after", payload.get("state_after") == "def", repr(payload))
        check("step_done 带 changed", payload.get("changed") == changed, repr(payload.get("changed")))

    # ---- 4. 没批准 → 一步都不许动 ----
    reg3 = FakeRegistry([ToolResult(status="ok", state_after="zzz", changed={"a": 1})])
    run3 = write_executor(reg3, turn_id="job-1", target="chat-1", shadow=False)
    store2 = FakeStore(approved=False)
    res3 = advance(_job(step), execute=run3, store=store2, allow_effects=("read",))
    check("没批准就停在 need_approval", res3.stop == "need_approval", res3.stop)
    check("没批准时执行器零调用", reg3.act_calls == 0 and reg3.observe_calls == 0,
          f"act={reg3.act_calls} observe={reg3.observe_calls}")

    # ---- 5. ui_control / external_send 连判定都不给 ----
    for effect in ("ui_control", "external_send"):
        out5 = write_executor(FakeRegistry([]), turn_id="job-1")(_step(1, "x:y", effect))
        check(f"{effect} 直接 blocked", out5["ok"] is False and "non_writable" in out5["detail"], repr(out5))

    print(f"RESULT {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
