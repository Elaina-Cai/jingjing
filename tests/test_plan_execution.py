# -*- coding: utf-8 -*-
"""批B Step B2：⑥手段-目的规划接线（复用 PLAN_LOOP：意图→步骤→工具→表达）。

覆盖：
* 只读工具视图：写/外发工具一律不自动执行；
* 执行载荷 [步骤执行结果] 渲染（off/shadow 零输出）；
* 生成口执行的条件与跳过（off / 纯表达 / task_session / 写工具 / 无 llm）；
* 复用 run_plan_loop：真实接线跑通并写 plan_execution；
* plan_turn_with_plan 同一次规划返回 Plan（执行层不二次规划）。
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.planner import (  # noqa: E402
    PLAN_LOOP_READ_ONLY_TOOLS,
    Planner,
    TaskStatus,
    plan_exec_tools,
    plan_execution_hint,
)
from core.routing import turn_pipeline  # noqa: E402


@contextlib.contextmanager
def _env(**values):
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _task(*, tool="search_messages", result="", status=TaskStatus.PENDING):
    return SimpleNamespace(
        id=1, description="查一下消息", tool=tool, status=status,
        attempts=0, switch_log=(), result=result,
    )


def _result(*, report="", over_budget=False, asked_user=False, replans=0):
    return SimpleNamespace(
        replans=replans, over_budget=over_budget,
        asked_user=asked_user, report=report,
    )


def _context(*, plan_object=None, extra=None):
    return SimpleNamespace(
        extra=dict(extra or {}), plan_object=plan_object,
        plan_execution=None, plan_execution_started=False,
    )


def _run_execute(context, *, tools=None):
    tool_table = tools if tools is not None else {"search_messages": object()}
    planner = Planner(None, tool_table)
    return asyncio.run(planner.execute_attached_plan(
        context, tools=tool_table, user_message="帮查下",
        ts_factory=lambda: "2026-03-01 12:00",
    ))


def test_read_only_view_blocks_side_effect_tools() -> None:
    tools = {
        "search_messages": object(),
        "web_search": object(),
        "send_message": object(),
        "create_reminder": object(),
    }
    view = plan_exec_tools(tools)
    assert view.get("search_messages") is tools["search_messages"]
    assert view.get("web_search") is tools["web_search"]
    assert view.get("send_message") is None
    assert view.get("create_reminder") is None
    assert sorted(view.keys()) == ["search_messages", "web_search"]
    assert "send_message" not in view and "search_messages" in view
    assert "send_message" not in PLAN_LOOP_READ_ONLY_TOOLS


def test_payload_and_hint_render_only_when_enforcing() -> None:
    plan = SimpleNamespace(subtasks=[
        _task(result="找到 2 条", status=TaskStatus.DONE),
        _task(tool=None),
    ])
    result = _result(report="已办完")
    payload = Planner(None, {}).plan_execution_payload(plan, result)
    assert len(payload["steps"]) == 1
    assert payload["steps"][0]["tool"] == "search_messages"
    assert payload["steps"][0]["result"] == "找到 2 条"
    assert payload["report"] == "已办完"

    with _env(YINYUE_FEATURE_PLAN_LOOP="off"):
        assert plan_execution_hint(payload) == ""
    with _env(YINYUE_FEATURE_PLAN_LOOP="shadow"):
        assert plan_execution_hint(payload) == ""
    with _env(YINYUE_FEATURE_PLAN_LOOP="on"):
        block = plan_execution_hint(payload)
        assert block.startswith("[步骤执行结果]")
        assert "找到 2 条" in block and "上报：已办完" in block


def test_execute_attached_plan_skips_when_not_eligible() -> None:
    # off：即使有 plan_object 也一行不走
    context = _context(plan_object=SimpleNamespace(subtasks=[_task()]))
    with _env(YINYUE_FEATURE_PLAN_LOOP="off"):
        _run_execute(context)
    assert context.plan_execution is None
    assert context.plan_execution_started is False

    with _env(YINYUE_FEATURE_PLAN_LOOP="on"):
        # 纯表达计划：没有工具步
        context = _context(plan_object=SimpleNamespace(subtasks=[_task(tool=None)]))
        _run_execute(context)
        assert context.plan_execution is None

        # task_session：交给既有 _react_with_planner，不双执行
        context = _context(
            plan_object=SimpleNamespace(subtasks=[_task()]),
            extra={"task_session": True},
        )
        _run_execute(context)
        assert context.plan_execution is None

        # 写工具：不自动执行
        context = _context(
            plan_object=SimpleNamespace(subtasks=[_task(tool="send_message")]),
        )
        _run_execute(context)
        assert context.plan_execution is None

        # 工具表里没有该只读工具：不执行
        context = _context(plan_object=SimpleNamespace(subtasks=[_task()]))
        _run_execute(context, tools={})
        assert context.plan_execution is None


def test_execute_attached_plan_runs_read_only_and_records() -> None:
    task = _task()
    plan = SimpleNamespace(subtasks=[task])
    context = _context(plan_object=plan)

    async def fake_run(self, user_message, **kwargs):
        task.status = TaskStatus.DONE
        task.result = "找到 2 条"
        task.attempts = 1
        return _result()

    original = Planner.run_plan_loop
    Planner.run_plan_loop = fake_run
    try:
        with _env(YINYUE_FEATURE_PLAN_LOOP="on"):
            _run_execute(context)
    finally:
        Planner.run_plan_loop = original

    payload = context.plan_execution
    assert context.plan_execution_started is True
    assert payload is not None and len(payload["steps"]) == 1
    assert payload["steps"][0]["tool"] == "search_messages"
    assert payload["steps"][0]["result"] == "找到 2 条"
    assert payload["steps"][0]["attempts"] == 1


def test_execution_is_wired_after_planning_in_the_assembly_point() -> None:
    source = inspect.getsource(turn_pipeline.attach_content_plan_for)
    assert "attach_content_plan(" in source
    assert "execute_attached_plan(" in source
    # 时序：执行必须在 attach（规划成功、plan_object 挂上）之后
    assert source.index("attach_content_plan(") < source.index("execute_attached_plan(")
    # 生成口顶部不许再有执行钩子（那里 plan_object 还没挂）
    assert "_execute_attached_plan" not in inspect.getsource(turn_pipeline.generate_turn)


def test_plan_turn_with_plan_returns_the_same_plan() -> None:
    planner = Planner(None, {})
    fake_result = object()
    fake_plan_obj = SimpleNamespace(
        needs_clarification=False, to_result=lambda: fake_result,
    )

    async def fake_understand(user_message, context_summary="", session_id=None):
        return fake_plan_obj

    planner.understand_and_plan = fake_understand
    result, plan = asyncio.run(planner.plan_turn_with_plan("帮我查下"))
    assert result is fake_result and plan is fake_plan_obj


# ---------------------------------------------------------------------------
# standalone runner（项目约定：python tests/test_plan_execution.py；与 pytest 双兼容）
# ---------------------------------------------------------------------------

def main() -> bool:
    checks = [
        test_read_only_view_blocks_side_effect_tools,
        test_payload_and_hint_render_only_when_enforcing,
        test_execute_attached_plan_skips_when_not_eligible,
        test_execute_attached_plan_runs_read_only_and_records,
        test_execution_is_wired_after_planning_in_the_assembly_point,
        test_plan_turn_with_plan_returns_the_same_plan,
    ]
    passed = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - 失败必须逐个报出来，不许静默
            print(f"  FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  ok   {check.__name__}")
    print(f"plan-execution checks: {passed}/{len(checks)} passed")
    return passed == len(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
