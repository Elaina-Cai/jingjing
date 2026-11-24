from __future__ import annotations

from core.tools import catalog as _CATALOG

import asyncio
import hashlib
import json
import math
import structlog
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

from core import temporal
from core.contracts.content_plan import (
    DEFAULT_EMPTY_MIN_CHARS,
    DEFAULT_STEP_BUDGET,
    LEVEL_PARAMS,
    LEVEL_SOURCE,
    LEVEL_TOOL,
    ContentPlanError,
    Method,
    MethodSource,
    PlanResult,
    Step,
    StepBudget,
    StepStatus,
    Switch,
    SwitchCriterion,
    budget_report,
    can_switch_method,
    compare_observation,
    criterion_reason,
    missing_contract_fields,
    next_source,
)
from safelog import warn

if TYPE_CHECKING:
    # 仅供下方 ToolCache 的字符串注解使用。运行时不需要 —— 本模块有
    # `from __future__ import annotations`（PEP 563），注解不会被求值。
    # 放在 TYPE_CHECKING 下可避免 agents.planner <-> agents.tools.base 循环导入。
    from agents.tools.base import ToolResult

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# N2（规划-执行两层循环）口径常数
# ---------------------------------------------------------------------------
# 依据 docs/decisions/架构评估.md §3.2 与 docs/需求契约.md §2.3（R-C1..R-C7）。

#: R-C3「empty」判据的适用面（**旧口径，逐字保留**）：召回/检索类工具本来就该返内容。
#: 这个集合原先散在 ``agents/conversational.py::_verify_step`` 里（收编前是它的第一条
#: 判据），现在由 N2 侧唯一定义、conversational 侧反向 import —— 免得两处各自漂移
#: （``get_current_time`` 这种短答复不该被当成空结果）。
RECALL_TOOLS = frozenset(
    {"recall_memories", "search_messages", "search_threads", "recall_thread"}
)
#: N2 口径的适用面 = 旧集合 ∪ {``web_search``}。
#: 为什么要加：R-C4 的③级「换来源」会把方法换到联网（``web_search``），
#: 旧集合不盖它就会出现「换了来源但 empty 判据失效」的盲区（实测：三级尝试里
#: 第三次的空返回被判成「对得上」）。旧集合逐字保留给 ``_verify_step`` 的
#: off 口径用，两者是同源的一行差集，不是两张互相漂移的表。
CONTENT_TOOLS = RECALL_TOOLS | {"web_search"}
#: 旧口径第二条判据（「搜索范围不匹配」）的适用面：带 ``chat_id`` 的检索工具。
SEARCH_TOOLS = frozenset({"search_messages", "search_threads"})

#: 工具成功 -> 出稿层短句种子 signal。只列结果本身能判断场景的工具。
SHORT_REPLY_SIGNAL_BY_TOOL: Mapping[str, str] = {
    "create_reminder": "reminder_recorded",
    "create_todo": "task_done",
    "complete_todo": "task_done",
    "notify_user": "task_done",
    "remember": "task_done",
    "web_search": "serious_result",
    "fetch_url": "serious_result",
    "get_weather": "serious_result",
    "search_messages": "serious_result",
    "search_threads": "serious_result",
    "search_vector": "serious_result",
    "search_knowledge": "serious_result",
    "recall_memories": "serious_result",
    "recall_thread": "serious_result",
    "get_contact_info": "serious_result",
    "get_today_analysis": "serious_result",
    "analyze_image": "serious_result",
    "acg_lookup": "serious_result",
    "query_commands": "serious_result",
    "list_todos": "serious_result",
    "get_person_profile": "serious_result",
    "decision_helper": "serious_result",
}

_SHORT_REPLY_SIGNAL_PRIORITY: Mapping[str, int] = {
    "serious_result": 1,
    "task_done": 2,
    "reminder_recorded": 3,
}

#: ``empty`` 判据的阈值（与旧 ``_verify_step`` 的 ``len < 10`` 同值，收编不许悄悄改口径）。
RECALL_EMPTY_MIN_CHARS = DEFAULT_EMPTY_MIN_CHARS

#: ③ 收编口径：``assess_quality`` 的结论是 ``over_budget`` 的**判据之一**（不再是
#: 独立策略）。阈值与旧路径同值（旧代码写的是 ``quality["completeness"] < 0.5``）。
LOW_COMPLETENESS = 0.5

#: 工具失败时原语把错误折进 observation 的前缀（``execute_task_with_retry`` 的
#: ``[错误]`` / ``[异常]`` 两条出口）。原生工具循环也用同一形状，所以它是**通道约定**：
#: 判 error 判据时先认前缀，能拿到 ``ToolResult`` 的地方优先用 ``is_error``。
OBSERVATION_ERROR_PREFIXES = ("[错误]", "[异常]")

#: R-C2 三字段在规划 prompt 里的声明块——**① 规划与 ② 回外层重规划共用同一份**
#: （两处各写一份 schema 就是等着漂移：一处加了字段、另一处没加，补出来的步全是无效步）。
_CONTRACT_SCHEMA_BLOCK = """      "needed_info": ["这一步需要先知道什么（至少一条，非空）"],
      "expected_output": "这一步应当产出什么",
      "success_criteria": "怎么算这一步做成了",
      "source": "memory | web | human","""


def _as_int_or(value: Any, fallback: int) -> int:
    """payload 里的 id 可能缺失/非数字 → 落回 ``fallback``（规划不该因脏 id 整份作废）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        warn("agents/planner.py:124 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return int(fallback)


def _as_ids_payload(value: Any) -> list[int]:
    """``depends_on`` 收敛成整数列表（原样交给契约层做结构校验）。"""
    if value is None or isinstance(value, (str, bytes)):
        return []
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            warn("agents/planner.py:136 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            continue
    return out


def _as_args_payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_needed_info_payload(value: Any) -> list[str]:
    """``needed_info`` 收敛成字符串列表；裸字符串按「一条」处理（不是每字一条）。"""
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        text = str(value).strip()
        return [text] if text else []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def observation_is_error(observation: str) -> bool:
    """observation 字符串是不是「工具失败」那条出口（见 :data:`OBSERVATION_ERROR_PREFIXES`）。"""
    return str(observation or "").startswith(OBSERVATION_ERROR_PREFIXES)


def plan_loop_mode() -> str:
    """``YINYUE_FEATURE_PLAN_LOOP`` 的**三态读法**（唯一读点，照 ``features.mode`` 纪律）。

    * ``off``（默认）→ 一行都不走：与接线前**逐字节一致**（回滚位）；
    * ``shadow``     → 只**判定 + 留痕**（内层对比写审计），不改对外行为；
    * ``on``         → N2 口径生效：三字段 prompt + 按级换方法 + 回外层重规划。
    """
    try:
        from core import features

        return str(features.mode(features.PLAN_LOOP, features.MODE_OFF))
    except Exception:  # noqa: BLE001 - 开关读不出来时按 off（回滚位）处理
        warn("agents/planner.py:176 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def plan_loop_tracing() -> bool:
    """留痕位（``shadow`` 与 ``on`` 都为真）：内层对比要不要跑并写台账。"""
    return plan_loop_mode() != "off"


def plan_loop_enforcing() -> bool:
    """生效位（只有 ``on`` 为真）：N2 的 prompt / 换方法 / 回外层是否真接管。"""
    return plan_loop_mode() == "on"


# ---------------------------------------------------------------------------
# R-C6 条件规划：快路径判定从「关键词/正则」移交「意图层结论」
# ---------------------------------------------------------------------------
# 依据 docs/decisions/架构评估.md §2.4 R-C6：「简单意图且不需要动作/外部信息 → 快路径；
# 否则进规划；**快路径必须 fail-safe，任何不确定都升级到规划**」。
#
# 历史：原来这里要修两个错处——① 路径分类依据是「是不是 task session」
# （agents/conversational.py 的 ``_is_task_session``）；② ``agents/fast_path.py``
# 用 7 组关键词/正则**在做意图判断**，与 R-A1「不许退化为关键词当主判据」冲突。
#
# 2026-10-23 主人定案「语义交回模型」：**关键词分类器整份删除**（``agents/fast_path.py``
# 已不存在，快路径生成入口也早已退役），于是②那条冲突从根上没了；本模块只剩下
# 「这一轮要不要跑规划」这一件事——判据就是 ``plan_loop_enforcing()``，
# R-C6 那个"看 N1 结论决定跑不跑"的门随 N1 一起退役（它读的结论已经不存在了）。

#: 快路径允许的意图（R-C6「简单意图」的封闭集合）：其余一档一律升级到规划。
#: **不含 ``指令``**（要动作）、**不含 ``元话题``**（在说系统本身，要判自己是谁）、
#: **不含 ``情绪宣泄`` / ``提问``**——前者要的是态度而不是答案（让她"直接答"正是
#: 误解高发区），后者按 R-C6 属于"需要外部信息"的候选，**宁可规划**。
#: 这不是"猜着放宽"：集合越窄，fail-safe 越保守（升级永远比误走快路径便宜）。
#: 快路径的置信度门槛：N1 自报的 ``confidence``（三腿合成后的保守值）低于它
#: 一律升级。取值与契约 ``DEFAULT_LOW_CONFIDENCE``（0.55）同源口径，但**不共用
#: 那个常数**：那个是"内层对比"的证据分阈值，这个是"要不要走快路径"的决断阈值，
#: 同值只是当前校准结果，改一个不该带动另一个。
#: ``plan_path`` 审计记录里 ``reason`` 的取值（**一条记录一个理由**，可回放）。
#: ``plan_path`` 记录的 ``reason``：R-C6 门退役后，唯一理由就是"规划层开着"。
PLAN_PATH_REASON = "plan_loop"

#: ``plan_path`` 记录的 kind（落在**既有** JSONL 通道 decision_trace.jsonl，
#: **不新建表**：``core.observability.decision_trace.record_decision``）。
PLAN_PATH_KIND = "plan_path"

#: ``[本轮步骤]`` 块头（``core/context/assembler.py::_BLOCK_POLICY`` 登记项逐字一致）。
CONTENT_PLAN_HEADER = "[本轮步骤]"
#: 进 prompt 的**硬上限**：块头 1 行 + 步骤 ≤3 行 + 上报 ≤1 行（超了从末尾丢）。
#: 只装"这一步做什么"，不装 needed_info / success_criteria——那是留痕与内层对比
#: 用的，进 prompt 只会把 token 花在系统自己看得懂的结构上。
CONTENT_PLAN_MAX_STEPS = 3
CONTENT_PLAN_MAX_LINES = 5

#: ``run_plan_loop`` 在上报出口上给 ``report`` 的**兜底**（**不是判据**）。
#: 判据与措辞都由 owner（``core/tasks/slot_fill.py::plan_completion``）给：它保证
#: ``complete=False`` 时 ``report`` 非空。这一句只在 owner 递回空串这种退化情形下
#: 顶一下——``PlanResult`` 的契约是 ``asked_user=True`` 必须带非空 ``report``
#: （R-C5「不许静默放弃」），所以这里不许留空格子。
_RECEIPT_DENIED_REPORT = "这次没拿到执行回执，我不敢说办好了。要我再试一次，还是先放着？"


# ---------------------------------------------------------------------------
# 装配与留痕（``content_plan`` 的渲染 / ``plan_path`` 的记录通道）
# ---------------------------------------------------------------------------
def _jsonable(value: Any) -> Any:
    """递归成可 JSON 序列化的值；**字符串一律原样**（不重渲染）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    inner = getattr(value, "value", None)
    if isinstance(inner, (bool, int, float, str)):
        return inner
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(dict(to_dict()))
    return str(value)


def _extra_of(context: Any) -> dict[str, Any] | None:
    """``context.extra``（不是 dict 就当没有：宁可不注入，也不许造一个假的袋子）。"""
    source = getattr(context, "extra", None)
    return source if isinstance(source, dict) else None


def _plan_steps(result: Any) -> tuple[Any, ...]:
    """计划里的步骤（``Plan`` / ``PlanResult`` / 载荷 dict 三种形态都读）。"""
    if result is None:
        return ()
    if isinstance(result, Mapping):
        return tuple(result.get("steps") or ())
    return tuple(getattr(result, "steps", ()) or ())


def plan_digest(result: Any) -> str:
    """计划的**稳定摘要**（跨进程可比）：只对"步骤顺序 + 每步做什么"取 sha256 前 12 位。

    刻意不含时间戳与 ``Switch.ts``：那些每轮都不同，取进来摘要就永远对不上，
    `plan_path` 记录里也就比对不出"两轮是不是同一份计划"。
    """
    shape: list[dict[str, Any]] = []
    for item in _plan_steps(result):
        if isinstance(item, Mapping):
            shape.append(
                {
                    "id": item.get("id"),
                    "description": str(item.get("description") or ""),
                    "tool": str((item.get("method") or {}).get("tool") or item.get("tool") or ""),
                }
            )
            continue
        method = getattr(item, "method", None)
        shape.append(
            {
                "id": getattr(item, "id", None),
                "description": str(getattr(item, "description", "") or ""),
                "tool": str(getattr(method, "tool", "") or getattr(item, "tool", "") or ""),
            }
        )
    blob = json.dumps(shape, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _line_budget(text: str, limit: int) -> str:
    """把一段文本裁到 ``limit`` 行（超了从末尾丢，与 ``_cap_lines`` 同一口径）。"""
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    return "\n".join(lines[: max(0, int(limit))])


def content_plan_hint(extra: Any, plan_result: Any = None) -> str:
    """``extra["content_plan"]`` → prompt 里的 ``[本轮步骤]`` 块（**没有就不输出一个字符**）。

    三条纪律（与 ``[在手的事]`` 那个块的既有纪律同一套）：

    1. **先开关**：``PLAN_LOOP`` 非 ``off`` 才可能注入（与写者同源；
       ``off`` 时写者本来也不写，这里是双保险，保证"golden 零 diff"不靠运气）；
    2. **不许编造**：读不到 ``steps`` / 载荷不是 Mapping → 返回 ``""``；
    3. **硬上限** :data:`CONTENT_PLAN_MAX_LINES` 行：块头 1 + 步骤 ≤3 + 上报 ≤1，
       超了从末尾丢（不是靠写代码的人自觉）。

    **它只是"这一轮打算分几步"的可读序列**，不是执行指令：本期内层仍走既有循环
    （方案 A 不改主循环），所以措辞刻意用"打算"。
    """
    if not plan_loop_tracing():
        return ""
    # WP-08 8.2b：typed PlanResult 的 to_dict() 优先；无 carrier 走旧投影。
    payload: Any = None
    if plan_result is not None:
        to_dict = getattr(plan_result, "to_dict", None)
        payload = to_dict() if callable(to_dict) else None
    else:
        source = extra if hasattr(extra, "get") else None
        if source is None:
            return ""
        try:
            payload = source.get("content_plan")
        except Exception:  # noqa: BLE001
            warn("agents/planner.py:335 Exception", err=repr(__import__("sys").exc_info()[1]))
            return ""
    if not isinstance(payload, Mapping):
        return ""
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        return ""
    lines = [CONTENT_PLAN_HEADER + "（这一轮打算分这么几步）"]
    for item in raw_steps[:CONTENT_PLAN_MAX_STEPS]:
        if isinstance(item, Mapping):
            text = str(item.get("description") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            lines.append(f"- {text}")
    report = str(payload.get("report") or "").strip()
    if bool(payload.get("asked_user")) and report:
        lines.append(f"先按这句问：{report}")
    elif report:
        lines.append(f"要如实说清楚：{report}")
    return _line_budget("\n".join(lines), CONTENT_PLAN_MAX_LINES)


def _plan_line(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def plan_execution_hint(execution: Any) -> str:
    """B2：执行读数 → prompt 的 ``[步骤执行结果]`` 块；off/shadow 或无读数 → ""。"""
    if not plan_loop_enforcing():
        return ""
    if not isinstance(execution, Mapping) or not execution:
        return ""
    steps = tuple(execution.get("steps") or ())
    report = _plan_line(execution.get("report"), 200)
    error = _plan_line(execution.get("error"), 200)
    if not steps and not report and not error:
        return ""
    lines = ["[步骤执行结果]"]
    for item in steps[:6]:
        if not isinstance(item, Mapping):
            continue
        status = _plan_line(item.get("status"), 24)
        result = _plan_line(item.get("result"), 120)
        line = (
            f"- 第{item.get('id')}步「"
            f"{_plan_line(item.get('description'), 40)}」{status}"
        )
        if result:
            line += f"：{result}"
        lines.append(line)
    if report:
        lines.append(f"- 上报：{report}")
    if error:
        lines.append(f"- 执行失败：{error}")
    return "\n".join(lines)

def _record_plan_path(record: Mapping[str, Any]) -> bool:
    """落一条 ``plan_path``（**既有通道**：``logs/decision_trace.jsonl``，不新建表）。

    ``force=True`` 是刻意的：这一条不是调试日志，而是 R-C7 的**分路读数**——
    没有它，"快路径 vs 规划路径"的分子分母就无从分组，而契约明文写着
    「没有对比读数，不许宣称快路径合格」。写失败（磁盘满 / 权限）只记日志，
    绝不打断这一轮。**只在规划层开关非 ``off`` 时写**：``off`` 是回滚位，
    回滚位连一个字节的审计都不该多（与"N1 shadow 一个 extra 键都不写"同一纪律）。
    """
    if not plan_loop_tracing():
        return False
    try:
        from core.observability.decision_trace import record_decision

        return bool(record_decision(dict(record), force=True))
    except Exception as exc:  # noqa: BLE001 - 审计失败绝不能拖垮一回合
        log.warning("plan_path_trace_failed", error=str(exc)[:160])
        return False


def quality_needs_more(quality: Mapping[str, Any] | None) -> bool:
    """§3.2 ③：``assess_quality`` 的结论 → 「还没做完」这一个布尔。

    这就是「收编为 ``over_budget`` 判据之一」的落点：旧路径里这段判断写在
    ``_react_with_planner`` 内联表达式里（``should_iterate and completeness < 0.5``），
    现在只有这一处口径，N2 外层直接读它。
    """
    if not quality:
        return False
    try:
        completeness = float(quality.get("completeness", 1.0))
    except (TypeError, ValueError):
        warn("agents/planner.py:424 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        completeness = 1.0
    return bool(quality.get("should_iterate")) and completeness < LOW_COMPLETENESS


# ---------------------------------------------------------------------------
# 证据不变式（**声明型断言必须有取数步骤**；主人标注 No.10/11）
# ---------------------------------------------------------------------------
# 判据**不在本模块**：owner 是 ``core.contracts.content_plan.evidence_verdict``
# （"这个计划的步骤里有没有一步取数"），结论由既有的计划层不变式 owner
# ``core.tasks.slot_fill.plan_completion`` 一次判出（与 A1-d「无回执不许当完成」
# **同一个 owner、同一条上报出口**）。本模块只做两件本层的事：
#
# 1. **规划 prompt 里表达出来**（预防）：把"要断言具体的人/作品/事实就得先有一次取数
#    步骤"写进 N2 的规划原则——模型自己排得出取数步骤，就不必走到"打回"那一步；
# 2. **读数**（留痕）：把 owner 的结论折进 ``plan_path`` 记录（分路读数要能按
#    "这一轮的计划有没有来源"分组），**不改任何出口**——出口仍由 owner 给。
#
# 与 A1-d 的关系：那条守"答应的事有没有回执"（承诺型步骤），这条守"要断言的事有没有
# 来源"（出话步骤）；**没有第二套判定**，本模块一个字节的判据都不写。


def plan_evidence(source: Any) -> dict[str, Any]:
    """本层对"计划有没有来源"的**只读**读数（判据在 owner，这里不重判）。

    返回 owner 的 ``{"ok", "reason", "steps"}``；owner 不可用时返回"判不了"
    （``ok=True`` + 空 steps）——取值方向与 R-C5 的 fail-safe 一致：**判据坏了不许
    凭空多一条打回**（异常不是判据）。
    """
    try:
        from core.contracts.content_plan import evidence_verdict

        return dict(evidence_verdict(source))
    except Exception as exc:  # noqa: BLE001 - 判据不可用 = 这一格不接管
        log.warning("plan_evidence_unavailable", error=str(exc)[:160])
        return {"ok": True, "reason": "", "steps": ()}


def plan_evidence_record(payload: Any) -> dict[str, Any]:
    """``plan_path`` 记录里那两格（**只记读数，不改行为**）。"""
    if not isinstance(payload, Mapping):
        return {}
    verdict = plan_evidence(payload)
    return {
        "evidence_ok": bool(verdict.get("ok")),
        "missing_evidence": [int(item) for item in (verdict.get("steps") or ())],
    }


def _now_ts() -> str:
    """默认时间戳（Switch.ts）。测试一律注入 ``ts_factory`` 以保幂等。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


#: ``TaskStatus``（执行态的 6 取值，含历史遗留的 ``RETRYING``）↔ ``StepStatus``
#: （R-C2 契约的 5 取值）的**唯一**换算表：``RETRYING`` 就是「还在跑」。
_STEP_STATUS: dict[TaskStatus, StepStatus] = {
    TaskStatus.PENDING: StepStatus.PENDING,
    TaskStatus.RUNNING: StepStatus.RUNNING,
    TaskStatus.DONE: StepStatus.DONE,
    TaskStatus.FAILED: StepStatus.FAILED,
    TaskStatus.SKIPPED: StepStatus.SKIPPED,
    TaskStatus.RETRYING: StepStatus.RUNNING,
}
_TASK_STATUS: dict[StepStatus, TaskStatus] = {
    StepStatus.PENDING: TaskStatus.PENDING,
    StepStatus.RUNNING: TaskStatus.RUNNING,
    StepStatus.DONE: TaskStatus.DONE,
    StepStatus.FAILED: TaskStatus.FAILED,
    StepStatus.SKIPPED: TaskStatus.SKIPPED,
}


@dataclass
class RetryPolicy:
    max_retries: int = 2
    backoff_base: float = 1.0
    fallback_tool: str | None = None

    _DEFAULTS: dict[str, "RetryPolicy"] = field(default_factory=dict, repr=False)

    @classmethod
    def for_tool(cls, tool_name: str) -> "RetryPolicy":
        defaults = {
            "search_threads": cls(max_retries=2, fallback_tool="search_messages"),
            "recall_thread": cls(max_retries=1),
            "web_search": cls(max_retries=2),
            "search_messages": cls(max_retries=1),
            "get_today_analysis": cls(max_retries=1),
            "create_reminder": cls(max_retries=0),
            "notify_user": cls(max_retries=1),
        }
        return defaults.get(tool_name, cls())

    @classmethod
    def fallback_tool_for(cls, tool_name: str | None) -> str | None:
        """「这个工具的②级候选是谁」——**唯一**的表（仍是上面那张 ``for_tool`` 表）。

        N2 外层换②级（换工具）时读它，所以升级的**知识**还在原语这里，
        但升级的**判定与留痕**搬到了外层（R-C3/R-C4：触发必须留痕）。
        """
        return cls.for_tool(str(tool_name or "")).fallback_tool

    @classmethod
    def for_primitive(cls) -> "RetryPolicy":
        """④ 级原语口径：**一次尝试，内部不重试、不降级**。

        ``execute_task_with_retry`` 从「独立策略」降为「①/② 级原语」之后，
        N2 路径一律用它构造：预算（尝试 ≤ 3）与换方法（①→②→③）只有外层一个 owner，
        原语里再 retry / 再 fallback 就会让降级**不被留痕**（违反 R-C3/R-C4）。
        """
        return cls(max_retries=0, backoff_base=0.0, fallback_tool=None)


@dataclass(init=False)
class SubTask:
    """N2 契约 :class:`Step` 的**运行时载体**（§3.2 ⑥：``SubTask`` 扩成契约的 ``Step``）。

    分工：``Step`` 是 frozen 契约（构造即校验、可回放）；``SubTask`` 是可变的执行态
    （既有调用方会就地改 ``status`` / ``result`` / ``tool``）。两者用
    :meth:`to_step` / :meth:`from_step` 桥接，``to_step()`` 会跑 R-C2 校验——
    「三字段缺一」的 SubTask 一旦要当 ``Step`` 用就当场抛 ``ContentPlanError``。

    **旧字段名保留为兼容属性**（``tool`` / ``tool_args`` / ``attempts``）：
    读写都落在 ``method`` / ``retry_count`` 上。``__init__`` 是手写的（不是 dataclass
    生成的），就为了**同时**接受新契约关键字与旧关键字——既有调用方
    （``agents/conversational.py::_react_with_planner`` 与 ``agents/self_evolve.py``）
    一行都不用改：``SubTask(id=1, description="…", tool=None, tool_args={})`` 照旧能造。
    """

    id: int
    description: str = ""
    # ---- R-C2 三字段：缺一即 Step 无效（旧 prompt 产出的 SubTask 会缺，
    #      用 :meth:`missing_contract_fields` 报账，见 test_plan_loop 的 off 取证）。
    needed_info: list[str] = field(default_factory=list)
    expected_output: str = ""
    success_criteria: str = ""
    # ---- R-C4 方法：级别 / 工具 / 参数 / 来源的**唯一**载体（tool/tool_args 是它的视图）。
    method: Method = field(default_factory=lambda: Method(level=LEVEL_PARAMS))
    depends_on: list[int] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    data: Any = None
    retry_count: int = 0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    # ---- R-C3 留痕：每次换方法追加一条 Switch（与 Step.switch_log 同一类型）。
    switch_log: list[Switch] = field(default_factory=list)

    def __init__(
        self,
        id: int,
        description: str = "",
        *,
        needed_info: Sequence[str] | None = None,
        expected_output: str = "",
        success_criteria: str = "",
        method: Method | None = None,
        depends_on: Sequence[int] | None = None,
        status: TaskStatus = TaskStatus.PENDING,
        result: str = "",
        data: Any = None,
        retry_count: int = 0,
        retry_policy: RetryPolicy | None = None,
        switch_log: Sequence[Switch] | None = None,
        # ---- 兼容旧字段名（给了 method 时以 method 为准）----
        tool: str | None = None,
        tool_args: Mapping[str, Any] | None = None,
    ) -> None:
        self.id = int(id)
        self.description = str(description or "")
        self.needed_info = [str(item) for item in (needed_info or [])]
        self.expected_output = str(expected_output or "")
        self.success_criteria = str(success_criteria or "")
        self.method = method or Method(
            level=LEVEL_PARAMS,
            tool=str(tool or ""),
            args=dict(tool_args or {}),
            source=MethodSource.MEMORY,
        )
        self.depends_on = [int(item) for item in (depends_on or [])]
        self.status = status
        self.result = str(result or "")
        self.data = data
        self.retry_count = int(retry_count)
        self.retry_policy = retry_policy or RetryPolicy()
        self.switch_log = list(switch_log or [])

    # ---------------------------------------------------------- 兼容属性（旧字段名）
    @property
    def tool(self) -> str | None:
        """兼容旧字段名：``""`` ↔ ``None``（与旧 ``SubTask.tool`` 同口径）。"""
        return self.method.tool or None

    @tool.setter
    def tool(self, value: str | None) -> None:
        self.method = replace(self.method, tool=str(value or ""))

    @property
    def tool_args(self) -> dict[str, Any]:
        """兼容旧字段名：返回 ``method.args`` 的**副本**（改副本不会改方法）。"""
        return dict(self.method.args)

    @tool_args.setter
    def tool_args(self, value: Mapping[str, Any] | None) -> None:
        self.method = replace(self.method, args=dict(value or {}))

    @property
    def attempts(self) -> int:
        """契约 ``Step.attempts`` 的别名 = **已发起的尝试次数**（旧名 ``retry_count``）。"""
        return int(self.retry_count)

    @attempts.setter
    def attempts(self, value: int) -> None:
        self.retry_count = int(value)

    @property
    def switches_used(self) -> int:
        return len(self.switch_log)

    # ---------------------------------------------------------- 契约桥接
    def missing_contract_fields(self) -> tuple[str, ...]:
        """R-C2：还缺哪几个字段（空元组 = 契约合法）。"""
        return missing_contract_fields(self)

    def to_step(self) -> Step:
        """转成 frozen 契约（R-C2 校验在这里跑：三字段缺一即抛 ``ContentPlanError``）。"""
        return Step(
            id=int(self.id),
            description=self.description,
            needed_info=tuple(self.needed_info or ()),
            expected_output=self.expected_output,
            success_criteria=self.success_criteria,
            method=self.method,
            depends_on=tuple(self.depends_on or ()),
            status=_STEP_STATUS.get(self.status, StepStatus.PENDING),
            attempts=int(self.attempts),
            switch_log=tuple(self.switch_log or ()),
        )

    @classmethod
    def from_step(
        cls, step: Step, *, retry_policy: RetryPolicy | None = None
    ) -> "SubTask":
        """从 frozen 契约建可变载体（``retry_policy`` 缺省取**原语口径**）。"""
        return cls(
            id=int(step.id),
            description=step.description,
            needed_info=list(step.needed_info),
            expected_output=step.expected_output,
            success_criteria=step.success_criteria,
            method=step.method,
            depends_on=list(step.depends_on),
            status=_TASK_STATUS.get(StepStatus(step.status), TaskStatus.PENDING),
            retry_count=int(step.attempts),
            switch_log=list(step.switch_log),
            retry_policy=retry_policy or RetryPolicy.for_primitive(),
        )

    @property
    def is_ready(self) -> bool:
        return self.status == TaskStatus.PENDING

    @property
    def is_done(self) -> bool:
        return self.status in (TaskStatus.DONE, TaskStatus.SKIPPED)

    @property
    def can_retry(self) -> bool:
        return self.retry_count < self.retry_policy.max_retries


@dataclass
class Plan:
    goal: str
    needs_clarification: bool = False
    clarification_question: str = ""
    subtasks: list[SubTask] = field(default_factory=list)
    reasoning: str = ""
    plan_id: str = ""
    created_at: float = 0.0
    session_id: int | None = None
    # ---- N2 预算账（R-C5）----
    #: 「回到外层重规划」的次数，只有 :meth:`Planner.replan_after_observation` 加它。
    replans: int = 0
    #: R-C2 的**回外层重规划**理由：哪些步因为三字段缺一被判无效（缺哪几个字段）。
    invalid_steps: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.plan_id:
            self.plan_id = hashlib.md5(
                f"{self.goal}:{time.time()}".encode()
            ).hexdigest()[:12]
        if not self.created_at:
            self.created_at = time.time()

    def to_result(
        self,
        *,
        over_budget: bool = False,
        report: str = "",
        asked_user: bool = False,
    ) -> PlanResult:
        """把计划翻成 N2 契约产物（R-C2 校验在这里跑）。

        ``PlanResult`` 是 frozen 的**唯一产物**；``Plan`` 只是执行期的可变载体，
        两者单向桥接（``SubTask.to_step()``），不存在两套各自记账的台账。
        ``over_budget`` / ``report`` / ``asked_user`` 由外层循环（R-C5 的上报责任）
        传进来——它们说的是「这一轮跑成什么样」，不是计划本身的属性。
        """
        return PlanResult(
            steps=tuple(task.to_step() for task in self.subtasks),
            replans=int(self.replans),
            over_budget=bool(over_budget),
            report=str(report or ""),
            asked_user=bool(asked_user),
        )

    @property
    def all_done(self) -> bool:
        return all(t.is_done for t in self.subtasks)

    @property
    def ready_tasks(self) -> list[SubTask]:
        done_ids = {t.id for t in self.subtasks if t.is_done}
        return [
            t for t in self.subtasks
            if t.is_ready and all(d in done_ids for d in t.depends_on)
        ]

    @property
    def parallel_groups(self) -> list[list[SubTask]]:
        ready = self.ready_tasks
        if not ready:
            return []
        with_deps = [t for t in ready if t.depends_on]
        without_deps = [t for t in ready if not t.depends_on]
        if without_deps:
            result = [without_deps]
            for t in with_deps:
                result.append([t])
            return result
        groups: list[list[SubTask]] = []
        assigned: set[int] = set()
        for t in ready:
            if t.id in assigned:
                continue
            group = [t]
            assigned.add(t.id)
            for other in ready:
                if other.id in assigned:
                    continue
                if set(other.depends_on) == set(t.depends_on):
                    group.append(other)
                    assigned.add(other.id)
            groups.append(group)
        return groups

    def get_task(self, task_id: int) -> SubTask | None:
        for t in self.subtasks:
            if t.id == task_id:
                return t
        return None

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal,
            "reasoning": self.reasoning,
            "needs_clarification": self.needs_clarification,
            # N2 预算账（R-C5）：重规划次数要能读出来，否则「回外层」是黑箱。
            "replans": int(self.replans),
            # R-C2 的无效步理由（哪些步因为三字段缺一被判无效）。
            "invalid_steps": list(self.invalid_steps),
            "subtasks": [
                {
                    "id": t.id, "description": t.description,
                    "tool": t.tool, "tool_args": t.tool_args,
                    "depends_on": t.depends_on, "status": t.status.value,
                    "result": t.result[:200],
                    # R-C1「可读的步骤序列」：三字段 + 方法 + 留痕一起可读
                    # （旧键一个不动，只**追加**，免得打断既有读方）。
                    "needed_info": list(t.needed_info or []),
                    "expected_output": t.expected_output,
                    "success_criteria": t.success_criteria,
                    "method": t.method.to_dict(),
                    "attempts": int(t.attempts),
                    "switch_log": [item.to_dict() for item in (t.switch_log or [])],
                }
                for t in self.subtasks
            ],
        }


class ToolCache:
    def __init__(self, ttl_seconds: float = 120.0, max_entries: int = 50):
        from core.infra.cache import TTLCache
        self._inner: TTLCache = TTLCache(ttl_seconds=ttl_seconds, max_entries=max_entries, label="tool")

    def _key(self, tool_name: str, args: dict) -> str:
        stable = json.dumps(args, sort_keys=True, ensure_ascii=False)
        return f"{tool_name}:{hashlib.md5(stable.encode()).hexdigest()}"

    def get(self, tool_name: str, args: dict) -> "ToolResult | None":
        return self._inner.get(self._key(tool_name, args))

    def put(self, tool_name: str, args: dict, result: "ToolResult"):
        self._inner.put(self._key(tool_name, args), result)

    def invalidate(self, tool_name: str):
        self._inner.invalidate_prefix(f"{tool_name}:")

    @property
    def stats(self):
        return self._inner.stats


class ObservationBuffer:
    def __init__(self, max_tokens: int = 2000):
        self._entries: list[tuple[str, float, int]] = []
        self._max_tokens = max_tokens
        self._current_tokens = 0

    def add(self, observation: str):
        est_tokens = len(observation) // 2
        self._entries.append((observation, time.time(), est_tokens))
        self._current_tokens += est_tokens
        while self._current_tokens > self._max_tokens and len(self._entries) > 1:
            old_obs, old_ts, old_tokens = self._entries.pop(0)
            compressed = self._compress(old_obs)
            comp_tokens = len(compressed) // 2
            self._entries.insert(0, (compressed, old_ts, comp_tokens))
            self._current_tokens = self._current_tokens - old_tokens + comp_tokens
            if comp_tokens < old_tokens:
                break
            else:
                self._entries.pop(0)
                self._current_tokens -= comp_tokens

    def all_observations(self) -> list[str]:
        return [obs for obs, _, _ in self._entries]

    @staticmethod
    def _compress(text: str) -> str:
        lines = text.strip().split("\n")
        if len(lines) <= 2:
            return text[:100] + "..." if len(text) > 100 else text
        return lines[0][:80] + f"... (共{len(lines)}行，已压缩)"

    def clear(self):
        self._entries.clear()
        self._current_tokens = 0


#: B2：日常回合自动执行计划步骤时**只放行只读工具**（保守白名单，唯一 owner）。
# 名单唯一属主在 core/tools/catalog.py（M3：工具目录只许一处）。
PLAN_LOOP_READ_ONLY_TOOLS = _CATALOG.PLAN_LOOP_ALLOWED


class PlanExecToolView:
    """只读工具视图：``run_plan_loop`` 的取工具/换工具候选都只能落进白名单。

    ``Planner`` 读工具表只用 ``.get``（``next_method`` / ``_source_method`` /
    ``execute_task_with_retry``）；其余接口为审计与兼容补齐。
    """

    def __init__(self, tools: Any, allowed: Any = PLAN_LOOP_READ_ONLY_TOOLS):
        self._tools = tools
        self._allowed = frozenset(str(item) for item in (allowed or ()))

    def get(self, name: Any, default: Any = None) -> Any:
        key = str(name or "")
        if key not in self._allowed:
            return default
        getter = getattr(self._tools, "get", None)
        if not callable(getter):
            return default
        try:
            value = getter(key)          # ToolRegistry.get(name) 只收一个参数
        except Exception:  # noqa: BLE001 - 取不到按没有
            warn("agents/planner.py:911 Exception", err=repr(__import__("sys").exc_info()[1]))
            return default
        return value if value is not None else default

    def all_tools(self) -> dict:
        """只读视图的工具表（``think`` 用它拼 schema；发送类工具不在内）。"""
        return {name: tool for name in self.keys() if (tool := self.get(name)) is not None}

    def keys(self) -> list[str]:
        # 兼容生产 ToolRegistry.all_tools() 与测试用 dict.keys()。
        all_tools = getattr(self._tools, "all_tools", None)
        if callable(all_tools):
            raw = all_tools()
        else:
            getter = getattr(self._tools, "keys", None)
            raw = getter() if callable(getter) else ()
        return [str(key) for key in raw if str(key) in self._allowed]

    def __iter__(self):
        return iter(self.keys())

    def __contains__(self, name: Any) -> bool:
        return self.get(name) is not None


def plan_exec_tools(tools: Any) -> PlanExecToolView:
    """把某轮的工具表包成自动执行用的只读视图（B2 的安全 gate）。"""
    return PlanExecToolView(tools)


class Planner:
    def __init__(self, llm, tools, cache: ToolCache | None = None, evolve=None):
        self.llm = llm
        self.tools = tools
        self.cache = cache or ToolCache()
        self.evolve = evolve
        self._context = None

    def set_context(self, context) -> None:
        """Carry the turn context so executed tools can resolve chat/account."""
        self._context = context

    def _apply_context(self, tool) -> None:
        if self._context is not None and hasattr(tool, "set_context"):
            tool.set_context(self._context)

    def _mark_short_reply_signal(self, tool_name: str) -> None:
        """工具成功后给本轮出稿层留一个短句种子 signal（best-effort）。

        优先级：reminder_recorded > task_done > serious_result。
        调用方显式写入的 signal（不在优先级表里）优先，不被工具结果覆盖。
        """
        signal = SHORT_REPLY_SIGNAL_BY_TOOL.get(str(tool_name or ""))
        if not signal:
            return
        context = self._context
        extra = getattr(context, "extra", None) if context is not None else None
        if not isinstance(extra, dict):
            return
        current = str(extra.get("short_reply_signal") or "")
        if current:
            current_rank = _SHORT_REPLY_SIGNAL_PRIORITY.get(current)
            if current_rank is None:
                return
            if current_rank >= _SHORT_REPLY_SIGNAL_PRIORITY.get(signal, 0):
                return
        extra["short_reply_signal"] = signal

    async def understand_and_plan(
        self, user_message: str, context_summary: str = "",
        session_id: int | None = None,
    ) -> Plan:
        """外层 owner 入口（§3.2 ①：``understand_and_plan`` **升格为外层 owner**）。

        两条口径由 ``YINYUE_FEATURE_PLAN_LOOP`` 切：

        * ``off``（默认）→ :meth:`_plan_legacy`：旧 prompt + 旧 ``SubTask`` 字段集，
          与接线前**逐字节一致**（回滚位）；
        * ``on`` → :meth:`_plan_n2`：规划 prompt 的 JSON schema **同步要求三字段**
          （R-C2），步骤对象带 ``method`` / ``switch_log``。

        返回值仍是 ``Plan``（既有调用方 ``_react_with_planner`` / ``TaskSession``
        一行都不用改）；N2 产物 ``PlanResult`` 由 :meth:`plan_turn` 给出，
        或直接 ``plan.to_result()``。
        """
        if not plan_loop_enforcing():
            return await self._plan_legacy(user_message, context_summary, session_id)
        return await self._plan_n2(user_message, context_summary, session_id)

    @staticmethod
    def _result_of(plan: Plan) -> PlanResult:
        """``Plan`` → 契约产物（澄清问题算 ``asked_user`` 的上报，R-C5）。"""
        if plan.needs_clarification:
            question = str(plan.clarification_question or "").strip()
            return plan.to_result(
                asked_user=True,
                report=question or "我没弄明白你想要什么，先说一句我再往下做。",
            )
        return plan.to_result()

    async def plan_turn(
        self, user_message: str, context_summary: str = "",
        session_id: int | None = None,
    ) -> PlanResult:
        """外层 owner 的**契约产物**入口（§3.2 ① 的「产出 ``PlanResult``」）。

        只规划、不执行；三字段缺一的步按 R-C2 判无效并进 ``plan.invalid_steps``
        （那是「回外层重规划」的理由），此时 :meth:`PlanResult` 里不会出现无效步。
        """
        plan = await self.understand_and_plan(
            user_message, context_summary=context_summary, session_id=session_id,
        )
        return self._result_of(plan)

    async def plan_turn_with_plan(
        self, user_message: str, context_summary: str = "",
        session_id: int | None = None,
    ) -> tuple[PlanResult, Plan]:
        """B2：与 :meth:`plan_turn` 同一次规划，同时把**可变 Plan** 交给执行层复用。

        口径与 ``plan_turn`` 逐字一致（共用 :meth:`_result_of`）；调用方拿到
        ``plan`` 后交给 :meth:`run_plan_loop` 执行，避免二次规划。
        """
        plan = await self.understand_and_plan(
            user_message, context_summary=context_summary, session_id=session_id,
        )
        return self._result_of(plan), plan

    # ------------------------------------------------------------------
    # N2 第二期：路由装配（``context.extra["content_plan"]`` 的**唯一写者**）
    # ------------------------------------------------------------------
    def plan_payload(self, result: Any) -> dict[str, Any]:
        """``Plan`` / ``PlanResult`` / ``None`` → 只含 JSON 值的载荷（鸭子类型，不 import 契约）。

        ``context.extra`` 里放**可 JSON 序列化**的一份：记录通道（decision_trace）
        与用例都要能原样 dump，frozen dataclass 放进去会在审计那一步才炸。
        ``str()`` 只作用在**枚举/异常对象**上，模型产出的字符串原样保留
        （约定：字符串值不重渲染，免得 ``"0.60"`` 变成 ``"0.6"`` 那种假 diff）。
        """
        if result is None:
            return {}
        steps: list[dict[str, Any]] = []
        for item in tuple(getattr(result, "steps", ()) or ()):
            if isinstance(item, Mapping):
                steps.append({str(k): _jsonable(v) for k, v in item.items()})
                continue
            to_dict = getattr(item, "to_dict", None)
            if callable(to_dict):
                steps.append({str(k): _jsonable(v) for k, v in dict(to_dict()).items()})
                continue
            steps.append({"id": int(getattr(item, "id", 0)), "description": str(item)})
        return {
            "steps": steps,
            "replans": int(getattr(result, "replans", 0) or 0),
            "over_budget": bool(getattr(result, "over_budget", False)),
            "report": str(getattr(result, "report", "") or ""),
            "asked_user": bool(getattr(result, "asked_user", False)),
        }

    def plan_execution_payload(self, plan: Any, result: Any) -> dict[str, Any]:
        """B2：执行后的 Plan/PlanResult → JSON 载荷（表达层渲染 + 审计回放）。"""
        steps: list[dict[str, Any]] = []
        for task in tuple(getattr(plan, "subtasks", ()) or ()):
            tool = str(getattr(task, "tool", "") or "")
            if not tool:
                continue
            status = getattr(task, "status", "")
            steps.append({
                "id": int(getattr(task, "id", 0) or 0),
                "description": str(getattr(task, "description", "") or ""),
                "tool": tool,
                "status": str(getattr(status, "value", status) or ""),
                "attempts": int(getattr(task, "attempts", 0) or 0),
                "switches": len(tuple(getattr(task, "switch_log", ()) or ())),
                "result": str(getattr(task, "result", "") or "")[:300],
            })
        return {
            "steps": steps,
            "replans": int(getattr(result, "replans", 0) or 0),
            "over_budget": bool(getattr(result, "over_budget", False)),
            "asked_user": bool(getattr(result, "asked_user", False)),
            "report": str(getattr(result, "report", "") or ""),
        }

    async def execute_attached_plan(
        self,
        context: Any,
        *,
        tools: Any = None,
        user_message: str = "",
        session_id: int | None = None,
        ts_factory: Callable[[], str] | None = None,
    ) -> dict[str, Any] | None:
        """B2：把装配时留下的 ``context.plan_object`` 按步骤执行（只读工具白名单）。

        v1 边界（与 features.PLAN_LOOP 注释同一口径）：
        * 仅 ``on``；
        * 计划里的工具步**全部**在 ``PLAN_LOOP_READ_ONLY_TOOLS`` 才执行——写/外发工具
          留给 TaskSession 显式确认路径；
        * 纯表达计划（没有工具步）不在这里执行，出话仍由表达层负责；
        * ``extra["task_session"]`` 走既有 ``_react_with_planner``，不双执行；
        * 失败/无工具一律返回 ``None``（降级为无计划）；执行读数写
          ``context.plan_execution``，并置 ``plan_execution_started`` 防双执行。
        """
        if not plan_loop_enforcing() or context is None:
            return None
        plan = getattr(context, "plan_object", None)
        if plan is None or getattr(context, "plan_execution_started", False):
            return None
        extra = getattr(context, "extra", None) or {}
        if isinstance(extra, Mapping) and extra.get("task_session"):
            return None
        planned_tools = [
            str(getattr(task, "tool", "") or "")
            for task in tuple(getattr(plan, "subtasks", ()) or ())
            if getattr(task, "tool", None)
        ]
        if not planned_tools:
            return None
        if any(name not in PLAN_LOOP_READ_ONLY_TOOLS for name in planned_tools):
            log.info(
                "plan_execution_skipped_write_tool",
                tools=planned_tools[:5],
            )
            return None
        tool_table = tools if tools is not None else self.tools
        safe_tools = plan_exec_tools(tool_table)
        if not any(safe_tools.get(name) for name in planned_tools):
            return None
        context.plan_execution_started = True
        try:
            result = await self.run_plan_loop(
                str(user_message or ""),
                tools=safe_tools,
                session_id=session_id,
                plan=plan,
                ts_factory=ts_factory,
            )
            payload = self.plan_execution_payload(plan, result)
            context.plan_execution = payload
            log.info(
                "plan_execution_done",
                steps=len(payload.get("steps") or ()),
                over_budget=bool(payload.get("over_budget")),
                asked_user=bool(payload.get("asked_user")),
            )
            return payload
        except Exception as exc:  # noqa: BLE001 - 执行失败降级为无计划，不打断回合
            log.warning("plan_execution_failed", error=str(exc)[:160])
            return None

    async def attach_content_plan(
        self,
        context: Any,
        *,
        chat_id: str = "",
        current: str = "",
        context_summary: str = "",
        session_id: int | None = None,
        origin: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """路由装配点：**意图结论 → 规划 → ``extra["content_plan"]``**（方案 A 的冻结契约）。

        两件事，一件都不许省：

        1. **写**（键名固定、只加不改既有键）：``on`` 时，
           ``context.extra["content_plan"] = PlanResult.to_dict()`` ——
           字段级就是契约的 ``PlanResult{steps,replans,over_budget,report,asked_user}``，
           取值只由 :meth:`plan_payload` 一处渲染；
        2. **留痕**（R-C7 的分路通道）：**每一轮**（含走快路径、含开关关闭）落一条
           ``kind="plan_path"`` 记录进**既有**通道
           （``core.observability.decision_trace``，``decision_trace.jsonl``，**不新建表**），
           带 ``path`` / ``reason`` / ``fast_eligible`` / 计划摘要。没有它就没有按路径
           分组的分子分母，R-C7 的硬条款（"没有对比读数不许宣称快路径合格"）就无从谈起。

        **失败绝不打断这一轮**（验收④）：规划抛异常 → 留痕 + 返回 ``None``，``extra``
        一个键都不写，这一轮按"无计划"照常生成（降级方向与 R-C5「不许静默放弃」一致：
        这里不是"放弃任务"，是"这一轮没有外层计划"）。
        """
        record: dict[str, Any] = {
            "kind": PLAN_PATH_KIND,
            "path": "planned",
            "reason": PLAN_PATH_REASON,
            "fast_eligible": False,
            "origin": str(origin or ""),
            "chat_id": str(chat_id or ""),
            "plan_loop": plan_loop_mode(),
        }

        target = extra if extra is not None else _extra_of(context)
        planned = False
        failed = False
        payload: dict[str, Any] | None = None
        # 计划读数**无条件出现**（没有计划就是 0 / 空摘要）：读数脚本按路径分组时
        # 不必区分"键不存在"与"没有计划"——那是两个会漂移的读法，只留一个。
        record["plan_steps"] = 0
        record["plan_digest"] = ""
        if plan_loop_enforcing():
            planned = True
            try:
                result, plan = await self.plan_turn_with_plan(
                    str(current or ""),
                    context_summary=context_summary,
                    session_id=session_id,
                )
                if result is not None:
                    payload = self.plan_payload(result)
                    record["plan_digest"] = plan_digest(payload)
                    record["plan_steps"] = len(payload.get("steps") or ())
                    record["over_budget"] = bool(payload.get("over_budget"))
                    record["asked_user"] = bool(payload.get("asked_user"))
                    # 证据不变式的**读数**（判据在 owner：content_plan.evidence_verdict）：
                    # 分路读数要能回答"这一轮的计划有没有来源"，但不改这里的出口。
                    record.update(plan_evidence_record(payload))
            except Exception as exc:  # noqa: BLE001 - 规划失败不许打断这一轮（降级为无计划）
                failed = True
                record["error"] = f"{type(exc).__name__}: {exc}"[:200]
                log.warning(
                    "content_plan_failed",
                    chat_id=str(chat_id or ""),
                    origin=str(origin or ""),
                    error=str(exc)[:160],
                )
        if payload is not None and target is not None:
            # WP-08 8.2 步骤 3：content_plan 投影键已退役；typed carrier 是唯一结论通道。
            if context is not None:
                context.plan_result = result
                # B2：把可变 Plan 留给生成口按步骤执行（只读工具白名单在生成口 gate）。
                if plan_loop_enforcing():
                    context.plan_object = plan
            log.info(
                "content_plan_attached",
                chat_id=str(chat_id or ""),
                origin=str(origin or ""),
                steps=len(payload.get("steps") or ()),
                digest=record.get("plan_digest", ""),
            )
        _record_plan_path(record)
        if failed:
            return None
        return payload if planned else None

    async def _plan_legacy(
        self, user_message: str, context_summary: str = "",
        session_id: int | None = None,
    ) -> Plan:
        """``PLAN_LOOP=off`` 的**原样**口径（接线前的那段代码，一字未改）。"""
        tool_list = self._tool_descriptions()
        experience_hint = self._experience_hint(user_message)

        system = f"""你是一个任务规划器。你的工作是：
1. 理解用户的真实意图——他们到底想要什么
2. 如果意图不明确，生成澄清问题
3. 如果意图明确，将复杂请求拆分为可执行的子任务
4. 涉及相对日期（今天/明天/下周/几号）时，以当前时间为基准；需要精确日期可调用 get_current_time

当前时间: {temporal.now_line()}

可用工具:
{tool_list}
{experience_hint}

规划原则:
- 简单闲聊/情感表达 → 不需要工具，直接回复
- 需要查信息 → 先搜索再回答
- 需要多步操作 → 拆分为有序子任务，标注依赖关系
- 意图模糊 → 先澄清再执行，不要猜
- 每个子任务应该有明确的工具和参数，或者是"直接回复"类型
- 无依赖关系的子任务可以并行执行，不需要标注depends_on

返回JSON:
{{
  "reasoning": "你对用户意图的分析",
  "needs_clarification": true/false,
  "clarification_question": "如果不明确，问用户什么",
  "subtasks": [
    {{
      "id": 1,
      "description": "子任务描述",
      "tool": "工具名或null(null表示直接回复)",
      "tool_args": {{"参数名": "值"}},
      "depends_on": []
    }}
  ]
}}"""

        user = f"用户说: {user_message}"
        if context_summary:
            user += f"\n\n对话上下文: {context_summary}"

        resp = await self.llm.chat(
            system=system, user=user,
            agent_name="planner", temperature=0.2, max_tokens=600,
        )
        parsed = self.llm.parse_json(resp, {
            "reasoning": "", "needs_clarification": False,
            "clarification_question": "", "subtasks": [],
        })

        plan = Plan(
            goal=user_message,
            needs_clarification=parsed.get("needs_clarification", False),
            clarification_question=parsed.get("clarification_question", ""),
            reasoning=parsed.get("reasoning", ""),
            session_id=session_id,
        )

        for st in parsed.get("subtasks", []):
            if not isinstance(st, dict):
                continue
            tool_name = st.get("tool")
            plan.subtasks.append(SubTask(
                id=st.get("id", 0),
                description=st.get("description", ""),
                tool=tool_name,
                tool_args=st.get("tool_args", {}),
                depends_on=st.get("depends_on", []),
                retry_policy=RetryPolicy.for_tool(tool_name) if tool_name else RetryPolicy(max_retries=0),
            ))

        if not plan.subtasks and not plan.needs_clarification:
            plan.subtasks.append(SubTask(
                id=1, description="直接回复用户", tool=None,
                retry_policy=RetryPolicy(max_retries=0),
            ))

        log.info("plan_created", goal=plan.goal[:50],
                 tasks=len(plan.subtasks), clarify=plan.needs_clarification,
                 plan_id=plan.plan_id)
        return plan

    def _reflection_state(self, suggested: list[tuple[str, float]]):
        """返回 (当前 scope 的 lessons, 证据支持最好的方法)。

        只读；异常一律当作没有经验，不能把规划带崩。
        """
        if self._context is None:
            return (), ""
        try:
            from core.context import reflection
            from core.context.cognitive_commit import Scope
            from core.terminal.authority import context_coordinates

            coords = context_coordinates(self._context)
            scope = Scope(
                str(coords.get("platform") or ""),
                str(coords.get("account_id") or ""),
                str(coords.get("chat_id") or ""),
            )
            if not scope.complete:
                return (), ""
            lessons = reflection.current_lessons_for_scope(scope)
            if not lessons:
                return (), ""
            methods = [str(name) for name, _ in (suggested or [])]
            if not methods:
                methods = [lesson.method for lesson in lessons]
            best = reflection.rank_methods(lessons, methods) if methods else ""
            return lessons, best
        except Exception as exc:
            log.warning("reflection_hint_failed", err=repr(exc)[:120])
            return (), ""

    def _reflection_hint(self, suggested: list[tuple[str, float]]) -> str:
        lessons, best = self._reflection_state(suggested)
        if not lessons:
            return ""
        from core.context import reflection
        lines: list[str] = []
        if best:
            chosen = next((item for item in lessons if item.method == best), None)
            rendered = reflection.render_lesson(chosen)
            if rendered:
                lines.append("优先：" + rendered)
        for lesson in lessons:
            if lesson.method == best:
                continue
            rendered = reflection.render_lesson(lesson)
            if rendered:
                lines.append(rendered)
            if len(lines) >= 4:
                break
        if not lines:
            return ""
        return "\n[方法经验]\n" + "\n".join("- " + line for line in lines)

    def _experience_hint(self, user_message: str) -> str:
        """历史经验的提示串（规划与重规划的 prompt 共用；失败一律静默）。"""
        parts: list[str] = []
        suggested: list[tuple[str, float]] = []
        if self.evolve:
            try:
                pattern = self.evolve._extract_pattern(user_message)
                suggested = self.evolve.suggest_tools_for_pattern(pattern) or []
                _, best = self._reflection_state(suggested)
                if best and suggested:
                    suggested.sort(key=lambda item: 0 if item[0] == best else 1)
                if suggested:
                    tools_str = ", ".join(f"{t}(成功率{s:.0%})" for t, s in suggested)
                    parts.append(f"\n\n[历史经验: 类似「{pattern}」的请求，以下工具组合效果好: {tools_str}]")
            except Exception as _:
                warn("agents/planner.py:1353 Exception", err=repr(_))
        reflection_hint = self._reflection_hint(suggested)
        if reflection_hint:
            parts.append(reflection_hint)
        return "".join(parts)

    async def _plan_n2(
        self, user_message: str, context_summary: str = "",
        session_id: int | None = None,
    ) -> Plan:
        """N2 口径的规划（R-C1/R-C2）：prompt 的 JSON schema **同步要求三字段**。

        与 ``_plan_legacy`` 的差别只有三处，都要在报告里报账：

        1. schema 多了 ``needed_info`` / ``expected_output`` / ``success_criteria``
           （R-C2）与 ``source``（R-C4 ③级的阶梯位置）；
        2. 解析时逐条跑 R-C2 判据，**缺一即无效步**（不进步骤序列，理由进
           ``plan.invalid_steps`` → 这就是「回外层重规划」的账）；
        3. ``retry_policy`` 一律取**原语口径**（一次尝试、内部不升级），
           因为预算与换方法由外层唯一拥有（R-C3/R-C4 的留痕前提）。
        """
        tool_list = self._tool_descriptions()
        experience_hint = self._experience_hint(user_message)

        system = f"""你是一个任务规划器。你的工作是：
1. 理解用户的真实意图——他们到底想要什么
2. 如果意图不明确，生成澄清问题
3. 如果意图明确，将复杂请求拆分为可执行的子任务
4. 涉及相对日期（今天/明天/下周/几号）时，以当前时间为基准；需要精确日期可调用 get_current_time

当前时间: {temporal.now_line()}

可用工具:
{tool_list}
{experience_hint}

规划原则:
- 简单闲聊/情感表达 → 不需要工具，直接回复
- 需要查信息 → 先搜索再回答
- 需要多步操作 → 拆分为有序子任务，标注依赖关系
- 意图模糊 → 先澄清再执行，不要猜
- 每个子任务应该有明确的工具和参数，或者是"直接回复"类型
- 无依赖关系的子任务可以并行执行，不需要标注depends_on
- **每一步必须写全 needed_info / expected_output / success_criteria 三字段**；
  缺任何一条这一步就算无效步骤（R-C2），会被打回重规划
- method 的 source 按「memory → web → human」声明这一步一开始用的是哪一级来源
  （③级换来源的顺序就是它）
- **要断言具体的人、作品或事实（谁是谁、某个角色/作品是什么），计划里必须真有一步
  去取数**（memory 的记忆检索 / web 的 web_search / human 问人）：一步取数都没有的
  计划会被判成"没做完"并回来问你——宁可先查一次，也不许凭印象说

返回JSON:
{{
  "reasoning": "你对用户意图的分析",
  "needs_clarification": true/false,
  "clarification_question": "如果不明确，问用户什么",
  "subtasks": [
    {{
      "id": 1,
      "description": "子任务描述",
{_CONTRACT_SCHEMA_BLOCK}
      "tool": "工具名或null(null表示直接回复)",
      "tool_args": {{"参数名": "值"}},
      "depends_on": []
    }}
  ]
}}"""

        user = f"用户说: {user_message}"
        if context_summary:
            user += f"\n\n对话上下文: {context_summary}"

        resp = await self.llm.chat(
            system=system, user=user,
            agent_name="planner", temperature=0.2, max_tokens=600,
        )
        parsed = self.llm.parse_json(resp, {
            "reasoning": "", "needs_clarification": False,
            "clarification_question": "", "subtasks": [],
        })

        plan = Plan(
            goal=user_message,
            needs_clarification=parsed.get("needs_clarification", False),
            clarification_question=parsed.get("clarification_question", ""),
            reasoning=parsed.get("reasoning", ""),
            session_id=session_id,
        )
        plan.subtasks = self._subtasks_from_payload(
            parsed.get("subtasks", []), plan, contract=True,
        )

        if not plan.subtasks and not plan.needs_clarification:
            # 一步都没有（或全被判无效）→ **不许静默放弃**（R-C5 的同一条纪律）：
            # 老实说拆不出来，请对方补一句。
            plan.needs_clarification = True
            plan.clarification_question = (
                "我没能把这件事拆成能做的步骤，你再说细一点？"
            )
            log.warning("plan_steps_all_invalid", invalid=list(plan.invalid_steps))
            from core.context.degradation import note_degradation

            note_degradation(
                stage="planner",
                failure="invalid_steps",
                fallback="clarify_plan",
                detail=str(len(plan.invalid_steps)),
            )

        log.info("plan_created", goal=plan.goal[:50],
                 tasks=len(plan.subtasks), clarify=plan.needs_clarification,
                 plan_id=plan.plan_id, invalid=len(plan.invalid_steps))
        return plan

    def _subtasks_from_payload(
        self,
        payload: Sequence[Any],
        plan: Plan,
        *,
        contract: bool,
        start_id: int | None = None,
    ) -> list[SubTask]:
        """把规划 payload 收敛成 ``SubTask`` 列表（规划与重规划**共用**）。

        ``contract=True``（N2 口径）时逐条跑 R-C2 判据：三字段缺一 → **判无效**，
        理由写进 ``plan.invalid_steps``，无效步不进步骤序列
        （不然它要等到 ``to_step()`` 才炸，账就记不清了）。
        """
        out: list[SubTask] = []
        next_id = (
            int(start_id)
            if start_id is not None
            else max((int(t.id) for t in plan.subtasks), default=0) + 1
        )
        for st in payload or ():
            if not isinstance(st, dict):
                continue
            tool_name = st.get("tool")
            try:
                method = Method(
                    level=LEVEL_PARAMS,
                    tool=str(tool_name or ""),
                    args=_as_args_payload(st.get("tool_args")),
                    source=st.get("source") or MethodSource.MEMORY,
                )
            except ContentPlanError as exc:
                # 非法 source 之类的结构错不该弄丢整份计划：退回①级默认来源并留痕。
                log.warning("plan_method_fallback", step=st.get("id"), error=str(exc)[:120])
                method = Method(
                    level=LEVEL_PARAMS,
                    tool=str(tool_name or ""),
                    args=_as_args_payload(st.get("tool_args")),
                )
            step_id = _as_int_or(st.get("id"), next_id)
            task = SubTask(
                id=step_id,
                description=str(st.get("description", "")),
                needed_info=_as_needed_info_payload(st.get("needed_info")),
                expected_output=str(st.get("expected_output") or ""),
                success_criteria=str(st.get("success_criteria") or ""),
                method=method,
                depends_on=_as_ids_payload(st.get("depends_on")),
                retry_policy=(
                    RetryPolicy.for_primitive()
                    if contract
                    else (
                        RetryPolicy.for_tool(str(tool_name))
                        if tool_name
                        else RetryPolicy(max_retries=0)
                    )
                ),
            )
            if contract:
                missing = task.missing_contract_fields()
                if missing:
                    plan.invalid_steps.append(
                        f"第{task.id}步「{task.description}」缺 "
                        f"{' / '.join(missing)}（R-C2 缺一即无效）"
                    )
                    log.warning("plan_step_invalid", step_id=task.id, missing=list(missing))
                    continue
            out.append(task)
            next_id = max(next_id, int(task.id)) + 1
        return out

    async def replan_after_observation(
        self, plan: Plan, observation: str, user_feedback: str = "",
    ) -> Plan:
        """回外层重规划（§3.2 ②：``replan_after_observation`` 升格为**回外层**）。

        预算账只有一处：本函数一被调用就 ``plan.replans += 1``（R-C5 的
        「超预算回外层重规划」要能数得出来）。prompt 口径与
        :meth:`understand_and_plan` 同源：``off`` 用旧 schema（回滚位），
        ``on`` 的 schema 同样要求三字段（R-C2），否则补出来的步全是无效步。
        """
        plan.replans = int(getattr(plan, "replans", 0) or 0) + 1
        contract = plan_loop_enforcing()
        tool_list = self._tool_descriptions()
        done_summary = self._summarize_done(plan)
        schema_tail = f"\n{_CONTRACT_SCHEMA_BLOCK}" if contract else ""

        system = f"""你是一个任务规划器。之前的计划执行后有了新观察，需要决定是否调整。

已完成任务: {done_summary}
最新观察: {observation}
{"用户反馈: " + user_feedback if user_feedback else ""}

可用工具:
{tool_list}

决定:
- 如果结果已经足够好，不需要新任务
- 如果结果不完整或有错误，添加补充任务
- 如果用户反馈表明方向不对，调整后续任务
{"- 补充任务同样必须写全 needed_info / expected_output / success_criteria 三字段（R-C2）" if contract else ""}

返回JSON:
{{
  "assessment": "对当前结果的评估",
  "need_more": true/false,
  "new_subtasks": [
    {{
      "id": 下一个可用ID,
      "description": "补充任务描述",{schema_tail}
      "tool": "工具名或null",
      "tool_args": {{}},
      "depends_on": []
    }}
  ]
}}"""

        resp = await self.llm.chat(
            system=system, user=f"原始目标: {plan.goal}",
            agent_name="planner", temperature=0.2, max_tokens=400,
        )
        parsed = self.llm.parse_json(resp, {
            "assessment": "", "need_more": False, "new_subtasks": [],
        })

        if parsed.get("need_more") and parsed.get("new_subtasks"):
            plan.subtasks.extend(self._subtasks_from_payload(
                parsed["new_subtasks"], plan, contract=contract,
            ))

        log.info("replan", assessment=parsed.get("assessment", "")[:50],
                 new_tasks=len(parsed.get("new_subtasks", [])),
                 replans=int(plan.replans))
        return plan

    async def assess_quality(self, plan: Plan, user_message: str) -> dict:
        """结果质量评估（§3.2 ③：**收编为 ``over_budget`` 的判据之一**）。

        它不再是独立策略：「该不该再做一轮」这个判断只有 :func:`quality_needs_more`
        一处口径，外层循环（:meth:`run_plan_loop`）拿它当 ``over_budget`` 的输入之一。
        """
        done_summary = self._summarize_done(plan)

        system = """评估任务执行结果的质量。返回JSON:
{
  "completeness": 0-1,
  "accuracy": 0-1,
  "relevance": 0-1,
  "should_iterate": true/false,
  "suggestion": ""
}"""

        resp = await self.llm.chat(
            system=system,
            user=f"用户问: {user_message}\n\n执行结果:\n{done_summary}",
            agent_name="planner", temperature=0.1, max_tokens=200,
        )
        return self.llm.parse_json(resp, {
            "completeness": 0.7, "accuracy": 0.7, "relevance": 0.7,
            "should_iterate": False, "suggestion": "",
        })

    # ------------------------------------------------------------------
    # N2 外层循环（R-C1 / R-C3 / R-C4 / R-C5）
    # ------------------------------------------------------------------
    async def execute_task_with_retry(
        self, task: SubTask, tools, on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[SubTask, str]:
        """**①/② 级原语**（§3.2 ④：从「独立策略」降为原语，不再是独立策略）。

        N2 路径一律配 ``RetryPolicy.for_primitive()`` 调它——一次尝试、内部不重试
        也不降级（否则降级发生在原语内部，外层看不到、也留不了痕，违反 R-C3/R-C4）。
        ``RetryPolicy.for_tool`` 那条旧策略仍是 ``PLAN_LOOP=off`` 的回滚位，一字未改。
        """
        if task.tool is None:
            return task, ""

        tool = tools.get(task.tool)
        if not tool:
            fallback_name = task.retry_policy.fallback_tool
            if fallback_name:
                tool = tools.get(fallback_name)
                if tool:
                    log.info("retry_fallback", original=task.tool, fallback=fallback_name)
                    task.tool = fallback_name
                else:
                    task.result = f"工具 {task.tool} 不存在，降级工具 {fallback_name} 也不存在"
                    task.status = TaskStatus.FAILED
                    return task, task.result
            else:
                task.result = f"工具 {task.tool} 不存在"
                task.status = TaskStatus.FAILED
                return task, task.result

        for attempt in range(task.retry_policy.max_retries + 1):
            try:
                cached = self.cache.get(task.tool, task.tool_args)
                if cached:
                    log.info("tool_cache_hit", tool=task.tool, attempt=attempt)
                    if not cached.is_error:
                        task.result = cached.output
                        task.data = cached.data
                        task.status = TaskStatus.DONE
                        self._mark_short_reply_signal(task.tool)
                        return task, f"[{task.tool}] {cached.output[:300]}"
                    elif attempt < task.retry_policy.max_retries:
                        continue

                if on_progress:
                    await on_progress(f"{'🔄' if attempt > 0 else '🔍'} {task.description}...")

                self._apply_context(tool)
                from agents.tools.base import execute_tool_with_timeout

                result = await execute_tool_with_timeout(tool, **task.tool_args)

                if not result.is_error:
                    self.cache.put(task.tool, task.tool_args, result)
                    task.result = result.output
                    task.data = result.data
                    task.status = TaskStatus.DONE
                    self._mark_short_reply_signal(task.tool)
                    observation = f"[{task.tool}] {result.output[:300]}"
                    if on_progress:
                        await on_progress(f"✅ {task.description} 完成")
                    return task, observation

                if attempt < task.retry_policy.max_retries:
                    task.retry_count = attempt + 1
                    task.status = TaskStatus.RETRYING
                    backoff = task.retry_policy.backoff_base * (2 ** attempt)
                    log.info("tool_retry", tool=task.tool, attempt=attempt + 1,
                             backoff=backoff, error=result.error[:50])
                    await asyncio.sleep(backoff)
                    continue

                fallback_name = task.retry_policy.fallback_tool
                if fallback_name and attempt == task.retry_policy.max_retries:
                    fallback_tool = tools.get(fallback_name)
                    if fallback_tool:
                        log.info("retry_fallback_after_fail",
                                 original=task.tool, fallback=fallback_name)
                        if on_progress:
                            await on_progress(f"🔄 尝试降级方案: {fallback_name}...")
                        self._apply_context(fallback_tool)
                        fb_result = await execute_tool_with_timeout(
                            fallback_tool, **task.tool_args
                        )
                        if not fb_result.is_error:
                            task.tool = fallback_name
                            task.result = fb_result.output
                            task.data = fb_result.data
                            task.status = TaskStatus.DONE
                            self._mark_short_reply_signal(task.tool)
                            return task, f"[{fallback_name}] {fb_result.output[:300]}"

                task.result = f"[错误] {result.error}"
                task.status = TaskStatus.FAILED
                if on_progress:
                    await on_progress(f"❌ {task.description} 失败")
                return task, task.result

            except Exception as e:
                warn("agents/planner.py:1727 Exception", err=repr(e))
                if attempt < task.retry_policy.max_retries:
                    task.retry_count = attempt + 1
                    backoff = task.retry_policy.backoff_base * (2 ** attempt)
                    log.warning("tool_exception_retry", tool=task.tool,
                                attempt=attempt + 1, error=str(e)[:50])
                    await asyncio.sleep(backoff)
                    continue
                task.result = f"[异常] {e}"
                task.status = TaskStatus.FAILED
                if on_progress:
                    await on_progress(f"❌ {task.description} 异常")
                return task, task.result

        return task, task.result

    async def run_plan_loop(
        self,
        user_message: str,
        *,
        tools=None,
        context_summary: str = "",
        session_id: int | None = None,
        budget: StepBudget = DEFAULT_STEP_BUDGET,
        plan: Plan | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        ts_factory: Callable[[], str] | None = None,
    ) -> PlanResult:
        """N2 外层循环（R-C1/C3/C4/C5）：外层规划 → 内层执行+对比+按级换方法 → 用尽回外层。

        与既有 agentic loop 的关系是**包裹，不是替换**（§3.2）：单步执行仍走原语
        :meth:`execute_task_with_retry`（cache / 工具异常 / 上下文注入都在那里）。

        预算与升级**只有这一处 owner**，而且每一次升级都落一条 :class:`Switch`：

        * 每步每发起一次尝试 ``attempts += 1``；对不上 → :func:`compare_observation`
          判五类 ``criterion``（R-C3）；
        * 还能换（``can_switch_method``）→ 按级取下一档方法（:meth:`next_method`），
          记 ``Switch``（R-C4），继续下一步尝试；
        * 三级用尽 / 尝试到 3 → 记「回外层」理由 → 有预算就
          :meth:`replan_after_observation`（``replans += 1``），否则 ``over_budget=True``；
        * ``over_budget`` 或 ``asked_user`` 时 ``report`` **必填**（R-C5 不许静默放弃）；
        * ③ 收编：跑完一轮后 :meth:`assess_quality` 的结论经 :func:`quality_needs_more`
          也算 ``over_budget`` 的判据之一。

        时间戳由 ``ts_factory`` 注入（默认 :func:`_now_ts`）：同输入同输出，
        所以「两次同一输入 → 同一 ``PlanResult``」是可断言的口径。
        """
        tools = tools or self.tools
        ts = ts_factory or _now_ts
        if plan is None:
            plan = await self.understand_and_plan(
                user_message, context_summary=context_summary, session_id=session_id,
            )

        if plan.needs_clarification:
            # R-C2 打回 / 意图不清：直接问用户，不假装做过（R-C5 的上报责任）。
            question = str(plan.clarification_question or "").strip()
            return plan.to_result(
                asked_user=True,
                report=question or "我没弄明白你想要什么，先说一句我再往下做。",
            )

        asked_user = False
        over_budget = False
        report = ""
        quality: dict[str, Any] = {}
        reasons: list[str] = []

        while True:
            for task in list(plan.ready_tasks):
                if task.tool is None:
                    # 「直接回复」类步骤由出稿口负责，本层不产出回复文本。
                    continue
                while True:
                    task.attempts += 1
                    task.status = TaskStatus.RUNNING
                    if on_progress:
                        await on_progress(f"🔍 {task.description}...")
                    task, observation = await self.execute_task_with_retry(
                        task, tools, on_progress,
                    )
                    criterion = self.compare_step(task, observation)
                    if criterion is None:
                        task.status = TaskStatus.DONE
                        break
                    evidence = self.switch_evidence(task, criterion, observation)
                    if not can_switch_method(task, budget):
                        task.status = TaskStatus.FAILED
                        reasons.append(
                            f"第{task.id}步「{task.description}」尝试 {task.attempts} 次"
                            f"仍未对上（{criterion.value}）：{evidence}"
                        )
                        log.warning(
                            "plan_step_over_budget",
                            step_id=task.id,
                            attempts=task.attempts,
                            switches=task.switches_used,
                            criterion=criterion.value,
                        )
                        break
                    method, skip_note = self.next_method(task, tools)
                    if method is None:
                        task.status = TaskStatus.FAILED
                        reasons.append(
                            f"第{task.id}步「{task.description}」无级可换（{criterion.value}）：{evidence}"
                        )
                        break
                    if skip_note:
                        # R-C4：跳级可以，但理由必须写在同一条留痕里。
                        evidence = f"{evidence}；跳级理由：{skip_note}"
                    from_level = int(task.method.level)
                    task.switch_log.append(Switch(
                        step_id=int(task.id),
                        from_level=from_level,
                        to_level=int(method.level),
                        criterion=criterion,
                        evidence=evidence,
                        ts=str(ts()),
                    ))
                    log.info(
                        "plan_method_switch",
                        step_id=task.id,
                        from_level=from_level,
                        to_level=int(method.level),
                        criterion=criterion.value,
                        skip=bool(int(method.level) - from_level > 1),
                    )
                    task.method = method
                    if method.is_ask_human:
                        # ③级走到阶梯末端「问人」：不再假装能自己做完（R-C4/R-C5）。
                        # report 就是那句问句——R-C5 不允许「问了但没留痕」。
                        asked_user = True
                        report = budget_report(step=task.to_step(), asks_user=True)
                        break
                if asked_user:
                    break
            if asked_user:
                break

            quality = await self.assess_quality(plan, user_message)
            incomplete = quality_needs_more(quality)
            if not reasons and not incomplete:
                break
            note_quality = (
                f"质量评估：完整度 {quality.get('completeness')}"
                f"（{quality.get('suggestion', '')[:60]}）"
                if incomplete
                else ""
            )
            if int(plan.replans) >= int(budget.max_outer_replans):
                over_budget = True
                report = budget_report(
                    step=plan.subtasks[0] if plan.subtasks else None,
                    reason="；".join(reasons) or "结果不完整",
                    replans=int(plan.replans),
                    quality_note=note_quality,
                )
                log.warning(
                    "plan_over_budget",
                    replans=int(plan.replans),
                    reasons=reasons[:3],
                    completeness=quality.get("completeness"),
                )
                break
            # 回外层重规划（replans 在 replan_after_observation 里 +1）
            plan = await self.replan_after_observation(
                plan,
                "\n".join(reasons) if reasons else str(quality.get("suggestion") or "结果不完整"),
                user_feedback="",
            )
            reasons = []
            if not plan.ready_tasks:
                if not quality_needs_more(quality):
                    break
                over_budget = True
                report = budget_report(
                    reason="回外层重规划后没有可执行的步骤",
                    replans=int(plan.replans),
                    quality_note=note_quality,
                )
                break

        # A1-d 执行回执（计划层不变式）的**第二个出口**：本循环把计划判为"跑完了"
        # 之前，必须过同一个 owner（``core/tasks/slot_fill.py::settle_plan_execution``
        # / ``plan_completion``）——承诺型步骤（create_reminder / notify_user /
        # send_message）**拿到执行回执才算办完**。判据一个字节都不在这里写（R-C5 的
        # "假承诺率 = 0" 要求判定只有一处 owner）；这里只做一件事：调用 + 服从结论。
        #
        # * ``None``（``EXEC_RECEIPT=off`` / ``SLOT_FILL=off`` / 这一轮没有承诺型步骤 /
        #   ``shadow`` 只读数）→ **逐字节退回接线前**，下面那句 return 一个字不动；
        # * ``complete=False``（有承诺、却没回执）→ 这里**不走"报喜"出口**，只回
        #   ``report``（R-C5：超预算必须上报，不许静默放弃、不许假承诺）。
        #
        # 与 ``agents/conversational.py::_react_with_planner`` 那一格是**同一结论**：
        # 两个出口调同一个 owner、读同一个字段、给同一种出口。
        if not asked_user and not over_budget:
            receipt = self._plan_receipt(plan)
            if receipt is not None and not receipt.complete:
                log.info(
                    "plan_completion_denied",
                    exit="run_plan_loop",
                    plan_id=str(getattr(plan, "plan_id", "")),
                    missing=list(receipt.missing),
                    missing_tools=list(receipt.missing_tools),
                    over_budget=bool(receipt.over_budget),
                )
                return plan.to_result(
                    over_budget=bool(receipt.over_budget) or over_budget,
                    asked_user=True,
                    report=str(receipt.report or "").strip() or _RECEIPT_DENIED_REPORT,
                )

        return plan.to_result(
            over_budget=over_budget, report=report, asked_user=asked_user,
        )

    def _plan_receipt(self, plan: Plan):
        """把这一轮交给 A1-d 回执 owner（``settle_plan_execution``）——**只调用，不判定**。

        与 ``agents/conversational.py::_slot_fill_receipt`` 同一个 owner、同一个 handle：
        本方法只读 flag、转交计划与上下文，结论（``PlanCompletion``）由 owner 给。

        * ``off``（默认）→ 本方法**直接返回 ``None``**，连 owner 都不碰（与
          ``conversational.py::_slot_fill_receipt`` 同一格：off 位下"新模块"不存在），
          所以出口与接线前**逐字节一致**；
        * owner 抛错 → 按"回执层没接管"处理（``None``）：回执坏了不许把既有出口一起
          带走（异常不是判据）。

        ``repo`` 从这一轮的上下文里取（:meth:`set_context` 收下的那个 ``AgentContext``）：
        台账可用时 owner 以台账为权威读回执，取不到就退回它自己的进程内一份。
        """
        try:
            from core import features
            from core.tasks import slot_fill

            if slot_fill.exec_receipt_mode() == features.MODE_OFF:
                return None
            context = self._context
            extra = dict(getattr(context, "extra", None) or {})
            extra.setdefault("session_id", getattr(context, "session_id", None) or "")
            return slot_fill.settle_plan_execution(
                plan=plan, context=extra, repo=self._receipt_repo(context),
            )
        except Exception as exc:  # noqa: BLE001 - 回执不许挡住既有出口
            log.warning("plan_receipt_failed", error=str(exc)[:160])
            return None

    @staticmethod
    def _receipt_repo(context):
        """回执层读台账用的 repo（取不到 → ``None``，owner 自己退回进程内一份）。

        取法照既有工具的同一口径（``getattr(context, "account_context", None)``，
        见 ``agents/tools/reminder.py:188``）：``AccountContext.repo`` 是
        ``ScopedRepository``，它的 ``meta_engine`` 就是台账所在的 meta 库
        （``core/tasks/store.py::resolve_engine`` 只认这个入口）。计划器没有自己的
        repo（``Planner(llm, tools, ...)``），所以这一格只做**解析**、不做任何判定：
        拿不到就交 ``None``，owner 照它自己的口径退回进程内那一份。
        """
        if context is None:
            return None
        account_ctx = getattr(context, "account_context", None)
        candidate = getattr(account_ctx, "repo", None) if account_ctx is not None else None
        if candidate is not None:
            return candidate
        for name in ("repo", "_repo"):
            candidate = getattr(context, name, None)
            if candidate is not None:
                return candidate
        getter = getattr(context, "get", None)
        if callable(getter):
            try:
                return getter("repo")
            except Exception:  # noqa: BLE001 - 取不到就按 None（fail-safe）
                warn("agents/planner.py:2000 Exception", err=repr(__import__("sys").exc_info()[1]))
                return None
        return None

    def compare_step(
        self, task: SubTask, observation: str, *, evidence_score: float | None = None,
    ) -> SwitchCriterion | None:
        """内层对比（R-C3 五判据）：判定 owner 是契约里的 :func:`compare_observation`。

        这里的活只是**把信号翻译过去**（收编纪律：判据不许有第二个实现）：

        * ``output`` —— **工具返回本身**（``task.result = ToolResult.output``），不是外面
          拼的 ``[工具名] …`` 包装：``empty`` 判的是「返回空不空」，
          拿包装去量长度会让短名字的工具永远「不空」（旧实现就是这么漏的）；
        * ``is_error`` —— 原语把失败写成 ``task.status == FAILED`` 或 ``[错误]/[异常]`` 前缀；
        * ``expects_content`` —— 召回/检索/联网类工具（见 :data:`CONTENT_TOOLS`，
          旧 ``_verify_step`` 的第一条判据 + ③级 ``web_search`` 的补集）；
        * ``scope`` —— 声明了 ``chat_id`` 的检索工具：返回里找不到就是形状不符
          （旧 ``_verify_step`` 的第二条判据「搜索范围不匹配」）；
        * ``evidence_score`` —— 自相矛盾 / 置信度低两条要求的新信号源（R-C3 备注），
          本期只有调用方显式给分时才会判。
        """
        args = task.tool_args or {}
        chat_id = str(args.get("chat_id") or "")
        scope = (
            {"chat_id": chat_id}
            if (task.tool in SEARCH_TOOLS and chat_id)
            else None
        )
        return compare_observation(
            output=str(task.result or ""),
            extra_text=str(observation or ""),
            is_error=bool(
                task.status == TaskStatus.FAILED
                or observation_is_error(str(observation or ""))
            ),
            expects_content=task.tool in CONTENT_TOOLS,
            min_chars=RECALL_EMPTY_MIN_CHARS,
            scope=scope,
            evidence_score=evidence_score,
        )

    def switch_evidence(
        self, task: SubTask, criterion: SwitchCriterion, observation: str,
    ) -> str:
        """R-C3 的「触发留痕」正文：判据 + 工具 + 这一步要什么信息 + 返回预览。"""
        head = (
            f"{criterion_reason(criterion, output=observation)}"
            f"（工具 {task.tool or '无'}，这一步要的信息："
            f"{'、'.join(task.needed_info or []) or '未声明'}）"
        )
        return head

    def next_method(
        self, task: SubTask, tools,
    ) -> tuple[Method | None, str]:
        """按级给出下一档方法（R-C4）；``(None, "")`` = 三级已用尽。

        返回 ``(方法, 跳级理由)``：跳级理由非空时，调用方必须把它并进同一条
        ``Switch.evidence``（R-C4「跳级要说明」）。

        * ①→②：**换工具**——候选来自既有 ``RetryPolicy.for_tool`` 的 ``fallback_tool``
          表（原语的知识留下、判定搬到外层）；表里没有候选就**跳级**到③；
        * ②→③：**换来源**——按「记忆 → 联网 → 问人」阶梯取下一档；
          走到 ``human`` 就是 :attr:`Method.is_ask_human`（问用户，不再自己试）。
        """
        level = int(task.method.level)
        if level == LEVEL_PARAMS:
            fallback = RetryPolicy.fallback_tool_for(task.tool)
            if fallback and tools.get(fallback):
                return (
                    Method(
                        level=LEVEL_TOOL,
                        tool=str(fallback),
                        args=dict(task.method.args),
                        source=task.method.source,
                    ),
                    "",
                )
            return (
                self._source_method(task, tools),
                "①→②级没有候选降级工具（RetryPolicy 表里没有 fallback_tool），直接跳到③级换来源",
            )
        if level == LEVEL_TOOL:
            return (self._source_method(task, tools), "")
        return (None, "")

    def _source_method(self, task: SubTask, tools) -> Method:
        """③级：换来源（记忆 → 联网 → 问人）。联网档优先指名 ``web_search``。"""
        source = next_source(task.method.source)
        tool = ""
        if source == MethodSource.WEB and tools.get("web_search"):
            tool = "web_search"
        elif source != MethodSource.HUMAN:
            tool = str(task.method.tool or "")
        if source != MethodSource.HUMAN and not tool:
            # ③级要么指名工具、要么就是「问人」：连工具都没有就落到阶梯末端。
            source = MethodSource.HUMAN
        if source == MethodSource.HUMAN:
            return Method(
                level=LEVEL_SOURCE, tool="", args={}, source=MethodSource.HUMAN,
            )
        return Method(
            level=LEVEL_SOURCE, tool=tool, args=dict(task.method.args), source=source,
        )

    def _tool_descriptions(self) -> str:
        lines = []
        for name, tool in self.tools.all_tools().items():
            params = ", ".join(
                f"{k}({v.get('type', '?')})" for k, v in tool.parameters.items()
            )
            lines.append(f"- {name}({params}): {tool.description}")
        return "\n".join(lines)

    def _summarize_done(self, plan: Plan) -> str:
        parts = []
        for t in plan.subtasks:
            if t.is_done:
                parts.append(f"[完成] {t.description} → {t.result[:100]}")
            elif t.status == TaskStatus.FAILED:
                parts.append(f"[失败] {t.description} → {t.result[:100]}")
        return "\n".join(parts) if parts else "(暂无完成结果)"
