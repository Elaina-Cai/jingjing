"""M0 骨架：九步按序、失败就停、finish 一定跑、每步一行日志。"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.turn import entry, steps          # noqa: E402
from core.turn import trace as T           # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok  {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def run(coro):
    return asyncio.run(coro)


def t_steps_contract() -> None:
    check("九步名字与顺序", steps.STEP_NAMES == (
        "ingest", "read", "judge", "record", "act", "say", "receipt", "metrics", "finish"))
    check("is_step 认识 ingest", steps.is_step("ingest"))
    check("is_step 拒绝瞎写", not steps.is_step("nope"))
    check("order_of 有序", steps.order_of("ingest") == 0 and steps.order_of("finish") == 8)
    check("order_of 未知 -1", steps.order_of("nope") == -1)
    check("before 判序", steps.before("judge", "say") and not steps.before("say", "judge"))
    check("before 拒未知", not steps.before("nope", "say"))


def t_register_guards() -> None:
    entry.reset()
    entry.register("ingest", lambda ctx: None)
    try:
        entry.register("ingest", lambda ctx: None)
    except ValueError:
        check("重复注册要显式 replace", True)
    else:
        check("重复注册要显式 replace", False)
    entry.register("ingest", lambda ctx: None, replace=True)
    check("replace 可以换掉", True)
    try:
        entry.register("nope", lambda ctx: None)
    except ValueError:
        check("非九步不许注册", True)
    else:
        check("非九步不许注册", False)
    try:
        entry.register("read", 42)  # type: ignore[arg-type]
    except TypeError:
        check("不可调用的拒绝注册", True)
    else:
        check("不可调用的拒绝注册", False)
    entry.unregister("ingest")
    check("unregister 生效", "ingest" not in entry.handlers())
    entry.reset()


def t_empty_skeleton() -> None:
    entry.reset()
    result = run(entry.run_turn({"chat_id": "c1", "turn_id": "t1"}))
    check("空骨架 status=ok", result.status == "ok")
    check("空骨架走满九步", len(result.done) == 9)
    check("空骨架 chat_id 透传", result.chat_id == "c1")
    check("空骨架 turn_id 透传", result.turn_id == "t1")
    trace = T.current()
    check("跑完清空当前轨迹", trace is None)


def t_sync_and_async() -> None:
    entry.reset()
    seen: list[str] = []

    def sync_step(ctx):
        seen.append("ingest")

    async def async_step(ctx):
        await asyncio.sleep(0)
        seen.append("judge")

    entry.register("ingest", sync_step)
    entry.register("judge", async_step)
    result = run(entry.run_turn({"chat_id": "c"}))
    check("同步处理器跑到了", "ingest" in seen)
    check("异步处理器跑到了", "judge" in seen)
    check("异步顺序不乱", seen.index("ingest") < seen.index("judge"))
    check("过了 judge 继续到 finish", result.done[-1] == "finish")
    entry.reset()


def t_failure_stops_but_finishes() -> None:
    entry.reset()
    ran: list[str] = []

    def boom(ctx):
        raise RuntimeError("炸在第 2 步")

    def later(ctx):
        ran.append("say")

    entry.register("read", boom)
    entry.register("say", later)
    result = run(entry.run_turn({"chat_id": "c", "turn_id": "t"}))
    check("失败 status=error", result.status == "error")
    check("失败步骤记名字", result.failed == "read")
    check("失败原因进 note", "炸在第 2 步" in result.note)
    check("失败后不再往下跑", ran == [])
    check("finish 仍然跑了", result.done[-1] == "finish")
    check("失败的步不算完成", "read" not in result.done)
    check("失败后 say 没跑", "say" not in result.done)
    check("失败前完成的步保留", "ingest" in result.done)
    entry.reset()


def t_trace_lines() -> None:
    entry.reset()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("core.turn.trace")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        tr = T.begin(chat_id="cx", turn_id="tx")
        tr.step("ingest", ms=5, note="hello")
        tr.step("judge", status="error", note="bad thing")
        T.step("say", note="after begin")
        text = stream.getvalue()
    finally:
        logger.removeHandler(handler)
        T.end()
    check("日志有 turn_step", "turn_step" in text)
    check("日志带 turn id", "turn=tx" in text)
    check("日志带 chat id", "chat_id=cx" in text)
    check("错误状态打出来", "status=error" in text)
    check("safe_note 压成一行", "\n" not in T.safe_note("a\nb"))
    check("safe_note 截断", len(T.safe_note("x" * 500)) <= 120)
    check("missing 报未跑的步", "ingest" not in tr.missing())
    check("last_failed 指到 judge", tr.last_failed() == "judge")
    check("未知步骤只警告不抛", (tr.step("nope"), True)[1])


def t_handlers_map_override() -> None:
    entry.reset()
    entry.register("ingest", lambda ctx: (_ for _ in ()).throw(AssertionError("不该用全局表")))
    hit: list[str] = []
    result = run(entry.run_turn({"chat_id": "c"}, handlers_map={"ingest": lambda ctx: hit.append("x")}))
    check("handlers_map 覆盖全局表", hit == ["x"])
    check("覆盖后 status=ok", result.status == "ok")
    entry.reset()


def main() -> int:
    for fn in (t_steps_contract, t_register_guards, t_empty_skeleton, t_sync_and_async,
               t_failure_stops_but_finishes, t_trace_lines, t_handlers_map_override):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
