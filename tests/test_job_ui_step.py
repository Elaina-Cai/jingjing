# -*- coding: utf-8 -*-
"""界面步（`ui_control`）接进「在手的事」：三把钥匙都要转。

跑法：``python -X utf8 -u tests/test_job_ui_step.py``

钉五件事：
1. 默认（`allow_ui=False`）执行器不接 `ui_control`——放行是显式的，不是默认的；
2. 放行 ≠ 免批准：没有那一行 `Approval` 时 `advance()` 停在 `need_approval`，执行器零调用；
3. 批准之后才 `act()`，且 `what` 落进工具声明的**主参数**（`gui_click` → `selector`）；
4. 界面步也要状态差异：`changed` 空就是 `no_change`，不算办成；
5. `external_send` 即使批了、也放行了，执行器仍然不接（对外说话只走出口，R3）。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

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
    """按剧本返回 `ToolResult`，记录调用参数；`get()` 给出工具声明的参数名。"""

    def __init__(self, results: list[ToolResult] | None = None, *, args: dict | None = None) -> None:
        self._results = list(results or [])
        self._args = dict(args or {})
        self.observe_calls: list[dict] = []
        self.act_calls: list[dict] = []

    def get(self, tool: str):
        return SimpleNamespace(name=tool, args=self._args)

    def observe(self, *, target: str = "", what: str = "", params=None) -> ToolResult:
        self.observe_calls.append({"target": target, "what": what, "params": dict(params or {})})
        return self._results.pop(0) if self._results else ToolResult(status="no_result")

    def act(self, *, target: str = "", action: str = "", params=None) -> ToolResult:
        self.act_calls.append({"target": target, "action": action, "params": dict(params or {})})
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

    def payload_of(self, kind: str) -> dict:
        for _, k, payload in self.events:
            if k == kind:
                return payload
        return {}

    def kinds(self) -> list[str]:
        return [k for _, k, _ in self.events]


def _job(*steps: Step) -> Job:
    return Job(
        job_id="job-ui",
        chat_id="chat-1",
        goal="把机票页面上的按钮点一下",
        steps=list(Plan(goal="g", steps=list(steps)).steps),
    )


def _click_step(n: int = 1, effects: str = "ui_control") -> Step:
    return Step(n=n, action="gui_click:#buy", effects=effects)


def main() -> int:
    changed = {"pixels": 1234, "title": "订单确认"}
    click_ok = ToolResult(
        status="ok",
        content="点 #buy：前台=过 / 标题=过 / 像素差=1234；证据 data/evidence/job-ui_click_after.png",
        state_before="t1|u1|d1",
        state_after="t2|u2|d2",
        changed=changed,
        reversible=False,
    )

    # ---- 1. 默认不接 ui_control：放行必须是显式的 ----
    reg0 = FakeRegistry([click_ok], args={"selector": "str"})
    out0 = write_executor(reg0, turn_id="job-ui", shadow=False)(_click_step())
    check("默认（allow_ui=False）拒绝界面步", out0["ok"] is False, repr(out0))
    check("拒绝原因是 non_writable:ui_control",
          out0["detail"] == "blocked:non_writable:ui_control", out0["detail"])
    check("拒绝时一次 act 都没调", reg0.act_calls == [], reg0.act_calls)

    # ---- 2. 放行 ≠ 免批准：没人批就停 need_approval，执行器零调用 ----
    reg1 = FakeRegistry([click_ok], args={"selector": "str"})
    run1 = write_executor(reg1, turn_id="job-ui", target="chat-1", shadow=False, allow_ui=True)
    res1 = advance(_job(_click_step()), execute=run1, store=FakeStore(approved=False), allow_effects=("read",))
    check("没人批就停在 need_approval", res1.stop == "need_approval", res1.stop)
    check("停着的时候执行器零调用", reg1.act_calls == [], reg1.act_calls)
    check("台账里写下「在等批准」", "awaiting" in ["awaiting"] and res1.job.state == "awaiting", res1.job.state)

    # ---- 3. 批了才真的点，且 what 落进主参数 ----
    reg2 = FakeRegistry([click_ok], args={"selector": "str"})
    run2 = write_executor(reg2, turn_id="job-ui", target="chat-1", shadow=False, allow_ui=True)
    store2 = FakeStore(approved=True)
    res2 = advance(_job(_click_step()), execute=run2, store=store2, allow_effects=("read",))
    check("批准后跑到底", res2.stop == "done", res2.stop)
    check("真的调了一次 act", len(reg2.act_calls) == 1, reg2.act_calls)
    call = reg2.act_calls[0] if reg2.act_calls else {}
    check("act 拿到工具名", call.get("action") == "gui_click", call)
    check("what 落进 selector", call.get("params", {}).get("selector") == "#buy", call.get("params"))
    check("what 原样留着（老工具还读它）", call.get("params", {}).get("what") == "#buy", call.get("params"))
    check("payload 带 turn_id", call.get("params", {}).get("turn_id") == "job-ui", call.get("params"))
    check("走 act 不碰 observe", reg2.observe_calls == [], reg2.observe_calls)

    # ---- 4. 状态差异一路进台账 ----
    payload = store2.payload_of("step_done")
    check("step_done 记下 state_before", payload.get("state_before") == "t1|u1|d1", payload)
    check("step_done 记下 state_after", payload.get("state_after") == "t2|u2|d2", payload)
    check("step_done 记下 changed", payload.get("changed") == changed, payload.get("changed"))
    check("办完写一条 done", "done" in store2.kinds(), store2.kinds())

    # ---- 5. 界面步也要状态差异 ----
    nothing = ToolResult(status="ok", content="点了一下没反应", state_before="same", state_after="same")
    reg3 = FakeRegistry([nothing], args={"selector": "str"})
    res3 = advance(
        _job(_click_step()),
        execute=write_executor(reg3, turn_id="job-ui", shadow=False, allow_ui=True),
        store=FakeStore(approved=True),
        allow_effects=("read",),
    )
    check("界面没变化不算办成", res3.stop != "done", res3.stop)
    check(
        "停因说清楚是 no_change",
        any("no_change" in str(r.detail) for r in res3.ran),
        [(r.status, r.detail) for r in res3.ran],
    )

    # ---- 6. external_send 连批准都不放行 ----
    reg4 = FakeRegistry([ToolResult(status="ok", content="发出去了", changed={"msg": "1"})])
    out4 = write_executor(reg4, turn_id="job-ui", shadow=False, allow_ui=True)(
        _click_step(effects="external_send")
    )
    check("external_send 一律不接", out4["ok"] is False, repr(out4))
    check("原因指向 non_writable:external_send",
          out4["detail"] == "blocked:non_writable:external_send", out4["detail"])
    check("一次 act 都没调", reg4.act_calls == [], reg4.act_calls)

    # ---- 7. 静态：接线处只放行界面步，不许把它塞进免批准名单 ----
    src = io.open(ROOT / "core/routing/turn_pipeline.py", encoding="utf-8").read()
    check("_job_tick 显式 allow_ui=True", "allow_ui=True" in src)
    check("免批准名单里没有 ui_control",
          'allow_effects=(READ_EFFECT,)' in src and "allow_effects=(READ_EFFECT, UI_EFFECT" not in src)

    print(f"RESULT {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
