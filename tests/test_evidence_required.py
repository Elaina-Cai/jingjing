# -*- coding: utf-8 -*-
"""件② **该查必查**：声明型断言必须有来源（主人标注 No.10/11）。

事故形态：她把游戏角色当真人、**没去搜索**——计划里一步取数都没有，就直接出一句
关于"谁是谁"的话。本文件守的就是那一格：

* **判据**（唯一 owner）：``core.contracts.content_plan.evidence_verdict``
  —— "这个计划的步骤里有没有一步取数"；
* **结论**（唯一 owner）：``core.tasks.slot_fill.plan_completion``
  —— 与 A1-d「无回执不许当完成」**同一个函数**判出来（``missing_evidence`` 与
  ``missing`` 并列）；出口同样走既有的上报路径（``asked_user`` / ``over_budget`` /
  ``report``）；
* **落点**（计划层）：``agents/planner.py`` 的 ``Step`` / ``PlanResult`` 层面——规划
  prompt 里表达这条不变式（预防），``plan_path`` 记录里带读数（留痕），
  ``Planner._plan_receipt`` **只调用不判定**（与既有回执同一条 handle）。

门控用既有的 ``EXEC_RECEIPT`` / ``PLAN_LOOP``，**不新增开关**：``EXEC_RECEIPT=off``
时``settle_plan_execution`` 第一行就返回 ``None``（逐字节退回接线前）。

**测试卫生**：断言"关"一律显式 ``off``（"env 未设"≠"关"）；开关整组钉死。

Run directly::

    .\\.venv\\Scripts\\python.exe tests\\test_evidence_required.py
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent

# --- import 期钉死：本文件依赖的每一个开关都显式表态（"env 未设"≠"关"）--------
_PINS = {
    "YINYUE_FEATURE_EXEC_RECEIPT": "off",        # 本文件的"关"位：显式 off
    "YINYUE_FEATURE_SLOT_FILL": "on",            # off 位的前置：状态机要在
    "YINYUE_FEATURE_WORK_ITEM_LEDGER": "1",      # 落库/判据的兄弟组一起钉
    "YINYUE_FEATURE_WORK_ITEM_RUNS": "1",
    "YINYUE_FEATURE_PLAN_LOOP": "off",           # 规划层那一半显式 off
    "YINYUE_FEATURE_CLAIM_LEDGER": "off",
}
os.environ.update(_PINS)
_ISO_ROOT = (ROOT / "data" / "_evidence_required_test").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")
os.environ["LOG_DIR"] = str(ROOT / "data" / "_test_logs_evidence_required")

from agents.planner import Plan, SubTask  # noqa: E402
from core.contracts.content_plan import (  # noqa: E402
    Method,
    MethodSource,
    PlanResult,
    Step,
    StepStatus,
    budget_report,
    evidence_verdict,
    is_fetch_step,
)
from core.tasks import slot_fill  # noqa: E402


def _pin() -> None:
    os.environ.update(_PINS)


@contextmanager
def _env(**pairs):
    saved = {key: os.environ.get(key) for key in pairs}
    try:
        for key, value in pairs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# 主人 No.10 的形态（fixture）
# ---------------------------------------------------------------------------
#: 主人那句话：她把游戏角色当真人说了。计划里**一步取数都没有**，只有一步"出话"。
NO_SEARCH_TASK = "那个角色是不是真有其人？你直接说说"
NO_SEARCH_DESC = "直接说这个角色是谁、出自哪里"
#: 有话术痕迹的取值**一个字都不许进代码**（红线：不做出口词表拦截）——它们只出现在
#: 这个 fixture 的字符串里，判据看的是"有没有取数步骤"，不是这些字。
NO_SEARCH_OUTPUT = "这角色是照着真人写的"

CHAT_ID = "10000000001@chatroom"
PLAN_ID = "plan-no-search"


def _no_source_plan() -> Plan:
    """No.10 形态：一步"出话"，**没有任何工具**（= 一步取数都没有）。"""
    return Plan(
        goal=NO_SEARCH_TASK,
        subtasks=[SubTask(
            id=1, description=NO_SEARCH_DESC,
            needed_info=["那个角色是谁"],
            expected_output=NO_SEARCH_OUTPUT,
            success_criteria="把这句话说出去",
            method=Method(level=1, tool="", args={}, source=MethodSource.MEMORY),
        )],
        plan_id=PLAN_ID,
    )


def _sourced_plan() -> Plan:
    """正常形态：**先取数**（联网/记忆检索），再出话。"""
    return Plan(
        goal=NO_SEARCH_TASK,
        subtasks=[
            SubTask(
                id=1, description="联网查这个角色的出处",
                needed_info=["角色名"],
                expected_output="一条查得到的出处",
                success_criteria="返回里有出处",
                method=Method(level=1, tool="web_search",
                              args={"query": "角色 出处"},
                              source=MethodSource.WEB),
            ),
            SubTask(
                id=2, description=NO_SEARCH_DESC,
                needed_info=["第1步查到的出处"],
                expected_output="一句有出处的话",
                success_criteria="说得出来源",
                method=Method(level=1, tool="", args={},
                              source=MethodSource.MEMORY),
                depends_on=[1],
            ),
        ],
        plan_id=PLAN_ID,
    )


def _only_reply_plan() -> Plan:
    """更窄的一格：一步都没有工具（连"出话"都只有一步）。"""
    return Plan(goal="随便聊一句", subtasks=[SubTask(
        id=1, description="回一句话", needed_info=["她刚说了什么"],
        expected_output="一句话", success_criteria="说出去了",
    )], plan_id="plan-chat-only")


# ---------------------------------------------------------------------------
# ① "要断言但无取数步骤" → 计划**不得完成** + 上报
# ---------------------------------------------------------------------------
def test_no_fetch_step_means_not_complete():
    """owner 的判据读数：一步取数都没有 → ``complete=False``，报告点名那几步。"""
    _pin()
    plan = _no_source_plan()
    verdict = evidence_verdict(plan)
    assert verdict["ok"] is False, verdict
    assert verdict["steps"] == (1,), verdict
    assert "来源" in verdict["reason"] or "取数" in verdict["reason"], verdict

    with _env(YINYUE_FEATURE_EXEC_RECEIPT="on"):
        completion = slot_fill.settle_plan_execution(
            plan=plan, context={"chat_id": CHAT_ID}, repo=None)
    assert completion is not None, "有出话步骤、没来源：回执层必须接管"
    assert completion.complete is False, completion.to_dict()
    assert completion.asked_user is True, completion.to_dict()
    assert completion.missing_evidence == (1,), completion.to_dict()
    assert completion.report.strip(), "不许静默放弃：报告必须非空"
    # 上报措辞来自既有 owner（``budget_report`` 的问句），不是这里各拼一句：
    # 报告的头部就是 owner 判据给的理由。
    reason = str(evidence_verdict(plan)["reason"])
    assert reason in completion.report, (reason, completion.report)
    print("  ok ① 无取数步骤 → complete=False + 上报（报告非空、点名第 1 步）")


def test_step_level_reading_of_the_invariant():
    """计划层读数：``Step`` / ``PlanResult`` 与运行时载体（``SubTask`` / ``Plan``）
    判**同一个结论**（没有第二份实现）。"""
    _pin()
    step = Step(
        id=1, description=NO_SEARCH_DESC, needed_info=("那个角色是谁",),
        expected_output=NO_SEARCH_OUTPUT, success_criteria="把这句话说出去",
        method=Method(level=1, tool="", args={}, source=MethodSource.MEMORY),
        status=StepStatus.DONE,
    )
    result = PlanResult(steps=(step,))
    assert evidence_verdict(result)["ok"] is False, evidence_verdict(result)
    assert evidence_verdict(_no_source_plan())["ok"] is False
    # 契约对象与运行时载体的读数一致（同一套鸭子读法）。
    assert evidence_verdict(result)["steps"] == evidence_verdict(_no_source_plan())["steps"]

    fetch = Step(
        id=1, description="联网查出处", needed_info=("角色名",),
        expected_output="一条出处", success_criteria="返回里有出处",
        method=Method(level=1, tool="web_search", args={"query": "x"},
                      source=MethodSource.WEB),
        status=StepStatus.DONE,
    )
    assert evidence_verdict(PlanResult(steps=(fetch,)))["ok"] is True
    assert is_fetch_step(fetch) is True and is_fetch_step(step) is False
    # 「问人」也算取数（把来源交给用户，不是自己编）。
    ask = Step(
        id=1, description="问她", needed_info=("角色名",),
        expected_output="她给的名字", success_criteria="问到了",
        method=Method(level=3, tool="", args={}, source=MethodSource.HUMAN),
        status=StepStatus.DONE,
    )
    assert is_fetch_step(ask) is True, ask.method
    print("  ok ① 计划层读数一致（Step/PlanResult 与 SubTask/Plan 同判）")


def test_over_budget_when_the_unsourced_step_exhausted_its_budget():
    """那份来源缺失的步骤**尝试到顶** → 同时算 ``over_budget``（R-C5 同一个口径）。"""
    _pin()
    plan = _no_source_plan()
    plan.subtasks[0].retry_count = 3          # 尝试 ≤ 3 已用尽
    with _env(YINYUE_FEATURE_EXEC_RECEIPT="on"):
        completion = slot_fill.settle_plan_execution(
            plan=plan, context={"chat_id": CHAT_ID}, repo=None)
    assert completion is not None and completion.complete is False, completion
    assert completion.over_budget is True, completion.to_dict()
    assert completion.report.strip(), completion.to_dict()
    print("  ok ① 来源缺失 + 尝试到顶 → over_budget=True（同一个上报出口）")


# ---------------------------------------------------------------------------
# ② 有取数步骤 → 正常完成
# ---------------------------------------------------------------------------
def test_with_a_fetch_step_the_plan_completes():
    """有取数步骤（web_search / 记忆检索 / 问人）→ 计划正常完成，不产生上报。"""
    _pin()
    plan = _sourced_plan()
    assert evidence_verdict(plan)["ok"] is True, evidence_verdict(plan)

    direct = slot_fill.plan_completion(plan, [])
    assert direct.complete is True, direct.to_dict()
    assert direct.asked_user is False and direct.report == "", direct.to_dict()

    with _env(YINYUE_FEATURE_EXEC_RECEIPT="on"):
        completion = slot_fill.settle_plan_execution(
            plan=plan, context={"chat_id": CHAT_ID}, repo=None)
    # 没有承诺型步骤 + 有来源 → 回执层不接管（与接线前的出口一致）。
    assert completion is None, completion
    print("  ok ② 有取数步骤 → 计划正常完成（零上报）")


def test_ask_human_counts_as_a_source():
    """③级走到「问人」也算取数：那是把来源交给用户，不是自己编。"""
    _pin()
    plan = Plan(goal=NO_SEARCH_TASK, subtasks=[SubTask(
        id=1, description=NO_SEARCH_DESC, needed_info=("角色是谁",),
        expected_output="她给的说法", success_criteria="问出口了",
        method=Method(level=3, tool="", args={}, source=MethodSource.HUMAN),
    )], plan_id=PLAN_ID)
    assert evidence_verdict(plan)["ok"] is True, evidence_verdict(plan)
    assert slot_fill.plan_completion(plan, []).complete is True
    print("  ok ② 「问人」也算来源（阶梯末端不判成无来源）")


# ---------------------------------------------------------------------------
# ③ 结构锁：**同一个 owner**（没有第二份判定）
# ---------------------------------------------------------------------------
def test_same_owner_for_both_invariants():
    """两条不变式都在 ``plan_completion`` 里判：判据只此一处，别处只调不判。"""
    _pin()
    body = inspect.getsource(slot_fill.plan_completion)
    assert "evidence_verdict" in body, "证据判据必须复用 owner"
    assert "is_fetch_step" not in body, \
        "判定不许在结论函数里重写一遍（那是第二份判据）"
    # 出口措辞只有一处：budget_report。
    assert body.count("budget_report(") == 2, body
    # 判据本体只有一个实现：契约里定义、别处不复制。
    for path in (ROOT / "agents").rglob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        assert "def evidence_verdict" not in text, f"{path.name} 复制了判据"
        assert "def is_fetch_step" not in text, f"{path.name} 复制了判据"
    assert "def evidence_verdict" in (
        ROOT / "core" / "contracts" / "content_plan.py").read_text(encoding="utf-8-sig")
    # 计划器**只读结论**：不自己写判据（结构证据）。
    planner = inspect.getsource(sys.modules["agents.planner"])
    assert "evidence_verdict(" in planner and "def evidence_verdict" not in planner
    print("  ok ③ 同一个 owner（判据一处、出口一处、计划器只读结论）")


def test_gating_reuses_existing_switches():
    """门控是既有的 ``EXEC_RECEIPT``（**不新增开关**）：``off`` 时层不接管。"""
    _pin()
    plan = _no_source_plan()
    with _env(YINYUE_FEATURE_EXEC_RECEIPT="off"):
        assert slot_fill.exec_receipt_mode() == "off"
        assert slot_fill.settle_plan_execution(
            plan=plan, context={"chat_id": CHAT_ID}, repo=None) is None, \
            "off 位必须逐字节退回接线前（层不接管）"
    with _env(YINYUE_FEATURE_EXEC_RECEIPT="shadow"):
        shadow = slot_fill.settle_plan_execution(
            plan=plan, context={"chat_id": CHAT_ID}, repo=None)
    assert shadow is None, "shadow 只读数、不改这一轮的对外行为"
    # 新开关一个都不许加：本文件依赖的开关就是既有的那几个。
    # （``EVIDENCE_INDEX`` 是**接线前就有**的索引开关，与本件无关——它必须原样在。）
    import core.features as features

    names = {name for name in dir(features) if name.isupper()}
    assert {name for name in names if "EVIDENCE" in name} == {"EVIDENCE_INDEX"}, names
    assert not [name for name in names if "CITATION" in name or "MUST_SEARCH" in name]
    assert "EXEC_RECEIPT" in names and "PLAN_LOOP" in names, "门控必须是既有开关"
    print("  ok ③ 门控复用 EXEC_RECEIPT（off 不接管 / shadow 只读数 / 零新开关）")


def test_planner_layer_expresses_the_rule():
    """落点在**规划层**：N2 prompt 里写明这条不变式，记录里带读数。"""
    _pin()
    source = (ROOT / "agents" / "planner.py").read_text(encoding="utf-8-sig")
    assert "要断言具体的人、作品或事实" in source, "规划原则必须写明这条"
    assert "plan_evidence_record(" in source, "plan_path 记录要带证据读数"
    assert "plan_evidence(" in source and "def plan_evidence(" in source
    # 规划器的出口**只调用** owner（与回执同一个 handle）。
    plan_receipt = inspect.getsource(
        sys.modules["agents.planner"].Planner._plan_receipt)
    assert "settle_plan_execution(" in plan_receipt, plan_receipt
    assert "evidence_verdict" not in plan_receipt, "出口不许自己判"
    print("  ok ③ 规划层落点：prompt 表达 + 记录读数 + 出口只调 owner")


# ---------------------------------------------------------------------------
# ④ 变异自检：去掉这条不变式 → 用例红；还原 → 绿
# ---------------------------------------------------------------------------
def test_mutation_without_the_invariant_the_plan_completes():
    """**变异自检**：让 owner 的判据恒 ``ok``（= 这条不变式没落地）→ 不得完成的那一格变绿。

    红→绿只能来自这一条判据本身：改了 owner 的结论，同一条 fixture 立刻"正常完成"。
    """
    _pin()
    import core.contracts.content_plan as contract

    original = contract.evidence_verdict
    contract.evidence_verdict = lambda source: {"ok": True, "reason": "", "steps": ()}
    try:
        plan = _no_source_plan()
        assert slot_fill.plan_completion(plan, []).complete is True, \
            "变异没生效（用例失真：结论不是这条判据给的）"
        with _env(YINYUE_FEATURE_EXEC_RECEIPT="on"):
            assert slot_fill.settle_plan_execution(
                plan=plan, context={"chat_id": CHAT_ID}, repo=None) is None, \
                "判据恒 ok 时层不该接管"
    finally:
        contract.evidence_verdict = original
    # 还原：同一条 fixture 立刻回到"不得完成"。
    plan = _no_source_plan()
    assert slot_fill.plan_completion(plan, []).complete is False, "还原后必须复红"
    with _env(YINYUE_FEATURE_EXEC_RECEIPT="on"):
        restored = slot_fill.settle_plan_execution(
            plan=plan, context={"chat_id": CHAT_ID}, repo=None)
    assert restored is not None and restored.complete is False, restored
    print("  ok ④ 变异自检：判据恒 ok → 计划变成「完成」（红）；还原 → 不得完成（绿）")


def test_mutation_treating_every_step_as_a_fetch_is_detected():
    """第二条变异：把"每一步都算取数"（= 判据形同虚设）→ 同一条 fixture 变绿。"""
    _pin()
    import core.contracts.content_plan as contract

    original = contract.is_fetch_step
    contract.is_fetch_step = lambda step: True
    try:
        assert contract.evidence_verdict(_no_source_plan())["ok"] is True
    finally:
        contract.is_fetch_step = original
    assert contract.evidence_verdict(_no_source_plan())["ok"] is False
    print("  ok ④ 变异自检：is_fetch_step 恒真 → 判据失效（被这条用例抓住）")


def test_mutation_in_the_planner_layer_leaves_owner_untouched():
    """第三条变异（接线层）：计划器不看记录 → owner 的结论**仍然**是"不得完成"。

    这条证明"计划器只读结论"：改接线层的读数不影响判据本体（判据不在那里）。
    """
    _pin()
    import agents.planner as planner_mod

    original = planner_mod.plan_evidence
    planner_mod.plan_evidence = lambda source: {"ok": True, "reason": "", "steps": ()}
    try:
        assert planner_mod.plan_evidence_record({})["evidence_ok"] is True
    finally:
        planner_mod.plan_evidence = original
    assert slot_fill.plan_completion(_no_source_plan(), []).complete is False, \
        "判据本体不受接线层读数影响（没有第二份判定）"
    print("  ok ④ 变异自检：接线层读数不影响判据本体")


# ---------------------------------------------------------------------------
# ⑤ 静态守卫：判据里没有"话术词表"（主人红线）
# ---------------------------------------------------------------------------
def test_no_phrase_blacklist_in_the_judgement():
    """结构守卫：判据只认 ``method.tool`` / ``method.source``，不看任何文本。

    断言"关"用显式 ``off``；取值（fixture 里那句话的正文）一个字都不许进判据代码。
    """
    _pin()
    path = ROOT / "core" / "contracts" / "content_plan.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for name in ("is_fetch_step", "evidence_verdict", "_plan_steps_of"):
        fn = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == name)
        for node in ast.walk(fn):
            assert not isinstance(node, ast.Compare) or not any(
                isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
            ) or not any(
                isinstance(side, ast.Constant) and isinstance(side.value, str)
                for side in (node.left, *node.comparators)
            ), f"{name} 里出现了「把某段话术当判据」的成员判定"
    body = inspect.getsource(evidence_verdict) + inspect.getsource(is_fetch_step)
    assert NO_SEARCH_OUTPUT not in body, "取值不许进判据代码（词表枚举形态）"
    assert "description" not in body, "判据不看步骤描述文本"
    # 计划器那一半也没把取值写成词表。
    planner_body = inspect.getsource(sys.modules["agents.planner"].plan_evidence)
    assert NO_SEARCH_OUTPUT not in planner_body
    print("  ok ⑤ 判据无词表（只看 tool/source，不看描述文本与取值）")


# ---------------------------------------------------------------------------
# 自带 runner（本仓库的测试不走 pytest 收集：python tests/xxx.py）
# ---------------------------------------------------------------------------
def main() -> bool:
    _pin()
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    print("=" * 68)
    print("件② 该查必查（声明型断言必须有来源：计划不得完成 + 上报）")
    print("=" * 68)
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
