# -*- coding: utf-8 -*-
"""N2 规划-执行两层循环契约回归：结构 / 校验 / 五判据 / 预算（纯结构，0 新增 LLM 调用）。

对应 ``docs/需求契约.md`` §2.3（R-C1..R-C7）与 ``docs/decisions/架构评估.md`` §3.2：

① **R-C2 唯一判据**：``needed_info`` / ``expected_output`` / ``success_criteria``
   三字段齐全，**逐个缺失**都必须抛 ``ContentPlanError``（三格），
   ``needed_info`` 另加「非空 + 无空串」（空列表不算声明过）；
② **R-C3 触发留痕**：``Switch.evidence`` 必填非空、``ts`` 必填、判据封闭五类；
③ **R-C4 三级**：``level ∈ {1,2,3}`` 封闭、只许按级升级（跳级靠 ``evidence`` 说明）、
   来源阶梯「记忆 → 联网 → 问人」；
④ **R-C5 预算**：``StepBudget`` 写死「尝试 ≤ 3 = 最多换 2 次方法」；
   ``over_budget`` / ``asked_user`` 时 ``report`` 必填——**不许静默放弃**；
⑤ **R-C3 五判据 owner**：``compare_observation`` 的判定顺序与五类命中；
⑥ 契约模块**零生产依赖**（AST 断言 import 列表）、字段冻结、同输入同输出。

Run directly:
    python tests/test_content_plan.py
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
# 隔离前导（照 tests/test_reply_intent_contract.py:37-46）：本文件只用纯函数，
# 但保持同一纪律，万一以后加了走 bootstrap 的断言也不会碰到生产库。
_TEST_LOG_DIR = str(Path("data") / "_test_logs_content_plan")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_DIR", _TEST_LOG_DIR)
_ISO_ROOT = (ROOT / "data" / "_content_plan_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))
# **测试卫生**（本应用里「env 未设」≠「关」）：本文件依赖的开关一律 import 期显式钉死。
os.environ["YINYUE_FEATURE_PLAN_LOOP"] = "off"

from core import features  # noqa: E402
from core.contracts.content_plan import (  # noqa: E402
    CONTRACT_FIELDS,
    DEFAULT_STEP_BUDGET,
    LEVELS,
    MAX_ATTEMPTS_PER_STEP,
    MAX_METHOD_SWITCHES_PER_STEP,
    SOURCE_LADDER,
    ContentPlanError,
    Method,
    MethodSource,
    PlanResult,
    Step,
    StepBudget,
    StepStatus,
    Switch,
    SwitchCriterion,
    can_switch_method,
    compare_observation,
    escalate_level,
    is_over_budget,
    missing_contract_fields,
    next_source,
    switch_step,
    validate_method,
    validate_step,
)

MODULE_PATH = ROOT / "core" / "contracts" / "content_plan.py"


# ---------------------------------------------------------------------------
# 样板
# ---------------------------------------------------------------------------
def _method(**overrides) -> Method:
    base = {"level": 1, "tool": "search_threads", "args": {"query": "昨晚"}, "source": "memory"}
    base.update(overrides)
    return Method(**base)


def _step_kwargs(**overrides) -> dict:
    base = {
        "id": 1,
        "description": "查一下昨晚谁提过这件事",
        "needed_info": ("昨晚群里的原话",),
        "expected_output": "消息列表（含 msg_id 与时间）",
        "success_criteria": "至少一条命中且 chat_id 对得上",
        "method": _method(),
    }
    base.update(overrides)
    return base


def _step(**overrides) -> Step:
    return Step(**_step_kwargs(**overrides))


def _switch(**overrides) -> Switch:
    base = {
        "step_id": 1,
        "from_level": 1,
        "to_level": 2,
        "criterion": "empty",
        "evidence": "检索返回空：len=0（工具 search_threads）",
        "ts": "2026-10-06T10:00:00",
    }
    base.update(overrides)
    return Switch(**base)


def _expect_error(fn, *, contains: str | None = None) -> str:
    """要求 fn() 抛 ContentPlanError，返回消息供继续断言（不抛 / 抛错类型都判失败）。"""
    try:
        fn()
    except ContentPlanError as exc:
        message = str(exc)
        if contains is not None:
            assert contains in message, f"错误消息里应有 {contains!r}，实际：{message}"
        return message
    except Exception as exc:  # noqa: BLE001 - 断言「抛的是契约错误」才是重点
        raise AssertionError(f"应抛 ContentPlanError，实际抛 {type(exc).__name__}: {exc}")
    raise AssertionError("应当抛 ContentPlanError，但没有抛")


# ---------------------------------------------------------------------------
# ① R-C2 唯一判据：三字段逐个缺失（三格）
# ---------------------------------------------------------------------------
def test_each_of_the_three_fields_missing_raises() -> None:
    """三字段**逐个**缺失 → 三格各抛一次，消息带字段名 + R-C2。"""
    assert CONTRACT_FIELDS == ("needed_info", "expected_output", "success_criteria")
    for field in CONTRACT_FIELDS:
        kwargs = _step_kwargs()
        del kwargs[field]
        message = _expect_error(lambda k=kwargs: Step(**k), contains=field)
        assert "R-C2" in message, message
    # 三个一起缺：也必须抛（且报的是第一个字段名，报账顺序稳定）
    empty_kwargs = {"id": 1, "description": "d", "method": _method()}
    assert "needed_info" in _expect_error(lambda: Step(**empty_kwargs))
    # 合法样板本身要被接受（否则上面的红是假的）
    assert _step().needed_info == ("昨晚群里的原话",)
    validate_step(_step())  # 安静通过


def test_three_fields_none_or_blank_are_missing_too() -> None:
    """``None`` / 空白串与「不存在」同判（R-C2「缺一即无效」不看形式看有没有声明）。"""
    _expect_error(lambda: _step(expected_output=None), contains="expected_output")
    _expect_error(lambda: _step(expected_output="   "), contains="expected_output")
    _expect_error(lambda: _step(success_criteria=None), contains="success_criteria")
    _expect_error(lambda: _step(success_criteria="\t\n"), contains="success_criteria")
    _expect_error(lambda: _step(needed_info=None), contains="needed_info")


def test_needed_info_must_be_non_empty_without_blank_items() -> None:
    """``needed_info`` 空列表 / 含空串 / 裸字符串都不合法（R-C2 的字段级形状）。"""
    message = _expect_error(lambda: _step(needed_info=[]), contains="needed_info")
    assert "R-C2" in message
    _expect_error(lambda: _step(needed_info=("昨晚的原话", "")), contains="空字符串")
    _expect_error(lambda: _step(needed_info=("   ",)), contains="空字符串")
    _expect_error(lambda: _step(needed_info="昨晚的原话"), contains="字符串序列")
    assert _step(needed_info=("a", "b")).needed_info == ("a", "b")


def test_missing_contract_fields_is_duck_typed() -> None:
    """判据对 ``Step`` / dict / 鸭子类型对象一视同仁（旧实现迁移时要用它报账）。"""
    assert missing_contract_fields(_step()) == ()
    assert missing_contract_fields({"id": 1}) == CONTRACT_FIELDS
    assert missing_contract_fields(
        {"needed_info": ["x"], "expected_output": "y", "success_criteria": "z"}
    ) == ()
    assert missing_contract_fields(
        {"needed_info": [], "expected_output": "y", "success_criteria": "z"}
    ) == ("needed_info",)
    assert missing_contract_fields(
        {"needed_info": ["x"], "expected_output": " ", "success_criteria": None}
    ) == ("expected_output", "success_criteria")
    assert missing_contract_fields(None) == CONTRACT_FIELDS

    class _Duck:
        needed_info = ("x",)
        expected_output = "y"
        success_criteria = "z"

    assert missing_contract_fields(_Duck()) == ()


# ---------------------------------------------------------------------------
# ② R-C3：Switch 的触发留痕
# ---------------------------------------------------------------------------
def test_switch_requires_evidence_and_timestamp() -> None:
    """``Switch.evidence`` 空 → 抛（R-C3 的要点就是「对不上」不许被吞掉）。"""
    message = _expect_error(lambda: _switch(evidence=""), contains="Switch.evidence")
    assert "R-C3" in message
    _expect_error(lambda: _switch(evidence="   "), contains="Switch.evidence")
    _expect_error(lambda: _switch(evidence=None), contains="Switch.evidence")
    _expect_error(lambda: _switch(ts=""), contains="Switch.ts")
    assert _switch().is_skip is False


def test_switch_criterion_and_levels_are_closed_sets() -> None:
    """五类 ``criterion``、三级 ``level``、五种 ``status``：封闭集合，非法值一律抛。"""
    assert [item.value for item in SwitchCriterion] == [
        "empty", "error", "schema_mismatch", "contradiction", "low_confidence",
    ]
    assert [item.value for item in StepStatus] == [
        "pending", "running", "done", "failed", "skipped",
    ]
    assert LEVELS == (1, 2, 3)
    _expect_error(lambda: _switch(criterion="想不通"), contains="Switch.criterion")
    _expect_error(lambda: _switch(from_level=0), contains="Switch.from_level")
    _expect_error(lambda: _switch(to_level=4), contains="Switch.to_level")
    for criterion in SwitchCriterion:
        assert _switch(criterion=criterion).criterion is criterion
    # 只许按级升级：同级 / 降级都不算「换方法」
    _expect_error(lambda: _switch(from_level=2, to_level=2), contains="必须高于")
    _expect_error(lambda: _switch(from_level=3, to_level=1), contains="必须高于")
    # 跳级可以，但 is_skip 为真（理由写在同一条 evidence 里，R-C4）
    assert _switch(from_level=1, to_level=3, evidence="①→②无候选工具，直接跳③换来源").is_skip is True


def test_step_switch_log_must_match_the_step() -> None:
    """留痕必须挂在对的步上、条数不许多于尝试次数（R-C3/R-C5 要对得上账）。"""
    good = _switch(step_id=1)
    step = Step(**_step_kwargs(attempts=1, switch_log=(good,)))
    assert step.switches_used == 1
    _expect_error(
        lambda: Step(**_step_kwargs(attempts=1, switch_log=(_switch(step_id=9),))),
        contains="step_id",
    )
    _expect_error(
        lambda: Step(**_step_kwargs(attempts=0, switch_log=(good,))),
        contains="switch_log",
    )
    _expect_error(
        lambda: Step(**_step_kwargs(status="paused")), contains="Step.status"
    )


# ---------------------------------------------------------------------------
# ③ R-C4 三级 + 来源阶梯
# ---------------------------------------------------------------------------
def test_method_levels_and_sources_are_closed_sets() -> None:
    """``level`` 三级、``source`` 三取值封闭；②级换工具必须指名工具。"""
    assert [item.value for item in MethodSource] == ["memory", "web", "human"]
    assert SOURCE_LADDER == (MethodSource.MEMORY, MethodSource.WEB, MethodSource.HUMAN)
    validate_method(_method())
    _expect_error(lambda: _method(level=0), contains="Method.level")
    _expect_error(lambda: _method(level="三级"), contains="Method.level")
    _expect_error(lambda: _method(source="梦里"), contains="Method.source")
    _expect_error(lambda: _method(level=2, tool=""), contains="Method.tool")
    _expect_error(lambda: _method(level=3, tool="", source="web"), contains="Method.tool")
    # ③级的「问人」不需要工具（阶梯末端就是没有工具可用了）
    assert _method(level=3, tool="", source="human").is_ask_human is True
    assert _method(level=3, tool="web_search", source="web").is_ask_human is False
    # 按级升一档 / 来源阶梯到顶不越界
    assert [escalate_level(level) for level in LEVELS] == [2, 3, 3]
    assert next_source("memory") is MethodSource.WEB
    assert next_source("web") is MethodSource.HUMAN
    assert next_source("human") is MethodSource.HUMAN
    _expect_error(lambda: next_source("bilibili"), contains="source")


# ---------------------------------------------------------------------------
# ④ R-C5 预算：尝试 ≤ 3 = 最多换 2 次方法
# ---------------------------------------------------------------------------
def test_budget_is_three_attempts_at_most_two_switches() -> None:
    """§7 拍板项 5 写死的口径：尝试 3 次、最多换 2 次（不是 ``max_retries`` 那套）。"""
    assert MAX_ATTEMPTS_PER_STEP == 3
    assert MAX_METHOD_SWITCHES_PER_STEP == 2
    budget = StepBudget()
    assert budget.max_attempts_per_step == 3
    assert budget.max_method_switches_per_step == 2  # 推导值，不是第二个常数
    assert DEFAULT_STEP_BUDGET.max_method_switches_per_step == 2
    _expect_error(
        lambda: StepBudget(max_attempts_per_step=0), contains="max_attempts_per_step"
    )
    _expect_error(lambda: StepBudget(max_outer_replans=-1), contains="max_outer_replans")

    def _at(attempts: int, switches: int) -> Step:
        trail = tuple(
            _switch(from_level=index + 1, to_level=index + 2, evidence=f"第{index + 1}次对不上")
            for index in range(switches)
        )
        return Step(**_step_kwargs(attempts=attempts, switch_log=trail))

    # 第 1 次失败 → 还能换（0 < 2 且 1 < 3）
    assert can_switch_method(_at(1, 0)) is True
    # 第 2 次失败 → 还能换最后一次（1 < 2 且 2 < 3）
    assert can_switch_method(_at(2, 1)) is True
    # 第 3 次失败 → **第 4 次不许再换**：两个上界同时到顶
    third = _at(3, 2)
    assert can_switch_method(third) is False
    assert is_over_budget(third) is True
    assert is_over_budget(_at(2, 1)) is False
    # 换方法次数不可超过尝试次数（形状不变量）
    _expect_error(lambda: _at(1, 2), contains="switch_log")


def test_switch_step_is_pure_and_appends_one_record() -> None:
    """``switch_step`` 返回**新** Step（原对象不动），并追加一条带证据的 Switch。"""
    step = Step(**_step_kwargs(attempts=1))
    new_method = _method(level=2, tool="search_messages")
    moved = switch_step(
        step, method=new_method, criterion="empty", evidence="返回空", ts="2026-10-06T10:00:01"
    )
    assert step.switch_log == () and step.method.level == 1  # 原对象没被就地改
    assert moved.method.level == 2
    assert moved.switches_used == 1
    record = moved.switch_log[0]
    assert (record.from_level, record.to_level) == (1, 2)
    assert record.criterion is SwitchCriterion.EMPTY
    assert record.ts == "2026-10-06T10:00:01"
    # 同输入同输出（可回放）
    again = switch_step(
        step, method=new_method, criterion="empty", evidence="返回空", ts="2026-10-06T10:00:01"
    )
    assert moved == again and moved.to_dict() == again.to_dict()


# ---------------------------------------------------------------------------
# ⑤ R-C3 五判据 owner
# ---------------------------------------------------------------------------
def test_compare_observation_five_criteria() -> None:
    """五类判据各自的命中条件；判定顺序固定（error → empty → …）。"""
    # 对得上 → None
    assert compare_observation(output="命中 3 条消息 msg_id=9 chat_id=123") is None
    # error 优先于一切（报错 + 空 + 形状不符，仍报 error）
    assert compare_observation(
        output="", is_error=True, expects_content=True, expected_keys=("msg_id",)
    ) is SwitchCriterion.ERROR
    # empty 只在 expects_content 时判（否则短答复会被误判成空）
    assert compare_observation(output="", expects_content=True) is SwitchCriterion.EMPTY
    assert compare_observation(output="", expects_content=False) is None
    assert compare_observation(output="x" * 9, expects_content=True) is SwitchCriterion.EMPTY
    assert compare_observation(output="x" * 10, expects_content=True) is None
    # schema_mismatch：声明的字段 / 声明的范围没出现在返回里
    assert compare_observation(
        output="随便一段文字", expected_keys=("msg_id",)
    ) is SwitchCriterion.SCHEMA_MISMATCH
    assert compare_observation(
        output="结果里没有那个群", scope={"chat_id": "10000000002@chatroom"}
    ) is SwitchCriterion.SCHEMA_MISMATCH
    # extra_text 是第二个查找面（旧实现同时看 observation 与 task.result）
    assert compare_observation(
        output="短的", scope={"chat_id": "42"}, extra_text="chat_id=42"
    ) is None
    # contradiction：同键不同值
    assert compare_observation(
        output="x", prior_values={"chat_id": "42"}, observed_values={"chat_id": "43"}
    ) is SwitchCriterion.CONTRADICTION
    assert compare_observation(
        output="x", prior_values={"chat_id": "42"}, observed_values={"chat_id": "42"}
    ) is None
    # low_confidence：低于阈值命中；给的不是有限数 → 判据不清按「对不上」（保守）
    assert compare_observation(output="x", evidence_score=0.2) is SwitchCriterion.LOW_CONFIDENCE
    assert compare_observation(output="x", evidence_score=0.9) is None
    assert compare_observation(
        output="x", evidence_score=float("nan")
    ) is SwitchCriterion.LOW_CONFIDENCE
    assert compare_observation(output="x", evidence_score="不知道") is SwitchCriterion.LOW_CONFIDENCE
    # 阈值是**参数**（评测集校准的落点，不是逻辑里的字面量）
    assert compare_observation(output="x", evidence_score=0.6) is None
    assert compare_observation(
        output="x", evidence_score=0.6, confidence_threshold=0.8
    ) is SwitchCriterion.LOW_CONFIDENCE


# ---------------------------------------------------------------------------
# ⑥ 冻结、PlanResult 的 R-C5 硬条款、零生产依赖
# ---------------------------------------------------------------------------
def test_legal_plan_result_constructs_and_fields_are_frozen() -> None:
    """合法 ``PlanResult`` 可构造；改属性抛（frozen）；同输入同输出、可 JSON 落审计。"""
    step = Step(**_step_kwargs(attempts=2, switch_log=(_switch(),)))
    result = PlanResult(steps=(step,), replans=1)
    assert result.over_budget is False and result.asked_user is False and result.report == ""
    assert result.steps[0].attempts == 2
    assert PlanResult(steps=(step,), replans=1) == result
    assert json.dumps(result.to_dict(), ensure_ascii=False)
    assert result.to_dict()["steps"][0]["needed_info"] == ["昨晚群里的原话"]

    for target, field, value in (
        (result, "replans", 2),
        (step, "attempts", 3),
        (step.method, "tool", "web_search"),
        (step.switch_log[0], "evidence", "改一下"),
    ):
        try:
            setattr(target, field, value)
        except dataclasses.FrozenInstanceError:
            continue
        raise AssertionError(f"{type(target).__name__}.{field} 应当冻结（改属性必须抛）")


def test_over_budget_must_report_never_silent() -> None:
    """R-C5：``over_budget`` / ``asked_user`` 时 ``report`` 必填——不许静默放弃。"""
    step = _step()
    message = _expect_error(
        lambda: PlanResult(steps=(step,), over_budget=True), contains="R-C5"
    )
    assert "report" in message
    _expect_error(
        lambda: PlanResult(steps=(step,), asked_user=True, report="   "), contains="R-C5"
    )
    assert PlanResult(steps=(step,), over_budget=True, report="第 1 步没做到").over_budget is True
    assert PlanResult(steps=(step,), asked_user=True, report="你是说 X 还是 Y？").asked_user is True
    # id 重复在步骤序列里不可解析（depends_on 靠 id 定位）
    _expect_error(
        lambda: PlanResult(steps=(_step(id=1), _step(id=1))), contains="重复"
    )
    # 非 Step 混进来一律拒收（R-C2 的步骤序列只认契约对象）
    _expect_error(lambda: PlanResult(steps=({"id": 1},)), contains="非 Step")


def test_module_has_zero_production_imports() -> None:
    """契约模块**零生产依赖**：只有 stdlib（+ ``typing`` 这类标准库）。"""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert imported, "至少应有 stdlib 导入"
    forbidden = [
        name for name in imported
        if name.split(".")[0] in {"core", "agents", "db", "llm", "web", "scheduler", "notify"}
    ]
    assert not forbidden, f"N2 契约模块不许依赖项目其它层：{forbidden}"
    assert set(imported) <= {
        "__future__", "math", "dataclasses", "enum", "typing",
    }, f"多出来的 import 要逐个说明来路：{sorted(set(imported))}"


def test_flag_constant_is_off_by_default() -> None:
    """P0/期一纪律：flag 定义好但**默认 off**（rollback = 与现状逐字节一致）。"""
    assert features.PLAN_LOOP == "plan_loop"
    assert features.MODE_OFF == "off"
    os.environ["YINYUE_FEATURE_PLAN_LOOP"] = "off"
    assert features.mode(features.PLAN_LOOP, features.MODE_OFF) == "off"


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
