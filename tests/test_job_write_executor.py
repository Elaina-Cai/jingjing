"""写操作执行器：`write_local` 走 act()，默认只判定不执行；其余 effect 一律不接。

对应 `docs/decisions/办_在手的事.md` §17.1 与 U-10 的
`write_executor(...)` / 停因 `shadow`。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine

from core.contracts.plan import Approval, Job, Step
from core.jobs import executor as ex
from core.jobs import runner as job_runner
from core.jobs.store import JobStore, ensure_schema


class FakeRegistry:
    """只记事，不碰真工具。"""

    def __init__(self, *, status="ok", content="办好了", truncated=False, boom=None, changed=True):
        self.observe_calls = []
        self.act_calls = []
        self._status = status
        self._content = content
        self._truncated = truncated
        self._boom = boom
        self._changed = changed

    def _out(self):
        if self._boom is not None:
            raise self._boom
        diff = {"path": "data/workspace/notes/行程.md", "before": "h0", "after": "h1"} if self._changed else {}
        return SimpleNamespace(
            status=self._status,
            content=self._content,
            truncated=self._truncated,
            state_before="" if self._changed else "h0",
            state_after="h1" if self._changed else "h0",
            changed=diff,
        )

    def observe(self, target, what, params=None):
        self.observe_calls.append({"target": target, "what": what, "params": dict(params or {})})
        return self._out()

    def act(self, target, action, params=None):
        self.act_calls.append({"target": target, "action": action, "params": dict(params or {})})
        return self._out()


def _step(effects="write_local", action="file_write:行程.md"):
    return SimpleNamespace(n=1, action=action, effects=effects, safe_point=True, approved=False)


def main() -> int:
    failed = 0

    def check(name, cond, detail=""):
        nonlocal failed
        if not cond:
            failed += 1
        print(("PASS " if cond else "FAIL ") + name + ("" if not detail else "  :: " + str(detail)))

    # ---------- read 与 write 的分岔 ----------
    reg = FakeRegistry(content="三条搜索结果")
    out = ex.write_executor(reg, turn_id="job-1", target="c1")(_step("read", "web_search:杭州"))
    check("read 步走 observe", out["ok"] is True and len(reg.observe_calls) == 1, out)
    check("observe 拿到 what", reg.observe_calls and reg.observe_calls[0]["what"] == "杭州",
          reg.observe_calls[:1])
    check("read 步不碰 act", reg.act_calls == [])

    reg2 = FakeRegistry()
    out2 = ex.write_executor(reg2, turn_id="job-2", target="c2")(_step("write_local"))
    check("影子档：判定该写但不写", out2.get("shadow") is True and out2["ok"] is False, out2)
    check("影子档 detail 认得出是影子", out2["detail"].startswith("blocked:shadow:"), out2["detail"])
    check("影子档一次 act 都没调", reg2.act_calls == [], reg2.act_calls)

    reg3 = FakeRegistry(content="已写入 行程.md")
    out3 = ex.write_executor(reg3, turn_id="job-3", target="c3", shadow=False)(_step("write_local"))
    check("非影子档真的写", out3["ok"] is True and len(reg3.act_calls) == 1, out3)
    call = reg3.act_calls[0] if reg3.act_calls else {}
    check("act 拿到动作名", call.get("action") == "file_write", call)
    check("act 拿到 what 与 turn_id",
          call.get("params", {}).get("what") == "行程.md"
          and call.get("params", {}).get("turn_id") == "job-3", call)
    check("写步不碰 observe", reg3.observe_calls == [])
    check("状态差异带回来了", out3.get("changed", {}).get("after") == "h1", out3)
    out3b = ex.write_executor(FakeRegistry(changed=False), shadow=False)(_step("write_local"))
    check("状态没变就是 no_change（不算办成）",
          out3b["ok"] is False and out3b["detail"].startswith("no_change:"), out3b)

    # ---------- 两档永远不接 ----------
    reg4 = FakeRegistry()
    out4 = ex.write_executor(reg4, shadow=False)(_step("ui_control", "click:确定"))
    check("ui_control 一律不接", out4["detail"].startswith("blocked:non_writable:ui_control"), out4)
    out5 = ex.write_executor(reg4, shadow=False)(_step("external_send", "mail:发"))
    check("external_send 只许走出口", out5["detail"].startswith("blocked:non_writable:external_send"), out5)
    check("两档都没碰 registry", reg4.act_calls == [] and reg4.observe_calls == [])
    out6 = ex.write_executor(reg4)(_step("", "x"))
    check("effect 缺失也不接", out6["detail"].startswith("blocked:non_writable:unknown"), out6)

    # ---------- 失败与截断 ----------
    check("没绑注册表就是 no_tool",
          ex.write_executor(None)(_step("read", "web_search:q"))["detail"].startswith("no_tool:"))
    check("空动作是 no_tool",
          ex.write_executor(FakeRegistry())(_step("read", ""))["detail"].startswith("no_tool:"))
    boom = ex.write_executor(FakeRegistry(boom=RuntimeError("网络断了")))(_step("read", "web_search:q"))
    check("工具抛异常变成这一步失败",
          boom["ok"] is False and "RuntimeError" in boom["detail"], boom)
    rl = ex.write_executor(FakeRegistry(status="rate_limited", content="太频繁"))(
        _step("read", "web_search:q"))
    check("非 ok 状态带状态前缀", rl["detail"].startswith("rate_limited:"), rl)
    long_out = ex.write_executor(FakeRegistry(content="字" * 900), max_result_chars=600)(
        _step("read", "web_search:q"))
    check("正文按上限截断", len(long_out["detail"]) == 601 and long_out["detail"].endswith("…"),
          len(long_out["detail"]))
    trunc = ex.write_executor(FakeRegistry(content="短", truncated=True))(_step("read", "web_search:q"))
    check("工具自己标了截断也加省略号", trunc["detail"].endswith("…"), trunc["detail"])

    # ---------- 与 advance 的合体：影子停、不推进、不算失败 ----------
    def _job():
        return Job(
            job_id="job-w1",
            chat_id="c9",
            goal="把行程写进文件",
            steps=[Step(n=1, action="file_write:行程.md", effects="write_local")],
        )

    engine = create_engine("sqlite://", future=True)
    ensure_schema(engine)
    store = JobStore(engine, mode="on")
    store.create(_job())
    first = job_runner.advance(store.load("job-w1"), store=store)
    check("写步没批准就停在 need_approval", first.stop == "need_approval", first.stop)

    approval = Approval(
        job_id="job-w1",
        step_n=1,
        action_hash=job_runner.step_action_hash("job-w1", _job().steps[0]),
        who="owner",
        channel="private",
        at="2026-10-21 10:00:00",
        expires_at=None,
    )
    store.approve(approval)
    store.append("job-w1", "approved", payload={"step_n": 1}, actor="human")

    reg5 = FakeRegistry(content="已写入")
    shadowed = job_runner.advance(
        store.load("job-w1"),
        execute=ex.write_executor(reg5, turn_id="job-w1", shadow=True),
        store=store,
        allow_effects=(ex.READ_EFFECT, ex.WRITE_EFFECT),
    )
    check("批准之后影子档停在 shadow", shadowed.stop == "shadow", shadowed.stop)
    check("影子档没真写", reg5.act_calls == [], reg5.act_calls)
    check("影子档不推进步号", shadowed.job.step_n == 0, shadowed.job.step_n)
    check("影子档这一步状态是 shadow",
          shadowed.ran and shadowed.ran[0].status == "shadow", shadowed.ran)
    kinds = store.event_kinds("job-w1")
    check("影子档不写 step_done", "step_done" not in kinds, kinds)
    check("影子档不算办完", "done" not in kinds, kinds)
    check("影子档没把事标成 done", store.load("job-w1").state != "done", store.load("job-w1").state)

    real = job_runner.advance(
        store.load("job-w1"),
        execute=ex.write_executor(reg5, turn_id="job-w1", shadow=False),
        store=store,
        allow_effects=(ex.READ_EFFECT, ex.WRITE_EFFECT),
    )
    check("非影子档同一步真的能办完", real.stop == "done", real.stop)
    check("非影子档真的调了 act", len(reg5.act_calls) == 1, reg5.act_calls)
    check("办完落 done 事件", "done" in store.event_kinds("job-w1"))

    # ---------- 停因 ----------
    check("停因集合里有 shadow", "shadow" in job_runner.STOP_REASONS, job_runner.STOP_REASONS)
    # 回合里接的是哪个执行器、用什么档：改由 `tests/test_job_tick_wiring.py`
    # 的 `test_on_wires_a_writable_executor` 真跑一次 `_job_tick` 来测。

    total = 36
    print(f"RESULT {failed} failed / {total - failed} checked")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
