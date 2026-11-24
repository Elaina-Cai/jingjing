"""回合轨迹 + 唯一入口接线测试。

钉死三件事：
1. 轨迹器本身能打点、能报「哪步没跑」、没轨迹时静默；
2. 真实回合路径（agents/conversational.py）里**确实**开了轨迹并调了记账入口——
   证据是**跑一次真回合**，从轨迹里读 act/read/judge 三行、从日志里读它们各只留一行；
3. 记账（_job_tick）抛异常时轨迹照打、回合照跑（轨迹/记账不许拖垮回合）。

口径变化（2026-10-22）：第 2、3 条原来是读 agents/conversational.py 的源码字符串
（"_turn.begin(" in src / "_job_tick(" in src / try 块切片…）。那是把实现文本当契约：
把 helper 抽出去、把打点挪一行，行为没变测试就红。现在改成真回合的证据。

坑（2026-10-23 踩过一次）：本仓有两套日志出口，探针要各接一个。
turn_step 是 stdlib logging（root handler 抓得到），turn_act_skipped
走 structlog（root handler 抓不到，必须套 agents.conversational.log 这一层）。
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agents.conversational as conv_module  # noqa: E402
import core.jobs.direct as direct_module  # noqa: E402
import core.routing.turn_pipeline as pipeline_module  # noqa: E402
from core.routing import turn  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name + ((" :: " + str(detail)) if detail else ""))


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def with_capture(fn):
    buf = io.StringIO()
    handler = Capture()
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        fn()
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        buf.close()
    return handler.lines


def t_steps_fixed() -> None:
    check("九步名固定", turn.STEPS == ("ingest", "read", "judge", "record", "act", "say", "receipt", "metrics", "finish"), turn.STEPS)


def t_step_logs() -> None:
    def run() -> None:
        trace = turn.begin(chat_id="c1", turn_id="t1")
        turn.step("read", note="tools=3")
        check("seen 记下 read", trace.seen.get("read") == "ok")
        check("missing 不含 read", "read" not in trace.missing())
    lines = with_capture(run)
    joined = "\n".join(lines)
    check("日志有 turn_step", "turn_step turn=t1 step=read" in joined, joined[:200])
    check("日志有 chat_id", "chat_id=c1" in joined)
    check("日志有 note", "note=tools=3" in joined)


def t_no_trace_is_silent() -> None:
    turn._CURRENT.set(None)
    try:
        turn.step("judge")  # 不该抛
        check("无轨迹时静默", True)
    except Exception as exc:  # noqa: BLE001
        check("无轨迹时静默", False, repr(exc))


def t_unknown_step_warns() -> None:
    def run() -> None:
        turn.begin(chat_id="c2", turn_id="t2")
        turn.step("not_a_step")
    lines = with_capture(run)
    check("未知步骤只警告不抛", any("turn_step_unknown" in x for x in lines), lines[:3])


def t_missing_lists_unvisited() -> None:
    trace = turn.begin(chat_id="c3", turn_id="t3")
    turn.step("read")
    turn.step("judge")
    miss = trace.missing()
    check("未打点的都列出来", "act" in miss and "read" not in miss, miss)


class _LogProbe:
    """套在 agents.conversational 的 structlog 上：把 warning/info 原文抄一份。

    为什么不挂 root handler：structlog 那层不保证走根 logger 的 handler，
    真机日志里有、Capture 里没有——测试就会对着一行真实存在的警告说 FAIL。
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.lines: list[str] = []

    def _record(self, level: str):
        def _emit(*args, **kwargs) -> None:
            self.lines.append(" ".join(str(arg) for arg in args))
            getattr(self.inner, level)(*args, **kwargs)

        return _emit

    def __getattr__(self, name):
        if name in ("warning", "info", "debug", "error"):
            return self._record(name)
        return getattr(self.inner, name)


class _ProbeTool:
    """一行 schema 的假工具：只为了让这条路径走到「有工具可给」的分支。"""

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "probe_tool",
                "description": "probe",
                "parameters": {"type": "object", "properties": {}},
            },
        }


class _ProbeLLM:
    """够跑到 judge 的最小 LLM：不吐内容、不调工具。

    出稿/综合分支用的是 stream_chat（空流），主循环用 chat_tools（零 tool_calls）。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return ""

    async def chat_tools(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": "", "tool_calls": []}

    def stream_chat(self, **kwargs):
        self.calls.append(kwargs)

        async def _empty():
            for _ in ():
                yield ""

        return _empty()


def _shell():
    """`__new__` 造壳（不连 DB），补齐这条路径要用的成员。"""
    agent = conv_module.ConversationalAgent.__new__(conv_module.ConversationalAgent)
    agent._llm = _ProbeLLM()
    agent._persona = SimpleNamespace(id=1)
    agent.max_iterations = 1
    for name in ("_trace", "_tools_used", "_plan_loop_checks"):
        setattr(agent, name, [])
    for name in (
        "_message_time", "_speech_snapshot", "_affect_ctx", "_affect_manager_instance",
        "_last_context", "_repo", "_uq", "_memory", "_planner", "_feedback", "_evolve",
        "_turn_decision",
    ):
        setattr(agent, name, None)
    for name in (
        "_affect_block", "_inbound_content", "_sender_nickname", "_group_recent_context",
        "_reason_text", "_last_assistant_msg", "_turn_intent",
    ):
        setattr(agent, name, "")
    agent._unknown_answer = False
    agent._task_sessions = {}
    agent._is_group = False
    agent._is_task_session = lambda ctx: False
    agent._tool_view = lambda ctx: SimpleNamespace(
        all_tools=lambda: {"probe_tool": _ProbeTool()}
    )
    agent._conversation_identity = lambda ctx: ""
    agent._build_system_prompt = lambda is_group: "SYS"
    agent._build_research_prompt = lambda user, history, summary: "USER"
    agent._shadow_record_thought = lambda step, ctx: None
    return agent


def _turn_once(*, tick=None, msg: str = "在吗", extra: dict | None = None) -> dict:
    """跑一次真回合：返回轨迹、记账调用、以及这次跑出来的日志行。"""
    agent = _shell()
    context = SimpleNamespace(
        extra=dict(extra if extra is not None else {"chat_id": "c1"}),
        trace_id="t1",
        get=lambda key, default=None: default,
    )
    seen: list[dict] = []

    def _spy(**kwargs) -> None:
        seen.append(kwargs)
        if tick is not None:
            tick(**kwargs)

    old_tick = pipeline_module._job_tick
    old_direct = direct_module.open_direct
    old_log = conv_module.log
    log_probe = _LogProbe(old_log)
    conv_module.log = log_probe
    pipeline_module._job_tick = _spy
    # 本用例只测轨迹与记账接线，不测「程序化直接开单」；把开单换掉，免得往库里写。
    direct_module.open_direct = lambda *args, **kwargs: ""
    try:
        async def _go():
            async for _event in agent._run_persona_brain_turn_stream(msg, "", "", context):
                pass
            return turn.current()

        trace = asyncio.run(_go())
    finally:
        pipeline_module._job_tick = old_tick
        direct_module.open_direct = old_direct
        conv_module.log = old_log
    return {"trace": trace, "seen": seen, "log": log_probe.lines}


def _run_real_turn(**kwargs) -> tuple[object, list[dict], list[str]]:
    holder: dict = {}

    def run() -> None:
        holder.update(_turn_once(**kwargs))

    lines = with_capture(run)
    return holder["trace"], holder["seen"], lines + list(holder.get("log") or [])


def t_real_turn_records_act_read_judge() -> None:
    """真回合里 act/read/judge 三行都留痕，且顺序是 act → read → judge。"""
    trace, seen, lines = _run_real_turn()
    order = list(getattr(trace, "seen", {}) or {})
    steps = [line for line in lines if "turn_step " in line]

    def hits(step: str) -> int:
        return sum(1 for line in steps if f"step={step}" in line)

    check("真回合开了轨迹", trace is not None, order)
    check("轨迹 chat_id 取自 context.extra", getattr(trace, "chat_id", "") == "c1", getattr(trace, "chat_id", ""))
    check("轨迹 turn_id 取自 context.trace_id", getattr(trace, "turn_id", "") == "t1", getattr(trace, "turn_id", ""))
    check("打了 act / read / judge 三个点", {"act", "read", "judge"} <= set(order), order)
    check("三步各只留一行日志", (hits("act"), hits("read"), hits("judge")) == (1, 1, 1), steps)
    check(
        "顺序是 act → read → judge",
        set(order) >= {"act", "read", "judge"}
        and order.index("act") < order.index("read") < order.index("judge"),
        order,
    )
    check("记账入口被调了一次", len(seen) == 1, len(seen))
    check(
        "记账拿到 chat_id / 主人原话 / 私聊标记",
        bool(seen)
        and seen[0].get("chat_id") == "c1"
        and seen[0].get("approval_text") == "在吗"
        and seen[0].get("is_private") is True,
        {k: seen[0].get(k) for k in ("chat_id", "approval_text", "is_private")} if seen else None,
    )


def t_tick_failure_does_not_break_turn() -> None:
    """记账抛异常：只记一行警告，轨迹照打，回合照跑。"""

    def boom(**kwargs) -> None:
        raise RuntimeError("boom")

    trace, seen, lines = _run_real_turn(tick=boom)
    order = list(getattr(trace, "seen", {}) or {})
    check("记账抛异常时回合继续跑", {"ingest", "read", "judge"} <= set(order), order)
    check("抛异常时 act 点确实没打（记账在 act 之前）", "act" not in order, order)
    check("失败只记一行 turn_act_skipped", sum(1 for x in lines if "turn_act_skipped" in x) == 1, lines)


def main() -> int:
    for fn in (
        t_steps_fixed,
        t_step_logs,
        t_no_trace_is_silent,
        t_unknown_step_warns,
        t_missing_lists_unvisited,
        t_real_turn_records_act_read_judge,
        t_tick_failure_does_not_break_turn,
    ):
        print("\n" + fn.__name__)
        fn()
    print(f"\nRESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())