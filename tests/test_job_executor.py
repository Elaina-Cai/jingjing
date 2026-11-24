"""U-10 第三刀：只读执行器——`Step.action` 只许接到工具包的 `observe`，别的 effect 一步都不跑。

钉住的几条：
  * `effects != "read"` 的步**根本不进 registry**（一个调用都不发）；
  * 会把 `turn_id`（= job_id）传下去，预算按这件事数；
  * 工具返回只认 `ok` 为成功：`no_result` 是「没查到」不是「办到了」，`timeout` 是「查不了」；
  * 返回进台账的正文有上限（≤600 字），超了截断并标 `truncated`；
  * registry 抛异常 / 没绑 registry → 这一步失败，**不许冒到 `advance()`**；
  * 端到端：两步纯只读能跑到 `stop=done` 且台账里有两条 `step_done`；
    带写操作的步没批准就停在 `need_approval`，执行器一次都不被调。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts.plan import Job, Step  # noqa: E402
from core.jobs import executor as X  # noqa: E402
from core.jobs import runner as R  # noqa: E402
from core.tools.registry import ToolResult  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        FAILED.append(name)
        print(f"FAIL {name} {detail}")


class FakeRegistry:
    """记下每次 observe 的参数；返回值由 `result` 决定。"""

    def __init__(self, result: ToolResult | None = None, *, boom: BaseException | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = result or ToolResult(status="ok", content="查到了：CA1234 08:00")
        self.boom = boom

    def observe(self, target: str, what: str, params: object = None) -> ToolResult:
        self.calls.append({"target": target, "what": what, "params": dict(params or {})})
        if self.boom is not None:
            raise self.boom
        return self.result


class FakeStore:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict]] = []

    def append(self, job_id: str, kind: str, *, payload: object = None, actor: str = "judge") -> int:
        self.rows.append((job_id, kind, dict(payload or {})))
        return len(self.rows)

    def is_approved(self, job_id: str, step_n: int, action_hash: str, *, now: object = None) -> bool:
        return False

    def kinds(self) -> list[str]:
        return [k for _j, k, _p in self.rows]


def main() -> int:
    # ---- 1. 非 read：一步都不发 ----
    reg = FakeRegistry()
    run = X.read_only_executor(reg, turn_id="job-1", target="c1")
    out = run(Step(n=1, action="buy", effects="external_send"))
    check("写操作不进 registry", reg.calls == [], str(reg.calls))
    check("写操作返回 blocked:non_read", out.get("ok") is False and "non_read" in str(out.get("detail")), str(out))

    # ---- 2. read：调 observe，参数对 ----
    reg = FakeRegistry()
    run = X.read_only_executor(reg, turn_id="job-1", target="c1")
    out = run(Step(n=1, action="web_search:杭州机票", effects="read"))
    check("read 步调了 observe 一次", len(reg.calls) == 1, str(len(reg.calls)))
    check("工具名解析对", reg.calls[0]["params"].get("tool") == "web_search", str(reg.calls[0]["params"]))
    check("what 解析对", reg.calls[0]["what"] == "杭州机票", str(reg.calls[0]["what"]))
    check("turn_id 传的是 job_id", reg.calls[0]["params"].get("turn_id") == "job-1",
          str(reg.calls[0]["params"]))
    check("target 传的是会话", reg.calls[0]["target"] == "c1", str(reg.calls[0]["target"]))
    check("ok 步返回成功", out.get("ok") is True, str(out))
    check("detail 带结果", "CA1234" in str(out.get("detail")), str(out))

    # ---- 3. 只有工具名没冒号 ----
    reg = FakeRegistry()
    run = X.read_only_executor(reg, turn_id="t", target="c")
    run(Step(n=1, action="read_screen", effects="read"))
    check("无冒号时 what 为空", reg.calls[0]["what"] == "", repr(reg.calls[0]["what"]))

    # ---- 4. 截断 ----
    reg = FakeRegistry(ToolResult(status="ok", content="长" * 900))
    run = X.read_only_executor(reg, turn_id="t", target="c")
    out = run(Step(n=1, action="fetch_url", effects="read"))
    detail = str(out.get("detail"))
    check("结果被截断到上限", len(detail) <= X.RESULT_MAX_CHARS + 1, str(len(detail)))
    check("截断带省略号", detail.endswith("…"), detail[-3:])

    # ---- 5. no_result / timeout 不算办到 ----
    for status in ("no_result", "timeout", "rate_limited"):
        reg = FakeRegistry(ToolResult(status=status, content=""))
        run = X.read_only_executor(reg, turn_id="t", target="c")
        out = run(Step(n=1, action="web_search:x", effects="read"))
        check(f"{status} 不算成功", out.get("ok") is False and status in str(out.get("detail")), str(out))

    # ---- 6. registry 抛异常 / 没绑 registry ----
    run = X.read_only_executor(FakeRegistry(boom=RuntimeError("网断了")), turn_id="t", target="c")
    out = run(Step(n=1, action="web_search:x", effects="read"))
    check("registry 异常不冒泡", out.get("ok") is False and "RuntimeError" in str(out.get("detail")), str(out))
    run = X.read_only_executor(None, turn_id="t", target="c")
    out = run(Step(n=1, action="web_search:x", effects="read"))
    check("没绑 registry → no_tool", out.get("ok") is False and "no_tool" in str(out.get("detail")), str(out))

    # ---- 7. 端到端：两步只读跑到 done ----
    reg = FakeRegistry()
    store = FakeStore()
    job = Job(job_id="job-e2e", chat_id="c1", goal="查机票", steps=[
        Step(n=1, action="web_search:杭州机票", effects="read"),
        Step(n=2, action="fetch_url:https://x", effects="read"),
    ])
    res = R.advance(job, execute=X.read_only_executor(reg, turn_id="job-e2e", target="c1"), store=store)
    check("两步只读 stop=done", res.stop == "done", res.stop)
    check("job 状态变 done", res.job.state == "done", res.job.state)
    check("step_n 推到 2", res.job.step_n == 2, str(res.job.step_n))
    check("台账里两条 step_done", store.kinds().count("step_done") == 2, str(store.kinds()))
    check("台账里有 done", "done" in store.kinds(), str(store.kinds()))
    check("工具预算累加到 2", res.job.tool_calls_total == 2, str(res.job.tool_calls_total))

    # ---- 8. 端到端：写操作没批准就停，执行器一次不调 ----
    reg = FakeRegistry()
    job2 = Job(job_id="job-w", chat_id="c1", goal="订票", steps=[
        Step(n=1, action="buy:CA1234", effects="external_send"),
    ])
    res2 = R.advance(job2, execute=X.read_only_executor(reg, turn_id="job-w", target="c1"), store=FakeStore())
    check("写操作停 need_approval", res2.stop == "need_approval", res2.stop)
    check("停住时执行器没被调", reg.calls == [], str(reg.calls))
    check("job 状态变 awaiting", res2.job.state == "awaiting", res2.job.state)

    # ---- 9. 静态：执行器不许自己开口、tick 只在 on 档注入 ----
    src = (ROOT / "core/jobs/executor.py").read_text(encoding="utf-8")
    for banned in ("core.speech", "core.routing", "agents", "scheduler", "web"):
        check(f"executor 不 import {banned}", f"import {banned}" not in src and f"from {banned}" not in src, banned)
    pipe = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    check("tick 里注入了 write_executor（影子档）", "write_executor(" in pipe, "没找到")
    check("注入只在 on 档", "mode == features.MODE_ON" in pipe and
          pipe.index("write_executor(") > pipe.index("mode = features.mode(features.JOB_LEDGER"),
          "档位判断不在注入之前")
    check("写步靠批准解冻（allow_effects 只放 read）",
          "allow_effects=(READ_EFFECT,)" in pipe, "写步被放进免批准名单了")
    check("写执行器不再停在影子档", "shadow=False" in pipe, "还是 shadow=True")

    print(f"RESULT {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
