# -*- coding: utf-8 -*-
"""N2 规划-执行两层循环契约：外层步骤序列 + 内层对比与换方法三级。

依据：

* ``docs/需求契约.md`` §2.3 —— R-C1..R-C7（外层先规划、三字段齐全、五判据、
  三级换方法、预算与上报、快路径 fail-safe、两路对比读数）；
* ``docs/decisions/架构评估.md`` §3.2 —— N2 **字段级**输出契约与「必须收编的现有近似实现」；
* ``docs/decisions/架构评估.md`` §7 拍板项 5 —— 预算口径**写死为「尝试 ≤ 3 次 = 最多换 2 次方法」**。
  这是**实现口径**，不是 ``RetryPolicy.max_retries`` 那套「同一方法再试」的语义：
  两者不是同一个数（3 次尝试 ≠ 3 次换方法），本模块只认前者。

本模块**只有结构、校验和纯函数**：不调 LLM、不读库、不读 prompt、不接路由
（与既有契约同一纪律，所以落地时 golden 必须零 diff）。

两个「唯一 owner」在这里落地：

* **R-C2 的唯一判据** = :func:`missing_contract_fields` —— ``needed_info`` /
  ``expected_output`` / ``success_criteria`` 三字段齐全，缺一即 Step 无效；
* **R-C3 的唯一判据** = :func:`compare_observation` —— 五类 ``criterion`` 的判定
  只此一处。内层对比的第一实现 ``agents/conversational.py::_verify_step`` 收编为
  它的适配器（旧的两条判据变成它的输入信号），不许再有第二处各判各的。

时间戳一律**由调用方注入**（:class:`Switch.ts` 是入参，不是这里读的钟）：
同输入同输出是 R-C5/R-C2 的验收口径，也是一切回放与幂等断言的前提。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence
from safelog import warn


class ContentPlanError(ValueError):
    """N2 结构不合法（R-C2「缺一即 Step 无效」/ R-C5「超预算必须上报」）。

    继承 ``ValueError``：调用方历史上就用 ``except ValueError`` 接解析失败，
    换个更清楚的名字不该让那些兜底路径失效（与 ``ReplyIntentError`` 同一口径）。
    """


class StepStatus(StrEnum):
    """``Step.status`` 的**封闭集合**（R-C2 字段级契约，五取值）。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class SwitchCriterion(StrEnum):
    """R-C3 的**五类判据**（任一命中 → 追加 :class:`Switch` 并换方法）。"""

    EMPTY = "empty"                      # 返回空 / 长度低于阈值
    ERROR = "error"                      # ToolResult.is_error
    SCHEMA_MISMATCH = "schema_mismatch"  # 返回字段与 expected_output 声明的形状不符
    CONTRADICTION = "contradiction"      # 与 needed_info 已有值冲突
    LOW_CONFIDENCE = "low_confidence"    # 证据分低于阈值


class MethodSource(StrEnum):
    """R-C4 ③级「换来源」的封闭集合：记忆 → 联网 → 问人。"""

    MEMORY = "memory"
    WEB = "web"
    HUMAN = "human"


#: R-C4 三级阶梯：①换参数/查询（同工具改 args）→ ②换工具 → ③换来源。
LEVELS: tuple[int, ...] = (1, 2, 3)
LEVEL_PARAMS = 1
LEVEL_TOOL = 2
LEVEL_SOURCE = 3

#: ③级的来源阶梯（顺序即「按级」本身）。
SOURCE_LADDER: tuple[MethodSource, ...] = (
    MethodSource.MEMORY,
    MethodSource.WEB,
    MethodSource.HUMAN,
)

#: R-C5 + §7 拍板项 5：**单步最多尝试 3 次，即最多换 2 次方法**。
#: 写成「尝试数」一个数、换方法数**推导**出来，免得两个常数各自漂移。
MAX_ATTEMPTS_PER_STEP = 3
MAX_METHOD_SWITCHES_PER_STEP = MAX_ATTEMPTS_PER_STEP - 1

#: ``empty`` 判据的默认阈值（与既有近似实现 ``_verify_step`` 的 ``len < 10`` 同值：
#: 收编时不许悄悄改口径）。
DEFAULT_EMPTY_MIN_CHARS = 10

#: ``low_confidence`` 的默认阈值。**是参数不是逻辑里的字面量**：真正的口径要由
#: 评测集校准（R-C6/R-C7 的对比读数），未校准前取保守值。
DEFAULT_LOW_CONFIDENCE = 0.55

#: R-C2 三字段的名字（顺序即报账顺序：缺一即无效，三条都要能单独报出来）。
CONTRACT_FIELDS: tuple[str, ...] = (
    "needed_info",
    "expected_output",
    "success_criteria",
)

#: 三字段各自的「缺了怎么改」说明（报错消息与 :func:`missing_contract_fields` 共用）。
_FIELD_HINTS: dict[str, str] = {
    "needed_info": "R-C2 要求每步声明「需要什么信息」（非空，至少一条）",
    "expected_output": "R-C2 要求每步声明「期望产出」",
    "success_criteria": "R-C2 要求每步声明「成功判据」",
}


# ---------------------------------------------------------------------------
# 收敛与校验工具（消息里一律带**字段名 + 规则编号**，照着就能改）
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    """把枚举成员或原始值渲染成消息里可读的文本。"""
    return value.value if isinstance(value, StrEnum) else str(value)


def _coerce(enum_cls: type[StrEnum], value: Any, *, field_name: str, rule: str) -> Any:
    """把原始值收敛到枚举成员；非法值给一条能直接照着改的错。"""
    try:
        return enum_cls(value)
    except (ValueError, TypeError) as exc:
        allowed = " / ".join(member.value for member in enum_cls)
        raise ContentPlanError(
            f"非法 {field_name}：{value!r}（{rule}；合法取值：{allowed}）"
        ) from exc


def _coerce_level(value: Any, *, field_name: str = "level") -> int:
    """``level`` 只能取 :data:`LEVELS` 里的三个值之一（R-C4 三级封闭集合）。"""
    try:
        level = int(value)
    except (TypeError, ValueError) as exc:
        raise ContentPlanError(
            f"非法 {field_name}：{value!r}（R-C4 换方法只有三级："
            f"{' / '.join(str(item) for item in LEVELS)}）"
        ) from exc
    if level not in LEVELS:
        raise ContentPlanError(
            f"非法 {field_name}：{value!r}（R-C4 换方法只有三级："
            f"{' / '.join(str(item) for item in LEVELS)}）"
        )
    return level


def _required_text(value: Any, *, field_name: str, rule: str, code: str) -> str:
    """必填非空文本；缺失/空白一律抛（消息带字段名 + 规则编号）。"""
    if value is None:
        raise ContentPlanError(f"{field_name} 缺失：{rule}（{code}）")
    text = str(value).strip()
    if not text:
        raise ContentPlanError(f"{field_name} 为空：{rule}（{code}）")
    return text


def _as_str_tuple(value: Iterable[Any], *, field_name: str) -> tuple[str, ...]:
    """把序列收敛成「非空字符串」元组；str 本身不算序列（那是少写了一个括号）。"""
    if value is None:
        raise ContentPlanError(f"{field_name} 不能为 None")
    if isinstance(value, (str, bytes)):
        raise ContentPlanError(
            f"{field_name} 必须是字符串序列（例如 (\"…\",)），收到裸字符串 {value!r}"
        )
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            raise ContentPlanError(f"{field_name} 里不能为空字符串：{value!r}")
        out.append(text)
    return tuple(out)


def _as_needed_info(value: Iterable[Any]) -> tuple[str, ...]:
    """``needed_info``：**非空**元组且无空串（三字段之一，R-C2）。"""
    if value is None:
        raise ContentPlanError(
            "Step.needed_info 缺失：R-C2 要求每步声明「需要什么信息」，缺一即 Step 无效"
        )
    if isinstance(value, (str, bytes)):
        raise ContentPlanError(
            f"Step.needed_info 必须是字符串序列（例如 (\"…\",)），收到裸字符串 {value!r}"
        )
    items = _as_str_tuple(value, field_name="Step.needed_info")
    if not items:
        raise ContentPlanError(
            "Step.needed_info 为空：R-C2 要求每步声明「需要什么信息」，缺一即 Step 无效"
        )
    return items


def _as_int_tuple(value: Iterable[Any], *, field_name: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise ContentPlanError(f"{field_name} 必须是整数序列，收到裸字符串 {value!r}")
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError) as exc:
            raise ContentPlanError(f"{field_name} 里出现非整数：{item!r}") from exc
    return tuple(out)


def _as_count(value: Any, *, field_name: str) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ContentPlanError(f"{field_name} 必须是整数，收到 {value!r}") from exc
    if count < 0:
        raise ContentPlanError(f"{field_name} 不能为负：{value!r}")
    return count


def _as_args(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContentPlanError(f"Method.args 必须是映射（dict），收到 {value!r}")
    return dict(value)


def _field_of(source: Any, name: str) -> Any:
    """鸭子类型取字段：Mapping 走键，其余走属性（旧 SubTask / dict 都能判）。"""
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


# ---------------------------------------------------------------------------
# R-C2 唯一判据
# ---------------------------------------------------------------------------
def missing_contract_fields(source: Any) -> tuple[str, ...]:
    """R-C2 的**唯一判据**：三字段里缺哪些（返回空元组 = 这一步契约合法）。

    入参是鸭子类型：:class:`Step` / 旧 ``SubTask`` / 规划 prompt 解出来的 dict
    都能判——旧实现一次性迁移时要靠它报账（「哪些步按新契约是无效的」），
    所以它不能只认 :class:`Step`。

    「缺」= 字段不存在 / 为 None / 是空白串；``needed_info`` 另加两条：必须是
    **非空**序列（空列表不算声明过），且不得含空串。
    """
    missing: list[str] = []
    for name in CONTRACT_FIELDS:
        value = _field_of(source, name)
        if value is None:
            missing.append(name)
            continue
        if isinstance(value, (str, bytes)):
            if not str(value).strip():
                missing.append(name)
            continue
        if name == "needed_info":
            items = [str(item).strip() for item in value]
            if not items or any(not item for item in items):
                missing.append(name)
            continue
        if not str(value).strip():
            missing.append(name)
    return tuple(missing)


def has_contract_fields(source: Any) -> bool:
    """三字段齐全？（R-C2 的布尔口径，与 :func:`missing_contract_fields` 同源。）"""
    return not missing_contract_fields(source)


# ---------------------------------------------------------------------------
# Method（R-C4）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Method:
    """这一步**用什么方法**做（R-C4）：级别 + 工具 + 参数 + 来源。

    ``level`` 是阶梯位置而不是次数：初次规划给 ①级（原工具 + 原参数/查询），
    对不上才按级换。所以「换了 2 次方法」= ``switch_log`` 有两条 = 尝试 3 次
    （§7 拍板项 5 的写死口径）。
    """

    level: int = LEVEL_PARAMS
    tool: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    source: MethodSource | str = MethodSource.MEMORY

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "level", _coerce_level(self.level, field_name="Method.level")
        )
        object.__setattr__(
            self,
            "source",
            _coerce(
                MethodSource,
                self.source,
                field_name="Method.source",
                rule="R-C4 ③级来源封闭集合（记忆→联网→问人）",
            ),
        )
        object.__setattr__(self, "tool", str(self.tool or "").strip())
        object.__setattr__(self, "args", _as_args(self.args))
        validate_method(self)

    @property
    def is_ask_human(self) -> bool:
        """③级走到阶梯末端的「问人」（没有工具，只有问句）。"""
        return bool(
            self.level == LEVEL_SOURCE and _text(self.source) == MethodSource.HUMAN
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": int(self.level),
            "tool": self.tool,
            "args": dict(self.args),
            "source": _text(self.source),
        }


def validate_method(method: Method) -> None:
    """校验一个 :class:`Method`（R-C4）：级别封闭、来源封闭、换工具必须指名工具。"""
    level = _coerce_level(method.level, field_name="Method.level")
    source = _coerce(
        MethodSource,
        method.source,
        field_name="Method.source",
        rule="R-C4 ③级来源封闭集合（记忆→联网→问人）",
    )
    tool = str(method.tool or "").strip()
    if level == LEVEL_TOOL and not tool:
        raise ContentPlanError(
            "Method.tool 为空：R-C4 ②级就是「换工具」，level=2 必须指名换成哪个工具"
        )
    if level == LEVEL_SOURCE and not tool and _text(source) != MethodSource.HUMAN:
        raise ContentPlanError(
            "Method.tool 为空：R-C4 ③级要么指名换来源后的工具，"
            "要么就是「问人」（source=human，没有工具）"
        )
    _as_args(method.args)


# ---------------------------------------------------------------------------
# Switch（R-C3 的触发留痕 / R-C4 的按级）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Switch:
    """一次「对不上 → 换方法」的留痕（R-C3：**触发必须留痕**）。

    ``evidence`` 是必填非空——没有证据的换方法等于把「对不上」这句话吞了，
    那正是 R-C3 要根治的东西。跳级（``to_level - from_level > 1``）也把理由
    写在同一条 ``evidence`` 里（R-C4「跳级要说明」）。
    """

    step_id: int
    from_level: int
    to_level: int
    criterion: SwitchCriterion | str
    evidence: str
    ts: str

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "step_id", int(self.step_id))
        except (TypeError, ValueError) as exc:
            raise ContentPlanError(f"Switch.step_id 必须是整数：{self.step_id!r}") from exc
        from_level = _coerce_level(self.from_level, field_name="Switch.from_level")
        to_level = _coerce_level(self.to_level, field_name="Switch.to_level")
        if to_level <= from_level:
            raise ContentPlanError(
                f"Switch 的 to_level({to_level}) 必须高于 from_level({from_level})："
                "R-C4 换方法只许**按级升级**（跳级可以，但要写进 evidence）"
            )
        object.__setattr__(self, "from_level", from_level)
        object.__setattr__(self, "to_level", to_level)
        object.__setattr__(
            self,
            "criterion",
            _coerce(
                SwitchCriterion,
                self.criterion,
                field_name="Switch.criterion",
                rule="R-C3 五判据封闭集合",
            ),
        )
        object.__setattr__(
            self,
            "evidence",
            _required_text(
                self.evidence,
                field_name="Switch.evidence",
                rule="R-C3「对不上」触发必须留痕，证据不能空",
                code="R-C3",
            ),
        )
        object.__setattr__(
            self,
            "ts",
            _required_text(
                self.ts,
                field_name="Switch.ts",
                rule="留痕必须带时间戳（由调用方注入，保证可回放）",
                code="R-C3",
            ),
        )

    @property
    def is_skip(self) -> bool:
        """跳级？（R-C4：跳级要在 ``evidence`` 里说明理由。）"""
        return int(self.to_level) - int(self.from_level) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": int(self.step_id),
            "from_level": int(self.from_level),
            "to_level": int(self.to_level),
            "criterion": _text(self.criterion),
            "evidence": self.evidence,
            "ts": self.ts,
        }


# ---------------------------------------------------------------------------
# Step（R-C2 字段级契约）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Step:
    """外层步骤序列里的一步（R-C1：可读、可判进退）。

    字段顺序与 ``docs/decisions/架构评估.md`` §3.2 的输出契约逐字一致；**构造即校验**
    （ :func:`validate_step` ），因为 R-C2 要求「缺一即无效」——一个缺字段的
    ``Step`` 不该存在，而不是「先生成再检查」。

    ``attempts`` = **已发起的尝试次数**（首次执行前 0，执行一次 1；三次用尽 =
    :data:`MAX_ATTEMPTS_PER_STEP`）。``switch_log`` 每换一次方法追加一条，
    所以 ``len(switch_log) <= attempts`` 是不变量。

    三字段在这里给了**空默认值**，是为了让「少写一个字段」落到
    :func:`validate_step` 的 R-C2 报错上（带字段名 + 规则编号），
    而不是先撞一个 Python 的 ``TypeError``——「构造即校验」是验收口径的一部分。
    给了默认不等于放行：缺一个就构造不出来（空值一律判缺）。
    """

    id: int
    description: str = ""
    needed_info: tuple[str, ...] = ()
    expected_output: str = ""
    success_criteria: str = ""
    method: Method = field(default_factory=lambda: Method(level=LEVEL_PARAMS))
    depends_on: tuple[int, ...] = ()
    status: StepStatus | str = StepStatus.PENDING
    attempts: int = 0
    switch_log: tuple[Switch, ...] = ()

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "id", int(self.id))
        except (TypeError, ValueError) as exc:
            raise ContentPlanError(f"Step.id 必须是整数：{self.id!r}") from exc
        object.__setattr__(self, "description", str(self.description or ""))
        object.__setattr__(self, "needed_info", _as_needed_info(self.needed_info))
        object.__setattr__(
            self,
            "expected_output",
            _required_text(
                self.expected_output,
                field_name="Step.expected_output",
                rule="R-C2 要求每步声明「期望产出」，缺一即 Step 无效",
                code="R-C2",
            ),
        )
        object.__setattr__(
            self,
            "success_criteria",
            _required_text(
                self.success_criteria,
                field_name="Step.success_criteria",
                rule="R-C2 要求每步声明「成功判据」，缺一即 Step 无效",
                code="R-C2",
            ),
        )
        if not isinstance(self.method, Method):
            raise ContentPlanError(
                f"Step.method 必须是 Method，收到 {type(self.method).__name__}："
                "R-C4 的三级、工具、参数、来源都挂在 Method 上"
            )
        object.__setattr__(
            self, "depends_on", _as_int_tuple(self.depends_on, field_name="Step.depends_on")
        )
        object.__setattr__(
            self,
            "status",
            _coerce(
                StepStatus,
                self.status,
                field_name="Step.status",
                rule="R-C2 字段级契约（五取值封闭集合）",
            ),
        )
        object.__setattr__(
            self, "attempts", _as_count(self.attempts, field_name="Step.attempts")
        )
        object.__setattr__(self, "switch_log", _as_switch_tuple(self.switch_log))
        validate_step(self)

    @property
    def switches_used(self) -> int:
        """已经换过几次方法（R-C5 预算的另一半口径）。"""
        return len(self.switch_log)

    def is_over_budget(self, budget: "StepBudget | None" = None) -> bool:
        return is_over_budget(self, budget)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "description": self.description,
            "needed_info": list(self.needed_info),
            "expected_output": self.expected_output,
            "success_criteria": self.success_criteria,
            "method": self.method.to_dict(),
            "depends_on": list(self.depends_on),
            "status": _text(self.status),
            "attempts": int(self.attempts),
            "switch_log": [item.to_dict() for item in self.switch_log],
        }


def _as_switch_tuple(value: Iterable[Any]) -> tuple[Switch, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ContentPlanError(f"switch_log 必须是 Switch 序列，收到 {value!r}")
    out: list[Switch] = []
    for item in value:
        if not isinstance(item, Switch):
            raise ContentPlanError(
                f"switch_log 里出现非 Switch：{item!r}（R-C3 只认 Switch 这一种留痕）"
            )
        out.append(item)
    return tuple(out)


def validate_step(step: Step) -> None:
    """校验一个 :class:`Step`（R-C2 的唯一 owner）。

    非法就抛 :class:`ContentPlanError`，消息里带 **字段名 + 规则编号**：

    * 三字段缺一（R-C2，:func:`missing_contract_fields` 同源判据）；
    * ``needed_info`` 为空 / 含空串（R-C2：声明了才有「所需信息」可比对）；
    * ``status`` 不在五取值里、``attempts`` 为负、``depends_on`` 非整数（字段级）；
    * ``depends_on`` 里出现自己的 ``id``（自依赖的步骤永远不可执行）；
    * ``switch_log`` 里的留痕 ``step_id`` 与本人不符，或条数多于 ``attempts``
      （R-C3/R-C5：留痕必须对得上账）。
    """
    missing = missing_contract_fields(step)
    if missing:
        name = missing[0]
        raise ContentPlanError(
            f"Step.{name} 缺失：{_FIELD_HINTS[name]}（R-C2）"
        )
    _coerce(
        StepStatus,
        step.status,
        field_name="Step.status",
        rule="R-C2 字段级契约（五取值封闭集合）",
    )
    _as_count(step.attempts, field_name="Step.attempts")
    if int(step.id) in tuple(step.depends_on):
        raise ContentPlanError(
            f"Step.depends_on 里出现自己的 id={step.id}：自依赖的步骤永远不可执行"
        )
    for item in step.switch_log:
        if int(item.step_id) != int(step.id):
            raise ContentPlanError(
                f"switch_log 里的 step_id={item.step_id} 与 Step.id={step.id} 不符："
                "R-C3 的留痕必须挂在同一步上"
            )
    if len(step.switch_log) > int(step.attempts):
        raise ContentPlanError(
            f"switch_log 有 {len(step.switch_log)} 条，但 attempts={step.attempts}："
            "换方法发生在一次尝试**之后**，条数不可能多于尝试次数（R-C5 预算口径）"
        )


# ---------------------------------------------------------------------------
# 预算（R-C5 + §7 拍板项 5）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StepBudget:
    """单步预算 + 外层重规划预算（§3.2 的 ``StepBudget``）。

    ``max_method_switches_per_step`` 是**推导**出来的（不是第二个常数）：
    §7 已定「尝试 ≤ 3 次 = 最多换 2 次方法」，写死在一个数上才不会漂移。
    """

    max_attempts_per_step: int = MAX_ATTEMPTS_PER_STEP
    max_outer_replans: int = 1
    deadline_ms: int = 0

    def __post_init__(self) -> None:
        attempts = _as_count(
            self.max_attempts_per_step, field_name="StepBudget.max_attempts_per_step"
        )
        if attempts < 1:
            raise ContentPlanError(
                f"StepBudget.max_attempts_per_step 必须 ≥ 1，收到 {self.max_attempts_per_step!r}"
            )
        object.__setattr__(self, "max_attempts_per_step", attempts)
        object.__setattr__(
            self, "max_outer_replans", _as_count(self.max_outer_replans, field_name="StepBudget.max_outer_replans")
        )
        object.__setattr__(
            self, "deadline_ms", _as_count(self.deadline_ms, field_name="StepBudget.deadline_ms")
        )

    @property
    def max_method_switches_per_step(self) -> int:
        """最多换几次方法 = 尝试上限 - 1（默认 3 - 1 = **2**，§7 拍板项 5）。"""
        return max(0, int(self.max_attempts_per_step) - 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts_per_step": int(self.max_attempts_per_step),
            "max_method_switches_per_step": int(self.max_method_switches_per_step),
            "max_outer_replans": int(self.max_outer_replans),
            "deadline_ms": int(self.deadline_ms),
        }


DEFAULT_STEP_BUDGET = StepBudget()


def is_over_budget(step: Any, budget: "StepBudget | None" = None) -> bool:
    """这一步的预算用完了吗（R-C5：到 3 次尝试 = 超预算 → 必须上报）。"""
    limit = budget or DEFAULT_STEP_BUDGET
    return int(_field_of(step, "attempts") or 0) >= int(limit.max_attempts_per_step)


def can_switch_method(step: Any, budget: "StepBudget | None" = None) -> bool:
    """还能再换一次方法吗（R-C5：尝试没到顶 且 换方法没到顶）。

    这一条同时挡住了「第 4 次尝试」：第三次尝试失败后 ``attempts == 3``，
    两个上界同时到顶 → ``False`` → 调用方只能转 ``over_budget`` / ``asked_user``。
    """
    limit = budget or DEFAULT_STEP_BUDGET
    used = len(tuple(_field_of(step, "switch_log") or ()))
    attempts = int(_field_of(step, "attempts") or 0)
    return used < int(limit.max_method_switches_per_step) and (
        attempts < int(limit.max_attempts_per_step)
    )


# ---------------------------------------------------------------------------
# 证据不变式（**声明型断言必须有取数步骤**）—— 与 A1-d 回执不变式同一形状
# ---------------------------------------------------------------------------
# 依据：主人 28 条标注 No.10/11（"她把游戏角色当真人、**没去搜索**"）。
#
# 判据是**结构**的，不是词表：计划里的每一步要么**取数**（这一步真的去够一次数据：
# 有工具，或者按阶梯走到「问人」），要么**出话**（纯由模型产出内容）。一个"全是出话
# 步骤、一步取数都没有"的计划 → **不得视为完成**，走既有的上报路径
# （``budget_report`` 的问句 + ``asked_user`` / ``over_budget``，与 A1-d 同一出口）。
#
# **不做出入口词表拦截**（主人红线）：这里不看任何话术形态、不枚举"什么算编造"，
# 只看计划自己声明的 ``Method.tool`` / ``Method.source``。取值一个字都不进代码。
#
# 与 A1-d（"没有执行回执不许当完成"）是**同一条思路**：那条守"答应的事有没有回执"，
# 这条守"要断言的事有没有来源"；两条都由同一个 owner
# （``core/tasks/slot_fill.py::plan_completion``）一次判出来，不是第二套判定。
#
# 已知边界（读数时要认）：计划层分不出"这一步出的话是事实断言"还是"只是闲聊"，
# 所以判据取"**一步取数都没有**"这个最保守的形状——那正是 No.10/11 的形态
# （她凭记忆把角色当真人，计划里一步检索都没有）。要出话的计划只要带上一次取数
# （检索 / 记忆 / 问人）就算有来源，不做"哪句话需要来源"的二次判定。


def is_fetch_step(step: Any) -> bool:
    """这一步**取数**了吗：有工具（真去够一次数据），或按阶梯走到「问人」。

    **问人也是取数**（``MethodSource.HUMAN``）：那是把来源交给用户，不是自己编。
    没有任何工具、也没有问人的那一步，是**纯出话**——它说的话没有来源。

    读法照 :func:`step_tool` 的口径：先 ``method.tool``，空则退回旧字段 ``tool``
    （``SubTask`` 的 ``tool`` 是 ``method.tool`` 的视图；**没有**这个属性时退回得到
    ``None``，不能把 ``None`` 渲染成 ``"None"`` 当成工具名）。
    """
    method = _field_of(step, "method")
    source = _field_of(method, "source") if method is not None else None
    if _text(source) == MethodSource.HUMAN:
        return True
    tool = _field_of(method, "tool") if method is not None else None
    if not tool:
        tool = _field_of(step, "tool")
    return bool(str(tool or "").strip())


def evidence_verdict(source: Any) -> dict[str, Any]:
    """计划层**证据不变式**：这些步骤里，要出话的那些有没有取数步骤托底？

    ``source`` 是鸭子类型的 ``PlanResult`` / ``Plan``（``steps`` 或 ``subtasks``）——
    与 :func:`missing_contract_fields` 同一套鸭子读法，所以契约对象与运行时载体
    （``SubTask``）都判得动，不需要第二份实现。

    返回 ``{"ok", "reason", "steps"}``：

    * 一步都没有 → ``ok=False``（"没有步骤"不是"办好了"）；
    * 有取数步骤（有工具 / 问到人）→ ``ok=True``（**有来源就能完成**）；
    * 一步取数都没有、却有步骤 → ``ok=False``，``steps`` 是那些**没来源的步骤 id**
      （报告要点名是哪几步）。
    """
    steps = _plan_steps_of(source)
    if not steps:
        return {"ok": False, "reason": "这个计划一步都没有", "steps": ()}
    if any(is_fetch_step(step) for step in steps):
        return {"ok": True, "reason": "", "steps": ()}
    return {
        "ok": False,
        "reason": (
            "要断言具体的人、作品或事实，得先有来源——这个计划一步都没去取数"
            "（记忆 / 联网 / 问人都算）"
        ),
        "steps": tuple(int(_field_of(step, "id") or 0) for step in steps),
    }


def _plan_steps_of(source: Any) -> tuple[Any, ...]:
    """取步骤序列：``PlanResult.steps`` 或 ``Plan.subtasks``（两种载体同一口径）。"""
    if source is None or isinstance(source, (str, bytes)):
        return ()
    for name in ("steps", "subtasks"):
        value = _field_of(source, name)
        if value is None or isinstance(value, (str, bytes)):
            continue
        try:
            return tuple(value)
        except TypeError:
            warn("core/contracts/content_plan.py:737 TypeError", err=repr(__import__("sys").exc_info()[1]))
            continue
    return ()


# ---------------------------------------------------------------------------
# 换方法三级（R-C4）+ 留痕（R-C3）
# ---------------------------------------------------------------------------
def escalate_level(level: int) -> int:
    """按级升一档（①→②→③）；已在顶就返回 :data:`LEVEL_SOURCE`。"""
    current = _coerce_level(level, field_name="level")
    return min(LEVEL_SOURCE, current + 1)


def next_source(source: MethodSource | str) -> MethodSource:
    """③级的来源阶梯：记忆 → 联网 → 问人（到顶仍是「问人」）。"""
    current = _coerce(
        MethodSource,
        source,
        field_name="source",
        rule="R-C4 ③级来源封闭集合（记忆→联网→问人）",
    )
    index = SOURCE_LADDER.index(current)
    return SOURCE_LADDER[min(index + 1, len(SOURCE_LADDER) - 1)]


def switch_step(
    step: Step,
    *,
    method: Method,
    criterion: SwitchCriterion | str,
    evidence: str,
    ts: str,
) -> Step:
    """换方法（R-C4）+ 留痕（R-C3）：返回**新** Step（frozen，不就地改）。

    ``from_level`` 取当前方法的级别、``to_level`` 取新方法的级别；调用方负责
    在**发起下一次尝试**时把 ``attempts`` 加一（本函数不动它）。
    """
    item = Switch(
        step_id=int(step.id),
        from_level=int(step.method.level),
        to_level=int(method.level),
        criterion=criterion,
        evidence=evidence,
        ts=ts,
    )
    return replace(
        step,
        method=method,
        status=StepStatus.RUNNING,
        switch_log=tuple(step.switch_log) + (item,),
    )


# ---------------------------------------------------------------------------
# R-C3 五判据的唯一 owner
# ---------------------------------------------------------------------------
def compare_observation(
    *,
    output: str = "",
    is_error: bool = False,
    expects_content: bool = False,
    min_chars: int = DEFAULT_EMPTY_MIN_CHARS,
    expected_keys: Sequence[str] = (),
    scope: Mapping[str, str] | None = None,
    extra_text: str = "",
    prior_values: Mapping[str, str] | None = None,
    observed_values: Mapping[str, str] | None = None,
    evidence_score: float | None = None,
    confidence_threshold: float = DEFAULT_LOW_CONFIDENCE,
) -> SwitchCriterion | None:
    """内层对比（**R-C3 五判据的唯一 owner**）：返回命中的 ``criterion``，或 ``None``（对得上）。

    五判据怎么判（信号由调用方给，判定只此一处）：

    * ``empty`` —— 长度低于 ``min_chars``（**不 strip**：与既有实现 ``len(obs) < 10``
      同口径，收编不许悄悄改判）；**只在** ``expects_content=True`` 时判，
      否则 ``get_current_time`` 这种短答复会被误判成空——这正是既有实现里
      ``recall_tools`` 那个集合的作用，收编后它变成这里的入参；
    * ``error`` —— ``ToolResult.is_error``；
    * ``schema_mismatch`` —— ``output + extra_text`` 里找不到 ``expected_keys`` 或
      ``scope`` 声明的值（既有实现的第二条判据「搜索范围不匹配」收编在这里：
      范围就是**声明的形状**；``extra_text`` 给「这一步已经记下的结果」留了第二个
      查找面，旧实现就是这么做的：``observation`` 与 ``task.result`` 一起看）；
    * ``contradiction`` —— ``observed_values`` 与 ``prior_values`` 对同一个键给出
      不同的值（``needed_info`` 已有值被本次返回推翻）；
    * ``low_confidence`` —— 证据分低于阈值；给了分但不是有限数 → **判据不清 → 按
      「对不上」处理**（R-C3 的保守方向）。

    判定顺序**固定**为 error → empty → schema_mismatch → contradiction →
    low_confidence，命中第一条即返回：顺序写死，留痕才可回放。
    """
    text = str(output or "")
    haystack = f"{text}\n{extra_text}" if extra_text else text
    if is_error:
        return SwitchCriterion.ERROR
    if expects_content and len(text) < int(min_chars):
        return SwitchCriterion.EMPTY
    for key in expected_keys:
        token = str(key or "").strip()
        if token and token not in haystack:
            return SwitchCriterion.SCHEMA_MISMATCH
    for key, value in dict(scope or {}).items():
        token = str(value or "").strip()
        if token and token not in haystack:
            return SwitchCriterion.SCHEMA_MISMATCH
    observed = dict(observed_values or {})
    for key, value in dict(prior_values or {}).items():
        if key in observed and str(observed[key]) != str(value):
            return SwitchCriterion.CONTRADICTION
    if evidence_score is not None:
        try:
            score = float(evidence_score)
        except (TypeError, ValueError):
            warn("core/contracts/content_plan.py:851 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            return SwitchCriterion.LOW_CONFIDENCE
        if not math.isfinite(score) or score < float(confidence_threshold):
            return SwitchCriterion.LOW_CONFIDENCE
    return None


def criterion_reason(criterion: SwitchCriterion | str, *, output: str = "") -> str:
    """把判据渲染成一句可读的证据（留痕与上报共用一处措辞）。"""
    item = _coerce(
        SwitchCriterion,
        criterion,
        field_name="criterion",
        rule="R-C3 五判据封闭集合",
    )
    preview = str(output or "").strip().replace("\n", " ")[:80]
    labels = {
        SwitchCriterion.EMPTY: "返回是空的",
        SwitchCriterion.ERROR: "工具报错",
        SwitchCriterion.SCHEMA_MISMATCH: "返回的形状/范围与这一步声明的不符",
        SwitchCriterion.CONTRADICTION: "返回与这一步已有的信息冲突",
        SwitchCriterion.LOW_CONFIDENCE: "证据分低于阈值",
    }
    tail = f"：{preview}" if preview else "（返回为空串）"
    return f"{labels[item]}{tail}"


# ---------------------------------------------------------------------------
# PlanResult（R-C5：超预算必须上报）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanResult:
    """外层循环的产物：步骤序列 + 预算账 + **上报**（R-C5）。

    R-C5 的硬条款是「不许静默放弃」：``over_budget=True`` 或 ``asked_user=True``
    时 ``report`` 必须非空——问用户时 ``report`` 就是那句问句，否则就是明说
    「这一步我没做到」。
    """

    steps: tuple[Step, ...] = ()
    replans: int = 0
    over_budget: bool = False
    report: str = ""
    asked_user: bool = False

    def __post_init__(self) -> None:
        raw = self.steps
        if raw is None or isinstance(raw, (str, bytes)):
            raise ContentPlanError(f"steps 必须是 Step 序列，收到 {raw!r}")
        steps: list[Step] = []
        for item in raw:
            if not isinstance(item, Step):
                raise ContentPlanError(
                    f"steps 里出现非 Step：{item!r}（R-C2 的步骤序列只认契约对象）"
                )
            steps.append(item)
        object.__setattr__(self, "steps", tuple(steps))
        object.__setattr__(
            self, "replans", _as_count(self.replans, field_name="PlanResult.replans")
        )
        ids = [int(item.id) for item in steps]
        if len(set(ids)) != len(ids):
            raise ContentPlanError(
                f"steps 的 id 有重复：{ids}（depends_on 靠 id 定位，重复即无法解析）"
            )
        validate_plan_result(self)

    @property
    def failed_steps(self) -> tuple[Step, ...]:
        return tuple(item for item in self.steps if item.status == StepStatus.FAILED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [item.to_dict() for item in self.steps],
            "replans": int(self.replans),
            "over_budget": bool(self.over_budget),
            "report": self.report,
            "asked_user": bool(self.asked_user),
        }


def validate_plan_result(result: PlanResult) -> None:
    """校验一个 :class:`PlanResult`：R-C5 的「超预算必须上报」是硬条款。"""
    _as_count(result.replans, field_name="PlanResult.replans")
    for item in result.steps:
        if not isinstance(item, Step):
            raise ContentPlanError(f"steps 里出现非 Step：{item!r}")
        validate_step(item)
    if result.over_budget and not str(result.report or "").strip():
        raise ContentPlanError(
            "over_budget=True 但 report 为空：R-C5 要求超预算**必须上报**"
            "（问用户或明说没做到），不许静默放弃"
        )
    if result.asked_user and not str(result.report or "").strip():
        raise ContentPlanError(
            "asked_user=True 但 report 为空：R-C5 要求上报留痕——report 就是那句问句"
        )


def budget_report(
    *,
    step: Step | None = None,
    reason: str = "",
    asks_user: bool = False,
    question: str = "",
    replans: int = 0,
    quality_note: str = "",
) -> str:
    """生成「上报」那句话（R-C5）：问用户就返回问句，否则明说哪一步没做到。

    纯函数、可断言，所以「不许静默放弃」这条能被用例钉住（同一处措辞，
    不许各调用点自己拼一句）。
    """
    if asks_user:
        text = str(question or "").strip()
        if text:
            return text
        wanted = "、".join(step.needed_info) if step is not None else ""
        return f"这一步我需要你确认一下：{wanted}" if wanted else "我需要你确认一下再往下做。"
    parts: list[str] = []
    if step is not None:
        head = f"第 {step.id} 步「{step.description}」我没做到"
        parts.append(head)
    if reason:
        parts.append(f"原因：{reason}")
    if step is not None and step.switch_log:
        trails = "；".join(
            f"第{item.from_level}级→第{item.to_level}级（{_text(item.criterion)}）"
            for item in step.switch_log
        )
        parts.append(f"换方法留痕：{trails}")
    if int(replans) > 0:
        parts.append(f"已回到外层重规划 {int(replans)} 次")
    if quality_note:
        parts.append(quality_note)
    parts.append("这事我没做完，先如实说一声。")
    return "；".join(parts)
