# -*- coding: utf-8 -*-
"""A1-d「计划完成」**两个出口同结论**的守卫用例（补洞①）。

背景（为什么要这一条）
--------------------
上一批把「**无回执不许当完成**」接在了 ``agents/conversational.py::_react_with_planner``
那一格（``_slot_fill_receipt`` → ``core/tasks/slot_fill.py::settle_plan_execution`` /
``plan_completion``）。但 ``agents/planner.py::run_plan_loop`` 是**第二个"计划完成"出口**
——从那条路出去的计划可以无回执被判完成 → 计划层不变式是漏的。

本文件锁住三件事：

① **同结论**：同一份计划、同一份回执事实 → 两个出口给出同一个结论
   （有回执 → 都当完成；无回执 → 都 ``complete=False`` 且出口是 ``report``）；
② **``off`` 逐字节一致**：``EXEC_RECEIPT=off`` 时 ``run_plan_loop`` 连 owner 都不碰，
   产物与"没有这一格"逐字段相同（回滚位不得漂移）；
③ **结构锁（唯一 owner）**：``run_plan_loop`` 里**没有第二份判据**——它只调
   ``slot_fill.settle_plan_execution``，不自建回执判据、不 import 承诺工具表。

测试卫生
--------
* **"关"一律显式 ``off``**（不靠"env 未设"——那是 ``off`` 与 ``shadow`` 的分界）；
  import 期把本文件依赖的每个开关都钉死：``EXEC_RECEIPT`` / ``SLOT_FILL`` ×
  ``WORK_ITEM_LEDGER`` / ``WORK_ITEM_RUNS`` + ``PLAN_LOOP``；
* **不写生产库**：``LOG_DIR`` / ``META_DB_PATH`` / ``PLATFORMS_DIR`` / ``DATA_DIR``
  全指向仓库内隔离目录（照 ``tests/test_exec_receipt.py``）；本文件不建库、只判纯函数
  与循环接线，所以连 ``create_all`` 都不需要。

Run directly::

    .\\.venv\\Scripts\\python.exe tests\\test_completion_exit_parity.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TEST_LOG_DIR = str(Path("data") / "_test_logs_completion_exit")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR
_ISO_ROOT = (ROOT / "data" / "_completion_exit_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

# 日志初始化会去碰 LOG_DIR：这里只留隔离目录，初始化本身拦掉。
import core.infra.logging as _logging  # noqa: E402

_logging.init_logging = lambda *args, **kwargs: None

# ---- 开关钉死（import 期，显式；**不许**靠"env 未设"）----------------------
_PINS = {
    "YINYUE_FEATURE_EXEC_RECEIPT": "off",        # 本文件的"关"位：显式 off
    "YINYUE_FEATURE_SLOT_FILL": "on",            # off 位的前置：state machine 在
    "YINYUE_FEATURE_WORK_ITEM_LEDGER": "1",      # 落库/判据的兄弟组一起钉
    "YINYUE_FEATURE_WORK_ITEM_RUNS": "1",
    "YINYUE_FEATURE_PLAN_LOOP": "on",            # N2 外层循环的 on 口径
}
os.environ.update(_PINS)


def _pin() -> None:
    """每个用例开始时重钉开关（pytest 先收集全部模块，别的文件也会改 env）。"""
    os.environ.update(_PINS)


def _receipt(state: str):
    """钉死 ``EXEC_RECEIPT`` 的一格（"关"一律显式 ``off``，见模块 docstring）。"""
    from contextlib import contextmanager

    @contextmanager
    def _sw():
        saved = os.environ.get("YINYUE_FEATURE_EXEC_RECEIPT")
        os.environ["YINYUE_FEATURE_EXEC_RECEIPT"] = state
        try:
            yield
        finally:
            if saved is None:
                os.environ.pop("YINYUE_FEATURE_EXEC_RECEIPT", None)
            else:
                os.environ["YINYUE_FEATURE_EXEC_RECEIPT"] = saved

    return _sw()


from agents.planner import Plan, Planner, SubTask, TaskStatus  # noqa: E402
from core.contracts.content_plan import PlanResult, StepBudget  # noqa: E402
from core.tasks import slot_fill  # noqa: E402

_PLANNER_PATH = ROOT / "agents" / "planner.py"
_CONVERSATIONAL_PATH = ROOT / "agents" / "conversational.py"

#: 计划里那一步（``create_reminder`` ∈ ``slot_fill.PROMISE_TOOLS``：这一步"要替主人做事"）。
_STEP_DESC = "12点提醒主人去吃饭"
_STEP_TOOL = "create_reminder"
_REPLY_DESC = "回一句知道了吗"
_CHAT_ID = "10000000002@chatroom"
_OK_QUALITY = {"completeness": 1.0, "accuracy": 1.0,
               "should_iterate": False, "suggestion": ""}


# --------------------------------------------------------------------- 桩
class _ToolResult:
    def __init__(self, output: str = "", error: str = ""):
        self.output = output
        self.error = error
        self.data = None

    @property
    def is_error(self) -> bool:
        return bool(self.error)


class _FakeTool:
    """承诺型工具替身（形状与真工具一致：``execute(**kwargs) -> ToolResult``）。"""

    def __init__(self, name: str, output: str = "已创建提醒", error: str = ""):
        self.name = name
        self.description = "建提醒"
        self.parameters = {"content": {"type": "str"}}
        self.calls: list[dict] = []
        self._result = _ToolResult(output=output, error=error)

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


class _FakeTools:
    def __init__(self, tools: list[_FakeTool]):
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name):
        return self._tools.get(name)

    def all_tools(self):
        return self._tools


class _FakeLLM:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("假 LLM 的响应队列空了：调用次数超出预期")
        return self._responses.pop(0)

    def parse_json(self, resp, defaults):
        return dict(resp) if isinstance(resp, dict) else dict(defaults)


class _Ctx(dict):
    """``AgentContext`` 的最小替身：既有 ``.extra`` 又有 ``.get``（两个都要）。"""

    def __init__(self, extra: dict | None = None, **kwargs):
        super().__init__(**kwargs)
        self.extra = dict(extra or {})


def _step_payload(**overrides) -> dict:
    step = {
        "id": 1,
        "description": _STEP_DESC,
        "needed_info": ["要提醒什么、几点"],
        "expected_output": "一条已登记的提醒",
        "success_criteria": "提醒建出来了",
        "tool": _STEP_TOOL,
        "tool_args": {"content": "吃饭", "remind_at": "12:00"},
        "depends_on": [],
        "source": "memory",
    }
    step.update(overrides)
    return step


def _reply_payload() -> dict:
    return {
        "id": 2,
        "description": _REPLY_DESC,
        "needed_info": ["上面那步的结果"],
        "expected_output": "一句回话",
        "success_criteria": "回话发出去了",
        "tool": None,
        "tool_args": {},
        "depends_on": [1],
        "source": "memory",
    }


def _plan_payload() -> dict:
    return {
        "reasoning": "先建提醒，再回一句",
        "needs_clarification": False,
        "clarification_question": "",
        "subtasks": [_step_payload(), _reply_payload()],
    }


def _planner(**kwargs) -> Planner:
    return Planner(
        llm=_FakeLLM([_plan_payload(), _OK_QUALITY]),
        tools=_FakeTools([_FakeTool(_STEP_TOOL)]),
        **kwargs,
    )


def _run(planner: Planner, context: _Ctx, **kwargs) -> PlanResult:
    planner.set_context(context)
    return asyncio.run(
        planner.run_plan_loop(
            "12点叫我吃饭",
            budget=StepBudget(max_outer_replans=0),
            ts_factory=lambda: "2026-10-06T10:00:00",
            **kwargs,
        )
    )


class _SpyOwner:
    """owner 的可观测替身：**把结论交给真 owner 算**，自己只记录"被调了几次"。"""

    def __init__(self, receipt=None):
        self.calls: list[dict] = []
        self.receipt = receipt

    def __call__(self, *, plan, context=None, repo=None, cache=None, now=None):
        self.calls.append({"plan": plan, "context": dict(context or {}), "repo": repo})
        return slot_fill.plan_completion(plan, [] if self.receipt is None else [self.receipt])


def _plan_for_verdict(*, attempts: int = 1) -> Plan:
    """与循环里那份计划**同形**的一份（用于定义层直接判，便于两出口对照）。"""
    promise = SubTask(id=1, description=_STEP_DESC, tool=_STEP_TOOL,
                      tool_args={"content": "吃饭", "remind_at": "12:00"})
    promise.retry_count = int(attempts)
    promise.attempts = int(attempts)
    promise.status = TaskStatus.DONE
    reply = SubTask(id=2, description=_REPLY_DESC, tool=None, depends_on=[1])
    reply.status = TaskStatus.DONE
    return Plan(goal="12点叫主人去吃饭", subtasks=[promise, reply], plan_id="plan-exit-parity")


def _receipt_for(plan: Plan):
    """一份**覆盖这一步**的执行回执（形状与 owner 落库的那份同源）。"""
    step = slot_fill.promise_steps(plan)[0]
    return slot_fill.ExecutionReceipt(
        fill_id="fill-parity",
        work_item_id="slotfill:fill-parity",
        state=slot_fill.DONE,
        outcome="sent",
        ok=True,
        step_ids=(int(step.id),),
        tools=(_STEP_TOOL,),
        details=({"step_id": int(step.id), "tool": _STEP_TOOL, "ok": True},),
        source="ledger",
        at="2026-10-06T10:00:01",
    )


def _run_with_owner(spy: _SpyOwner, *, receipt):
    """真跑一次 ``run_plan_loop``，把 owner 换成 ``spy``（结论仍是真 owner 算的）。"""
    original = slot_fill.settle_plan_execution
    slot_fill.settle_plan_execution = spy
    try:
        context = _Ctx(extra={"chat_id": _CHAT_ID, "session_id": 7})
        return _run(_planner(), context), context
    finally:
        slot_fill.settle_plan_execution = original

# ---------------------------------------------------------------------------
# ① 两个出口同结论
# ---------------------------------------------------------------------------
def test_both_exits_deny_completion_without_a_receipt() -> None:
    """无回执：定义层判 ``complete=False``，接线层（第二个出口）也只回 ``report``。"""
    _pin()
    plan = _plan_for_verdict()
    # 定义层（唯一 owner 的判据本体）：**没有回执 → 不得视为完成**
    verdict = slot_fill.plan_completion(plan, [])
    assert verdict.complete is False, verdict
    assert verdict.asked_user is True and verdict.report.strip(), verdict
    assert verdict.missing == (1,) and verdict.missing_tools == (_STEP_TOOL,), verdict

    # 接线层：run_plan_loop（第二个"计划完成"出口）必须给出**同一个结论**
    spy = _SpyOwner(receipt=None)
    with _receipt("on"):
        result, context = _run_with_owner(spy, receipt=None)
    assert len(spy.calls) == 1, f"第二个出口必须过 owner 一次：{spy.calls}"
    called = spy.calls[0]
    assert called["plan"] is not None and slot_fill.promise_steps(called["plan"])
    assert called["context"].get("session_id") == 7, called["context"]
    assert called["repo"] is None, "本用例没给 repo：owner 按它自己的口径退回进程内那份"

    assert result.asked_user is True, result
    assert result.report.strip() == verdict.report.strip(), (
        "出口文本必须就是 owner 的 report（不许各写一句）"
    )
    assert "没拿到执行回执" in result.report, result.report
    assert result.over_budget is False, result

    # 两条出口（conversational / planner）走的是**同一个 owner handle**
    conv = _CONVERSATIONAL_PATH.read_text(encoding="utf-8-sig")
    plan_src = _PLANNER_PATH.read_text(encoding="utf-8-sig")
    assert "slot_fill.settle_plan_execution(" in conv
    assert "slot_fill.settle_plan_execution(" in plan_src


def test_both_exits_accept_completion_with_a_receipt() -> None:
    """有回执：定义层判完成，接线层照旧走"报喜"出口（不追加任何上报）。"""
    _pin()
    plan = _plan_for_verdict()
    receipt = _receipt_for(plan)
    verdict = slot_fill.plan_completion(plan, [receipt])
    assert verdict.complete is True, verdict
    assert verdict.report == "" and verdict.asked_user is False, verdict

    spy = _SpyOwner(receipt=receipt)
    with _receipt("on"):
        result, _context = _run_with_owner(spy, receipt=receipt)
    assert len(spy.calls) == 1, spy.calls
    assert result.asked_user is False, result
    assert result.report == "", "有回执就不许无中生有地报「没办成」"
    assert result.over_budget is False, result
    assert [step.status for step in result.steps][0] == "done", result.steps

    # 同一份计划、只差"有没有回执" → 两个出口的结论跟着翻面（判据真的在读回执）
    denied = slot_fill.plan_completion(plan, [])
    assert (denied.complete, verdict.complete) == (False, True)


def test_second_exit_reads_the_owner_verdict_not_its_own() -> None:
    """接线层**服从** owner：owner 给 ``complete=False`` 就走上报，给 ``True`` 就放行。

    这一条是"接线"而不是"判据"的证据：同一个循环、同一份计划，只换 owner 的结论，
    出口就跟着换（如果 ``run_plan_loop`` 里另有一份判据，这里必然对不上）。
    """
    _pin()
    denied = slot_fill.PlanCompletion(
        complete=False,
        missing=(1,),
        missing_tools=(_STEP_TOOL,),
        asked_user=True,
        report="第 1 步「" + _STEP_DESC + "」我还没拿到执行回执。要我再试一次，还是先放着？",
    )

    class _Fixed:
        def __init__(self, verdict):
            self.verdict = verdict
            self.calls = 0

        def __call__(self, **kwargs):
            self.calls += 1
            return self.verdict

    original = slot_fill.settle_plan_execution
    try:
        with _receipt("on"):
            for verdict, want_report in (
                (denied, True),
                (slot_fill.PlanCompletion(complete=True), False),
            ):
                owner = _Fixed(verdict)
                slot_fill.settle_plan_execution = owner
                context = _Ctx(extra={"chat_id": _CHAT_ID, "session_id": 7})
                result = _run(_planner(), context)
                assert owner.calls == 1, owner.calls
                if want_report:
                    assert result.asked_user is True and result.report == verdict.report, result
                else:
                    assert result.asked_user is False and result.report == "", result
    finally:
        slot_fill.settle_plan_execution = original

    # 空 report 的退化情形：出口仍不许留空格子（PlanResult 的契约）。
    empty = slot_fill.PlanCompletion(complete=False, missing=(1,), asked_user=True, report="")
    original = slot_fill.settle_plan_execution
    try:
        with _receipt("on"):
            slot_fill.settle_plan_execution = _Fixed(empty)
            result = _run(_planner(), _Ctx(extra={"chat_id": _CHAT_ID, "session_id": 7}))
    finally:
        slot_fill.settle_plan_execution = original
    assert result.asked_user is True and result.report.strip(), result


# ---------------------------------------------------------------------------
# ② off 逐字节一致
# ---------------------------------------------------------------------------
def test_off_mode_is_byte_identical_and_owner_is_untouched() -> None:
    """``EXEC_RECEIPT=off``：owner 零调用，产物与"没有这一格"逐字段相同。"""
    _pin()
    context1 = _Ctx(extra={"chat_id": _CHAT_ID, "session_id": 7})
    context2 = _Ctx(extra={"chat_id": _CHAT_ID, "session_id": 7})

    # 位一：off —— 把 owner 换成"一碰就炸"的替身；循环照旧跑完（说明它一次都没进）
    original = slot_fill.settle_plan_execution

    def _boom(**kwargs):
        raise AssertionError("off 位下第二个出口调了回执 owner（回滚位破口）")

    with _receipt("off"):
        assert slot_fill.exec_receipt_mode() == "off"
        off_result = _run(_planner(), context1)

        slot_fill.settle_plan_execution = _boom
        try:
            boom_result = _run(_planner(), context2)
        finally:
            slot_fill.settle_plan_execution = original

    assert off_result.to_dict() == boom_result.to_dict(), (
        "off = 连 owner 都不存在：两次产物必须逐字段相同"
    )
    assert off_result.asked_user is False and off_result.report == "", off_result

    # 位二：对照 —— 同一条循环在 on 位**确实**会进 owner（否则上一条断言是空转）
    spy = _SpyOwner(receipt=None)
    with _receipt("on"):
        slot_fill.settle_plan_execution = spy
        try:
            on_result = _run(_planner(), _Ctx(extra={"chat_id": _CHAT_ID, "session_id": 7}))
        finally:
            slot_fill.settle_plan_execution = original
    assert len(spy.calls) == 1, "on 位没进 owner：对照失效"
    assert on_result.to_dict() != off_result.to_dict(), "on/off 位给出了同一个产物 → 接线是空转"


# ---------------------------------------------------------------------------
# ②b off 位上的冻结基线（含"循环提前收工带 report"那一条路）
# ---------------------------------------------------------------------------
#: 冻结产物：``off`` 口径下一次真实循环的 ``PlanResult.to_dict()``（canonical JSON）。
#: 这是"接线前长什么样"的**可重放取证**：接线要是在 off 位上多说了/少说了一个字，
#: 这里立刻红。剧本 = 一步承诺型工具步（执行成功）+ 一步回话步 + assess_quality 报
#: "不完整"且预算用尽 → 循环走 "over_budget 带 report" 那条出口（**正是补的那一格**）。
_OFF_BASELINE_PATH = ROOT / "tests" / "_completion_exit_off_baseline.json"
_OFF_SCENARIO_STEP = {
    "id": 1,
    "description": "查昨晚的消息",
    "needed_info": ["昨晚群里的原话"],
    "expected_output": "消息列表（含 msg_id 与时间）",
    "success_criteria": "至少一条命中且时间在昨晚",
    "tool": "search_threads",
    "tool_args": {},
    "depends_on": [],
    "source": "memory",
}
_OFF_LONG = "命中 3 条：msg_id=11 昨晚 22:10 说的那件事；msg_id=12 补充一句。"
_OFF_INCOMPLETE = {
    "completeness": 0.1, "accuracy": 0.9, "relevance": 0.9,
    "should_iterate": True, "suggestion": "还差一条关键信息",
}


def _off_scenario_planner() -> Planner:
    return Planner(
        llm=_FakeLLM([
            {
                "reasoning": "先搜群消息",
                "needs_clarification": False,
                "clarification_question": "",
                "subtasks": [dict(_OFF_SCENARIO_STEP)],
            },
            dict(_OFF_INCOMPLETE),
        ]),
        tools=_FakeTools([_FakeTool("search_threads", output=_OFF_LONG)]),
    )


def _off_scenario_result() -> dict:
    """跑一次全 off 口径的循环（``StepBudget(max_outer_replans=0)`` → 走"超预算带 report"）。"""
    with _receipt("off"):
        planner = _off_scenario_planner()
        planner.set_context(_Ctx(extra={"chat_id": _CHAT_ID, "session_id": 7}))
        result = asyncio.run(
            planner.run_plan_loop(
                "帮我看看昨晚谁说的事",
                budget=StepBudget(max_outer_replans=0),
                ts_factory=lambda: "2026-10-06T10:00:00",
            )
        )
    return result.to_dict()


def test_off_mode_matches_the_frozen_baseline() -> None:
    """off 位 + "提前收工带 report" → 与冻结基线**逐字段相同**。"""
    _pin()
    current = _off_scenario_result()
    baseline = json.loads(_OFF_BASELINE_PATH.read_text(encoding="utf-8"))
    assert current == baseline, (
        "off 位的产物与冻结基线不一致（回滚位漂了）：\n"
        f"  now      = {json.dumps(current, ensure_ascii=False, sort_keys=True)}\n"
        f"  baseline = {json.dumps(baseline, ensure_ascii=False, sort_keys=True)}"
    )
    # 这一条剧本确实走到了"带 report 的那条出口"（不是走了别的分支的空转基线）
    assert current["over_budget"] is True and current["report"].strip(), current
    # 确定性：同一剧本再跑一次逐字段相同（时间戳由 ts_factory 注入）
    assert _off_scenario_result() == current


def _write_off_baseline() -> None:
    """重建冻结基线（只在**确认 off 位没漂**之后手工跑一次）。"""
    _OFF_BASELINE_PATH.write_text(
        json.dumps(_off_scenario_result(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# ③ 结构锁：run_plan_loop 里没有第二份判据
# ---------------------------------------------------------------------------
def _run_plan_loop_node() -> ast.AST:
    tree = ast.parse(_PLANNER_PATH.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_plan_loop":
            return node
    raise AssertionError("agents/planner.py 里找不到 run_plan_loop")


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            names.add(fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", ""))
    return names


def test_run_plan_loop_calls_the_unique_owner_and_keeps_no_second_verdict() -> None:
    """结构锁：判定只在 ``core/tasks/slot_fill.py``；这里只允许**调用** owner。"""
    _pin()
    node = _run_plan_loop_node()
    called = _called_names(node)
    assert "_plan_receipt" in called, sorted(called)

    source = _PLANNER_PATH.read_text(encoding="utf-8-sig")
    # (a) **判据本体**一个都不许调（"调用点不许自己再判一次"）：
    #     ``plan_completion`` / ``unreceipted_promises`` 是唯一 owner 的判据；
    #     ``promise_steps`` / ``_missing_reason`` 是它的内部件。判据的名字出现在
    #     日志事件名里（plan_completion_denied）不算——所以这里判的是**调用**。
    for forbidden in ("plan_completion", "unreceipted_promises", "promise_steps",
                      "_missing_reason"):
        assert forbidden not in called, (
            f"run_plan_loop 里出现了第二份判据的调用：{forbidden}（owner 只此一处）"
        )
    # 承诺工具表也不许在本函数里被读（那就等于在本层判"哪些步骤算承诺"）
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            assert sub.id != "PROMISE_TOOLS", "run_plan_loop 里读了承诺工具表"
        if isinstance(sub, ast.Attribute):
            assert sub.attr != "PROMISE_TOOLS", "run_plan_loop 里读了承诺工具表"

    # (b) 而且真的调了 owner 的接线入口（不是"什么都没接"）
    assert "slot_fill.settle_plan_execution(" in source

    # (c) ``agents/`` 里没有第二份判据（与 tests/test_exec_receipt.py ⑦(f) 同一口径）
    for path in (ROOT / "agents").rglob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        assert "_receipt_ok" not in text and "def plan_completion" not in text, path

    # (d) 只有一处定义：owner 的判据在 core/tasks/slot_fill.py（本文件只读不写）
    assert "def plan_completion" in (
        ROOT / "core" / "tasks" / "slot_fill.py"
    ).read_text(encoding="utf-8-sig")


def main() -> bool:
    print("=" * 62)
    print("plan completion exit parity (A1-d second exit) checks")
    print("=" * 62)
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok    {fn.__name__}")
        except Exception:  # pragma: no cover - diagnostics only
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
