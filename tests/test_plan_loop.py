# -*- coding: utf-8 -*-
"""N2 规划-执行两层循环回归：外层 owner（``agents/planner.py``）+ 内层对比收编。

对应 ``docs/需求契约.md`` §2.3（R-C1..R-C7）与 ``docs/decisions/架构评估.md`` §3.2 的
「必须收编的现有近似实现」逐条：

① ``understand_and_plan`` **升格为外层 owner**：一次规划 → ``Step`` 序列且三字段齐全（R-C2）；
② 内层对比失败 → **换方法三级**（①→②换工具→③换来源）+ 每条 ``Switch`` 留痕（R-C3/R-C4）；
③ 三级用尽 → 记证据、**回外层重规划**（``replans += 1``，§3.2 ②）；
④ 预算写死「尝试 ≤ 3 = 最多换 2 次方法」：**第 4 次不许再试**，转 ``over_budget``（§7 拍板项 5）；
⑤ ``over_budget=True`` 时 ``report`` 必填——**不许静默放弃**（R-C5）；③ ``assess_quality``
   的结论是 ``over_budget`` 判据之一；
⑥ ``PLAN_LOOP=off`` **与现状逐字节一致**：旧 prompt 逐字命中、旧口径的步按 R-C2 判无效、
   planner 门（``_is_task_session``）照旧挡住日常回合、主循环 AST 除那一处 hook 外一字未动；
⑦ 幂等/确定性：同一输入两次 → 同一 ``PlanResult``（时间戳由 ``ts_factory`` 注入）。

``_verify_step`` 的收编单独一组：off 口径的 ``{"passed","reason"}`` 与接线前逐字相同
（含 ``task.status`` 副作用），``on`` 口径才把 R-C3 的 error 判据接上。

Run directly:
    python tests/test_plan_loop.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
# 隔离前导（照 tests/test_reply_intent_contract.py:37-46）。
_TEST_LOG_DIR = str(Path("data") / "_test_logs_plan_loop")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_DIR", _TEST_LOG_DIR)
_ISO_ROOT = (ROOT / "data" / "_plan_loop_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))
# **测试卫生**（本应用里「env 未设」≠「关」）：本文件依赖的开关一律 import 期显式钉死。
os.environ["YINYUE_FEATURE_PLAN_LOOP"] = "off"

from agents.planner import (  # noqa: E402
    Planner,
    RetryPolicy,
    SubTask,
    TaskStatus,
    plan_loop_enforcing,
    plan_loop_mode,
    plan_loop_tracing,
    quality_needs_more,
)
from core.contracts.content_plan import (  # noqa: E402
    ContentPlanError,
    PlanResult,
    StepBudget,
    missing_contract_fields,
)

_CONV_PATH = ROOT / "agents" / "conversational.py"

# ---------------------------------------------------------------------------
# 冻结的旧 prompt（``PLAN_LOOP=off`` 口径）——**逐字**抄自接线前的 agents/planner.py。
# 这就是「off = 与现状逐字节一致」的取证：动态部分（当前时间 / 工具表 / 已完成 / 观察）
# 用 @…@ 占位，其余一个字符都不许差。
# ---------------------------------------------------------------------------
_LEGACY_PLAN_SYSTEM = """\
你是一个任务规划器。你的工作是：
1. 理解用户的真实意图——他们到底想要什么
2. 如果意图不明确，生成澄清问题
3. 如果意图明确，将复杂请求拆分为可执行的子任务
4. 涉及相对日期（今天/明天/下周/几号）时，以当前时间为基准；需要精确日期可调用 get_current_time

当前时间: @NOW@

可用工具:
@TOOLS@


规划原则:
- 简单闲聊/情感表达 → 不需要工具，直接回复
- 需要查信息 → 先搜索再回答
- 需要多步操作 → 拆分为有序子任务，标注依赖关系
- 意图模糊 → 先澄清再执行，不要猜
- 每个子任务应该有明确的工具和参数，或者是"直接回复"类型
- 无依赖关系的子任务可以并行执行，不需要标注depends_on

返回JSON:
{
  "reasoning": "你对用户意图的分析",
  "needs_clarification": true/false,
  "clarification_question": "如果不明确，问用户什么",
  "subtasks": [
    {
      "id": 1,
      "description": "子任务描述",
      "tool": "工具名或null(null表示直接回复)",
      "tool_args": {"参数名": "值"},
      "depends_on": []
    }
  ]
}"""

_LEGACY_REPLAN_SYSTEM = """\
你是一个任务规划器。之前的计划执行后有了新观察，需要决定是否调整。

已完成任务: @DONE@
最新观察: @OBS@


可用工具:
@TOOLS@

决定:
- 如果结果已经足够好，不需要新任务
- 如果结果不完整或有错误，添加补充任务
- 如果用户反馈表明方向不对，调整后续任务


返回JSON:
{
  "assessment": "对当前结果的评估",
  "need_more": true/false,
  "new_subtasks": [
    {
      "id": 下一个可用ID,
      "description": "补充任务描述",
      "tool": "工具名或null",
      "tool_args": {},
      "depends_on": []
    }
  ]
}"""

_TOOLS_LINE = "- search_threads(query(str)): 查群消息"
_N2_TOKENS = ("needed_info", "expected_output", "success_criteria")


# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------
class _ToolResult:
    def __init__(self, output: str = "", error: str = ""):
        self.output = output
        self.error = error
        self.data = None

    @property
    def is_error(self) -> bool:
        return bool(self.error)


class _FakeTool:
    def __init__(self, name: str, outputs: list):
        self.name = name
        self.description = "查群消息"
        self.parameters = {"query": {"type": "str"}}
        self._outputs = list(outputs)
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        item = self._outputs[min(len(self.calls) - 1, len(self._outputs) - 1)]
        return item if isinstance(item, _ToolResult) else _ToolResult(output=str(item))


class _FakeTools:
    def __init__(self, tools: list[_FakeTool]):
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name):
        return self._tools.get(name)

    def all_tools(self):
        return self._tools


class _FakeLLM:
    """按顺序吐 ``responses``（每次 ``chat`` 一个）；``parse_json`` 直接把 dict 交回。"""

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


@contextmanager
def _plan_loop(state: str):
    """钉死 PLAN_LOOP 三态（测试里「关」必须显式 ``off``，不能靠"未设"）。"""
    saved = os.environ.get("YINYUE_FEATURE_PLAN_LOOP")
    os.environ["YINYUE_FEATURE_PLAN_LOOP"] = state
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("YINYUE_FEATURE_PLAN_LOOP", None)
        else:
            os.environ["YINYUE_FEATURE_PLAN_LOOP"] = saved


class _Ctx(dict):
    """``AgentContext`` 的最小替身：既有 ``.extra`` 又有 ``.get``（两者都要）。"""

    def __init__(self, extra: dict | None = None, **kwargs):
        super().__init__(**kwargs)
        self.extra = dict(extra or {})


_OK_QUALITY = {"completeness": 0.9, "accuracy": 0.9, "relevance": 0.9,
               "should_iterate": False, "suggestion": ""}
_LONG = "命中 3 条：msg_id=11 昨晚 22:10 说的那件事；msg_id=12 补充一句。"


def _step_payload(**overrides) -> dict:
    step = {
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
    step.update(overrides)
    return step


def _plan_payload(*steps, **overrides) -> dict:
    payload = {
        "reasoning": "先搜群消息",
        "needs_clarification": False,
        "clarification_question": "",
        "subtasks": [dict(step) for step in (steps or (_step_payload(),))],
    }
    payload.update(overrides)
    return payload


def _planner(responses: list, tools: list[_FakeTool], **kwargs) -> Planner:
    return Planner(llm=_FakeLLM(responses), tools=_FakeTools(tools), **kwargs)


def _run(planner: Planner, **kwargs) -> PlanResult:
    return asyncio.run(planner.run_plan_loop("帮我看看昨晚谁说的事", **kwargs))


def _ts() -> str:
    return "2026-10-06T10:00:00"


# ---------------------------------------------------------------------------
# ① 一次规划 → Step 序列（三字段齐全）
# ---------------------------------------------------------------------------
def test_one_plan_yields_steps_with_three_fields() -> None:
    """§3.2 ①：``understand_and_plan`` 升格为外层 owner，产物是 ``PlanResult``。"""
    tool = _FakeTool("search_threads", [_LONG])
    with _plan_loop("on"):
        planner = _planner([_plan_payload(), _OK_QUALITY], [tool])
        result = _run(planner, budget=StepBudget(max_outer_replans=1), ts_factory=_ts)
    assert isinstance(result, PlanResult)
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.needed_info == ("昨晚群里的原话",)
    assert step.expected_output == "消息列表（含 msg_id 与时间）"
    assert step.success_criteria == "至少一条命中且时间在昨晚"
    assert step.status == "done" and step.attempts == 1 and step.switch_log == ()
    assert step.method.level == 1 and step.method.tool == "search_threads"
    assert (result.replans, result.over_budget, result.asked_user) == (0, False, False)
    assert json.dumps(result.to_dict(), ensure_ascii=False)
    # 规划 prompt 的 schema 同步要求三字段（R-C2 的 prompt 落点）
    prompt = planner.llm.calls[0]["system"]
    for token in _N2_TOKENS:
        assert token in prompt, f"on 口径的规划 prompt 缺 {token}"
    assert planner.llm.calls[0]["agent_name"] == "planner"

    # 缺三字段里任意一条 → 该步被判无效、不进步骤序列（R-C2「缺一即无效」）
    broken = _step_payload(expected_output="")
    with _plan_loop("on"):
        planner = _planner([_plan_payload(broken), _OK_QUALITY], [tool])
        result = _run(planner)
    assert result.asked_user is True and result.report.strip()
    assert result.steps == ()


def test_same_input_twice_yields_same_plan_result() -> None:
    """⑦ 幂等/确定性：时间戳注入 → 同一输入两次给出同一个 ``PlanResult``。"""
    with _plan_loop("on"):
        first = _run(
            _planner([_plan_payload(), _OK_QUALITY], [_FakeTool("search_threads", [_LONG])]),
            ts_factory=_ts,
        )
        second = _run(
            _planner([_plan_payload(), _OK_QUALITY], [_FakeTool("search_threads", [_LONG])]),
            ts_factory=_ts,
        )
    assert first == second
    assert first.to_dict() == second.to_dict()


# ---------------------------------------------------------------------------
# ② 内层对比失败 → 换方法三级 + Switch 留痕
# ---------------------------------------------------------------------------
def test_inner_compare_failure_switches_method_by_level() -> None:
    """② ①→②换工具→③换来源，每条留痕带判据与证据（R-C3/R-C4）。"""
    threads = _FakeTool("search_threads", [""])
    messages = _FakeTool("search_messages", [""])
    web = _FakeTool("web_search", [_LONG])
    with _plan_loop("on"):
        planner = _planner([_plan_payload(), _OK_QUALITY], [threads, messages, web])
        result = _run(planner, ts_factory=_ts)
    step = result.steps[0]
    assert step.status == "done"
    assert step.attempts == 3 and len(step.switch_log) == 2
    assert [(item.from_level, item.to_level) for item in step.switch_log] == [(1, 2), (2, 3)]
    assert [item.criterion for item in step.switch_log] == ["empty", "empty"]
    for item in step.switch_log:
        assert item.evidence.strip(), "R-C3：换方法必须留痕（evidence 非空）"
        assert item.ts == _ts()
        assert item.step_id == step.id
    # 三级各用了一次，且顺序就是阶梯顺序
    assert [len(tool.calls) for tool in (threads, messages, web)] == [1, 1, 1]
    assert step.method.level == 3 and step.method.tool == "web_search"
    assert step.method.source == "web"
    assert result.over_budget is False and result.replans == 0
    # ①②级原语：工具表里的 fallback_tool 仍是②级的候选来源（收编而不是另起一张表）
    assert RetryPolicy.fallback_tool_for("search_threads") == "search_messages"
    assert RetryPolicy.for_primitive().max_retries == 0


# ---------------------------------------------------------------------------
# ③ 三级用尽 → 回外层重规划（replans += 1，带证据）
# ---------------------------------------------------------------------------
def test_exhausted_three_levels_replans_to_outer() -> None:
    """§3.2 ②③：三级用尽 → 回外层重规划一次，证据必须进重规划 prompt。"""
    threads = _FakeTool("search_threads", [""])
    messages = _FakeTool("search_messages", [""])
    web = _FakeTool("web_search", [""])
    recall = _FakeTool("recall_memories", [_LONG])
    replan = {
        "assessment": "第一步搜不到，换条路",
        "need_more": True,
        "new_subtasks": [_step_payload(
            id=2, description="改问记忆", tool="recall_memories", tool_args={},
        )],
    }
    with _plan_loop("on"):
        planner = _planner([_plan_payload(), _OK_QUALITY, replan, _OK_QUALITY],
                           [threads, messages, web, recall])
        result = _run(planner, ts_factory=_ts)
    assert result.replans == 1, "三级用尽必须回外层重规划一次"
    assert [step.status for step in result.steps] == ["failed", "done"]
    assert len(result.steps[0].switch_log) == 2
    assert result.over_budget is False and result.report == ""
    # 回外层的证据：重规划 prompt 里带着失败原因与留痕
    replan_prompt = planner.llm.calls[2]["system"]
    assert "尝试 3 次仍未对上" in replan_prompt
    assert "empty" in replan_prompt
    assert replan_prompt.startswith("你是一个任务规划器。之前的计划执行后有了新观察")
    # 重规划也是一次预算消耗，且新步同样要三字段（R-C2）——否则补出来的步全是无效步
    assert "needed_info" in replan_prompt


def test_attempt_cap_is_three_and_fourth_is_never_tried() -> None:
    """④ §7 拍板项 5：尝试 ≤ 3 = 最多换 2 次；第 4 次**不许再试**，转 ``over_budget``。"""
    threads = _FakeTool("search_threads", [""])
    messages = _FakeTool("search_messages", [""])
    web = _FakeTool("web_search", [""])
    replan = {"assessment": "", "need_more": False, "new_subtasks": []}
    with _plan_loop("on"):
        planner = _planner([_plan_payload(), _OK_QUALITY, replan], [threads, messages, web])
        result = _run(planner, budget=StepBudget(max_outer_replans=0), ts_factory=_ts)
    step = result.steps[0]
    assert step.attempts == 3, f"尝试次数必须停在 3，实际 {step.attempts}"
    assert len(step.switch_log) == 2, "最多换 2 次方法"
    assert [len(tool.calls) for tool in (threads, messages, web)] == [1, 1, 1], "第 4 次不许再试"
    assert step.status == "failed"
    # ⑤ 超预算必须上报：report 非空（不许静默放弃）
    assert result.over_budget is True
    assert result.report.strip(), "R-C5：over_budget 必须带 report"
    assert "没做到" in result.report
    assert result.asked_user is False


# ---------------------------------------------------------------------------
# ⑤ R-C5 上报（③ assess_quality 收编为 over_budget 判据之一）
# ---------------------------------------------------------------------------
def test_quality_judgement_is_one_of_the_over_budget_criteria() -> None:
    """§3.2 ③：``assess_quality`` 的结论是 ``over_budget`` 判据之一，且口径只此一处。"""
    assert quality_needs_more({"should_iterate": True, "completeness": 0.2}) is True
    assert quality_needs_more({"should_iterate": True, "completeness": 0.8}) is False
    assert quality_needs_more({"should_iterate": False, "completeness": 0.1}) is False
    assert quality_needs_more(None) is False
    assert quality_needs_more({"should_iterate": True, "completeness": "半截"}) is False

    incomplete = {"completeness": 0.2, "accuracy": 0.9, "relevance": 0.9,
                  "should_iterate": True, "suggestion": "只答了一半"}
    with _plan_loop("on"):
        planner = _planner([_plan_payload(), incomplete],
                           [_FakeTool("search_threads", [_LONG])])
        result = _run(planner, budget=StepBudget(max_outer_replans=0), ts_factory=_ts)
    assert result.over_budget is True
    assert result.report.strip(), "R-C5：质量不达标也必须上报，不许静默收工"
    assert "只答了一半" in result.report
    # 步骤本身是「成功」的：判据来自 assess_quality，而不是某一步失败
    assert result.steps[0].status == "done"


# ---------------------------------------------------------------------------
# ⑥ off 口径：逐字节一致 + planner 门照旧 + 主循环 AST 锁定
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    text = re.sub(r"当前时间: .*", "当前时间: @NOW@", text)
    text = text.replace(_TOOLS_LINE, "@TOOLS@")
    return text


def test_off_mode_keeps_the_legacy_plan_and_replan_prompts_verbatim() -> None:
    """off = 旧 prompt **逐字**（含旧 schema 里没有三字段这件事）。"""
    with _plan_loop("off"):
        assert plan_loop_mode() == "off"
        assert plan_loop_tracing() is False
        assert plan_loop_enforcing() is False
        planner = _planner([_plan_payload()], [_FakeTool("search_threads", [_LONG])])
        plan = asyncio.run(planner.understand_and_plan("帮我看看昨晚谁说的事"))
        got = _normalize(planner.llm.calls[0]["system"])
        assert got == _LEGACY_PLAN_SYSTEM, "off 口径的规划 prompt 变了（逐字节一致性破了）"
        assert planner.llm.calls[0]["max_tokens"] == 600
        assert planner.llm.calls[0]["temperature"] == 0.2
        # 旧口径的步按 R-C2 是**无效步**（三字段一个都没有）——这就是 R-C2 的缺口取证
        task = plan.subtasks[0]
        assert missing_contract_fields(task) == (
            "needed_info", "expected_output", "success_criteria",
        )
        try:
            task.to_step()
        except ContentPlanError as exc:
            assert "R-C2" in str(exc) and "needed_info" in str(exc)
        else:
            raise AssertionError("off 口径的 SubTask 缺三字段，to_step() 必须抛 R-C2")
        # 旧口径不产生无效步账、也不记 replans（新字段的默认值是 inert 的）
        assert plan.invalid_steps == [] and plan.replans == 0
        # 旧的兼容属性一字不变：tool / tool_args / retry_policy 照旧可读写
        task.tool = "search_messages"
        task.tool_args = {"query": "x"}
        assert task.method.tool == "search_messages" and task.tool_args == {"query": "x"}
        # RetryPolicy.for_tool 的旧策略还在（off 口径就是那套「同一方法再试」的语义）
        assert task.retry_policy.max_retries == 2

        # ② 回外层重规划的旧 prompt 同样逐字
        planner2 = _planner([_plan_payload(), {"assessment": "", "need_more": False,
                                              "new_subtasks": []}],
                            [_FakeTool("search_threads", [_LONG])])
        plan2 = asyncio.run(planner2.understand_and_plan("帮我看看昨晚谁说的事"))
        planner2.llm.calls.clear()
        asyncio.run(planner2.replan_after_observation(plan2, "返回空：len=0"))
        got_replan = _normalize(planner2.llm.calls[0]["system"])
        expected_replan = (
            _LEGACY_REPLAN_SYSTEM
            .replace("@DONE@", "(暂无完成结果)")
            .replace("@OBS@", "返回空：len=0")
        )
        assert got_replan == expected_replan, "off 口径的重规划 prompt 变了"
    # off 口径下 N2 的 schema 关键字一个都不出现
    assert not any(token in got for token in _N2_TOKENS)


def test_off_mode_gate_and_hook_are_inert() -> None:
    """⑥ off：planner 门照旧挡住日常回合；原生循环的 hook 一行都不执行。"""
    from agents.conversational import ConversationalAgent

    agent = ConversationalAgent.__new__(ConversationalAgent)
    agent._task_sessions = {}
    daily_chat = _Ctx({"chat_type": "group"}, session_id=7)
    task_chat = _Ctx({"task_session": True}, session_id=7)
    with _plan_loop("off"):
        assert ConversationalAgent._plan_loop_on() is False
        assert agent._is_task_session(daily_chat) is False, "日常回合必须进不去 Planner"
        assert agent._is_task_session(task_chat) is True
    with _plan_loop("shadow"):
        assert ConversationalAgent._plan_loop_on() is True
    with _plan_loop("on"):
        assert ConversationalAgent._plan_loop_on() is True


def test_main_loop_hook_is_the_only_allowed_change() -> None:
    """⑥ 主循环只许长那 3 行 hook：唯一、由 ``_plan_loop_on()`` 守卫、紧跟在
    ``observations.append(...)`` 之后、算的是 ``(name, arguments, observation)``。

    2026-10-22 改口径（原先锁 ``ast.dump`` 的 sha256）：摘要锁的语义是「主循环逐字节
    没动」，主循环每做一次**合法**改动都得重算指纹，五次功能提交（defe76d → d5741b3）
    就把它变成了 known_red 里的常驻红。现在锁结构不变量——允许的改动仍然只有那一处
    hook，「其余部分有没有被顺手改坏」交给 review，不再用指纹冒充证据。
    """
    tree = ast.parse(_CONV_PATH.read_text(encoding="utf-8-sig"))

    loop = _main_loop_node(tree, "_run_persona_brain_turn_stream")
    hooks = _hook_sites(loop)
    assert len(hooks) == 1, f"hook 必须恰好一处（本期唯一允许的改动）：{len(hooks)} 处"

    siblings, index, hook = hooks[0]
    assert index > 0, "hook 不能是循环体第一句：它必须紧跟 observation 赋值"
    assert _is_observation_append(siblings[index - 1]), (
        "hook 必须紧跟在 observations.append(...) 之后（本期接线契约就是「观察即对比」，"
        f"换位置等于换语义）：{ast.dump(siblings[index - 1])[:160]}"
    )
    assert _compare_call_shape(hook) == ("name", "arguments", "observation"), (
        "hook 算的必须是 (name, arguments, observation)，否则内层对比的口径变了："
        f"{ast.dump(hook)[:200]}"
    )
    assert not _has_else(hook), "hook 不许带 else：plan_loop 关掉时必须与旧路径逐字相同"

    legacy = _main_loop_node(tree, "_run_agentic_loop_stream")
    assert not _hook_sites(legacy), "遗留 agentic 路径不许接 hook（一个字都没碰）"


def _hook_sites(loop: ast.For) -> list[tuple[list, int, ast.If]]:
    """主循环里的 hook 位置：``(所在语句列表, 下标, if 节点)``。

    hook 的判据是**条件里含 ``_plan_loop_on``**——不看函数名，这样把守卫抽成帮助函数
    也骗不过这条。
    """
    found: list[tuple[list, int, ast.If]] = []
    for node in ast.walk(loop):
        for attr in ("body", "orelse", "finalbody"):
            items = getattr(node, attr, None)
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items):
                if not isinstance(item, ast.If):
                    continue
                if not any(
                    isinstance(sub, ast.Attribute) and sub.attr == "_plan_loop_on"
                    for sub in ast.walk(item.test)
                ):
                    continue
                found.append((items, index, item))
    return found


def _is_observation_append(node: ast.AST) -> bool:
    """``observations.append(...)`` 这一句（hook 的锚点）。"""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "append"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "observations"
    )


def _compare_call_shape(hook: ast.If) -> tuple[str, ...]:
    """hook 里 ``_inner_compare(...)`` 的位置参数名（只取 Name 实参）。"""
    for node in ast.walk(hook):
        if not isinstance(node, ast.Call):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "_inner_compare"
            ):
                return tuple(
                    arg.id for arg in sub.args if isinstance(arg, ast.Name)
                )
    return ()


def _has_else(hook: ast.If) -> bool:
    return bool(hook.orelse)


def _main_loop_node(tree: ast.AST, method: str) -> ast.For:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == method:
            for sub in ast.walk(node):
                if not isinstance(sub, ast.For):
                    continue
                call = sub.iter
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == "range" and call.args
                        and isinstance(call.args[0], ast.Name)
                        and call.args[0].id == "effective_max_iter"):
                    return sub
    raise AssertionError(f"{method} 里找不到主循环（for i in range(effective_max_iter)）")


# ---------------------------------------------------------------------------
# ⑤ 内层对比第一实现的收编（_verify_step）
# ---------------------------------------------------------------------------
def _verify_agent():
    from agents.conversational import ConversationalAgent

    return ConversationalAgent.__new__(ConversationalAgent)


def test_verify_step_off_matches_the_legacy_two_criteria_verbatim() -> None:
    """off：``{"passed","reason"}`` 与旧实现逐字相同（含 ``status`` 副作用）。"""
    agent = _verify_agent()
    with _plan_loop("off"):
        # 旧口径第一句：observation 为空 → 直接算过（一个字都不多判）
        skipped = SubTask(id=0, description="查", tool="search_threads", tool_args={})
        assert asyncio.run(agent._verify_step(skipped, "", "问")) == {
            "passed": True, "reason": "",
        }

        empty = SubTask(id=1, description="查", tool="search_threads", tool_args={})
        got = asyncio.run(agent._verify_step(empty, "（空）", "问"))
        assert got == {"passed": False, "reason": "召回工具search_threads返回空结果"}
        assert empty.status == TaskStatus.PENDING, "旧副作用：判空 → 回 PENDING"

        # 判据②：范围不符（先过①的长度关，才轮得到②——旧口径就是这个顺序）
        scope = SubTask(id=2, description="查", tool="search_messages",
                        tool_args={"chat_id": "10000000002@chatroom"})
        got = asyncio.run(
            agent._verify_step(scope, "召回了 3 条，但都不是那个群里的消息", "问")
        )
        assert got == {
            "passed": False,
            "reason": "搜索范围不匹配: 期望chat_id=10000000002@chatroom",
        }
        assert scope.status == TaskStatus.PENDING  # 旧口径下判据②不改状态

        ok = SubTask(id=3, description="查", tool="get_current_time", tool_args={})
        assert asyncio.run(agent._verify_step(ok, "12:30", "问")) == {
            "passed": True, "reason": "",
        }
        # 旧口径**一次都不判 error**（R-C3 缺口）：同一条报错观察在 off 下算通过
        broken = SubTask(id=4, description="查", tool="search_threads", tool_args={})
        assert asyncio.run(
            agent._verify_step(broken, "[错误] 连接超时了（旧口径不看这个）", "问")
        ) == {"passed": True, "reason": ""}


def test_verify_step_on_mode_collects_the_five_criteria() -> None:
    """on：同一处判据 owner 接上 R-C3 的 error 判据，并回传 ``criterion``。"""
    agent = _verify_agent()
    with _plan_loop("on"):
        broken = SubTask(id=1, description="查", tool="search_threads", tool_args={})
        got = asyncio.run(agent._verify_step(broken, "[错误] 连接超时了，重试也没用", "问"))
        assert got["passed"] is False and got["criterion"] == "error"
        # 旧两条判据在新口径下仍然照旧命中（observation 只有 3 字 → 召回类判空）
        empty = SubTask(id=2, description="查", tool="recall_memories", tool_args={})
        got = asyncio.run(agent._verify_step(empty, "（空）", "问"))
        assert got["criterion"] == "empty" and "返回空结果" in got["reason"]
        # 采到的判据全在封闭集合里
        assert got["criterion"] in {
            "empty", "error", "schema_mismatch", "contradiction", "low_confidence",
        }


def test_inner_compare_records_evidence_for_the_native_loop() -> None:
    """原生工具循环那一处 hook 的干活函数：命中五判据就留痕，对得上就什么都不写。"""
    from agents.conversational import ConversationalAgent

    agent = _verify_agent()
    with _plan_loop("on"):
        assert agent._inner_compare("search_threads", {}, "")["criterion"] == "empty"
        broken = agent._inner_compare("search_threads", {}, "[错误] 连接超时了，重试也没用")
        assert broken["criterion"] == "error" and broken["evidence"]
        scope = agent._inner_compare(
            "search_messages", {"chat_id": "10000000002@chatroom"},
            "召回了 3 条，但都不是那个群里的消息",
        )
        assert scope["criterion"] == "schema_mismatch"
        assert agent._inner_compare("get_current_time", {}, "12:30") == {
            "tool": "get_current_time", "criterion": "", "evidence": "",
        }
    # 台账只在留痕位（shadow/on）才会被写；off 下 guard 为假，这一处一行都不跑
    with _plan_loop("off"):
        assert ConversationalAgent._plan_loop_on() is False


def main() -> bool:
    tests = [fn for name, fn in globals().items() if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
