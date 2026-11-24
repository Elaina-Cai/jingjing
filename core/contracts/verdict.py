"""输出契约校验（三必填 + 三条件）。

依据 ``docs/decisions/判投记_契约.md`` §2：

必填：decision / deliver / basis
条件：reading（仅 respond·ask）/ open_loop（仅当有未结事）/ memory（0~3 条）

本模块是**纯函数**：不读环境变量、不写日志、不碰数据库。档位由调用方读取
（``core.features.mode(features.VERDICT_CONTRACT)``），这样契约校验能在隔离
测试里单独跑，不把"读开关"混进"判对错"。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

DECISIONS: tuple[str, ...] = ("respond", "ask", "silent", "abstain")
SPEAKING: frozenset[str] = frozenset({"respond", "ask"})
DELIVER_MAX = 2
MEMORY_MAX = 3

# 错误码是稳定字符串：报表按码聚合，改字面量等于改指标口径。
ERR_DECISION_MISSING = "decision_missing"
ERR_DECISION_INVALID = "decision_invalid"
ERR_DELIVER_MISSING = "deliver_missing"
ERR_DELIVER_NOT_LIST = "deliver_not_list"
ERR_DELIVER_TOO_MANY = "deliver_too_many"
ERR_DELIVER_ITEM_INVALID = "deliver_item_invalid"
ERR_DELIVER_SAY_MORE_THAN_ONE = "deliver_say_more_than_one"
ERR_DELIVER_REQUIRED = "deliver_required"
ERR_DELIVER_MUST_BE_EMPTY = "deliver_must_be_empty"
ERR_BASIS_MISSING = "basis_missing"
ERR_BASIS_INVALID = "basis_invalid"
ERR_BASIS_EMPTY_ON_SPEAK = "basis_empty_on_speak"
ERR_READING_MISSING = "reading_missing"
ERR_MEMORY_TOO_MANY = "memory_too_many"
ERR_MEMORY_ITEM_INVALID = "memory_item_invalid"


class VerdictContractError(RuntimeError):
    """``VERDICT_CONTRACT=on`` 且载荷不合契约时抛出（携带 :class:`VerdictResult`）。"""

    def __init__(self, result: "VerdictResult") -> None:
        self.result = result
        super().__init__("verdict_contract_violation: " + ",".join(result.errors))


@dataclass(frozen=True)
class VerdictResult:
    ok: bool
    errors: tuple[str, ...]
    decision: str
    deliver_count: int
    basis_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "decision": self.decision,
            "deliver_count": self.deliver_count,
            "basis_count": self.basis_count,
        }


def _as_sequence(value: Any) -> tuple[Any, ...] | None:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return None


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_verdict(payload: Mapping[str, Any] | None) -> VerdictResult:
    """校验一份判决载荷，返回稳定错误码元组（不抛异常）。"""
    errors: list[str] = []
    data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}

    raw_decision = data.get("decision")
    decision = raw_decision.strip().lower() if isinstance(raw_decision, str) else ""
    if not decision:
        errors.append(ERR_DECISION_MISSING)
    elif decision not in DECISIONS:
        errors.append(ERR_DECISION_INVALID)

    raw_deliver = data.get("deliver")
    deliver = _as_sequence(raw_deliver)
    if raw_deliver is None:
        errors.append(ERR_DELIVER_MISSING)
        deliver = ()
    elif deliver is None:
        errors.append(ERR_DELIVER_NOT_LIST)
        deliver = ()
    elif len(deliver) > DELIVER_MAX:
        errors.append(ERR_DELIVER_TOO_MANY)

    say_count = 0
    for item in deliver:
        if not isinstance(item, Mapping) or not _nonempty_str(item.get("kind")):
            errors.append(ERR_DELIVER_ITEM_INVALID)
            continue
        if str(item.get("kind")).strip().lower() == "say":
            say_count += 1
    if say_count > 1:
        errors.append(ERR_DELIVER_SAY_MORE_THAN_ONE)

    if decision in SPEAKING and not deliver:
        errors.append(ERR_DELIVER_REQUIRED)
    if decision in ("silent", "abstain") and deliver:
        errors.append(ERR_DELIVER_MUST_BE_EMPTY)

    raw_basis = data.get("basis")
    basis = _as_sequence(raw_basis)
    if raw_basis is None:
        errors.append(ERR_BASIS_MISSING)
        basis = ()
    elif basis is None:
        errors.append(ERR_BASIS_INVALID)
        basis = ()
    else:
        for item in basis:
            if not _nonempty_str(item):
                errors.append(ERR_BASIS_INVALID)
                break
    if decision in SPEAKING and not basis:
        errors.append(ERR_BASIS_EMPTY_ON_SPEAK)

    if decision in SPEAKING and not _nonempty_str(data.get("reading")):
        errors.append(ERR_READING_MISSING)

    raw_memory = data.get("memory")
    memory = _as_sequence(raw_memory)
    if memory is not None:
        if len(memory) > MEMORY_MAX:
            errors.append(ERR_MEMORY_TOO_MANY)
        for item in memory:
            if not isinstance(item, Mapping) or not all(
                _nonempty_str(item.get(k)) for k in ("kind", "key", "value")
            ):
                errors.append(ERR_MEMORY_ITEM_INVALID)
                break
    elif raw_memory is not None:
        errors.append(ERR_MEMORY_ITEM_INVALID)

    unique: list[str] = []
    for code in errors:
        if code not in unique:
            unique.append(code)

    return VerdictResult(
        ok=not unique,
        errors=tuple(unique),
        decision=decision,
        deliver_count=len(deliver),
        basis_count=len(basis),
    )


def payload_from_extra(
    extra: Mapping[str, Any] | None, *, decision: str | None = None
) -> dict[str, Any]:
    """把现有 ``extra`` 尽力映射成一份判决载荷（影子期 / 测试用）。

    生产链路到 E1 为止**还没有**"模型一次性输出四段"的 JSON，所以这里只做
    只读映射：拿到什么给什么，缺的键保持缺失，交给 :func:`validate_verdict`
    如实报缺。**绝不补默认值**——补了就等于替模型作答，还会把缺口藏起来。
    """
    data: dict[str, Any] = {}
    if isinstance(extra, Mapping):
        if decision is not None:
            data["decision"] = decision
        elif isinstance(extra.get("turn_decision"), str):
            data["decision"] = extra["turn_decision"]
        for key in ("deliver", "basis", "reading", "open_loop", "memory"):
            if key in extra:
                data[key] = extra[key]
    elif decision is not None:
        data["decision"] = decision
    return data
