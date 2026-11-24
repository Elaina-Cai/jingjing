# -*- coding: utf-8 -*-
"""结构化输出校验（审计 ``G5-15``）：把「模型给的一段文本」变成「按 schema 校验过的 dict」。

**为什么要单独一个模块**：``llm/client.py`` 历史上的读法是

    try:
        arguments = json.loads(raw_args)
    except (TypeError, json.JSONDecodeError):
        arguments = {"_raw": raw_args}

——解析失败**静默降级**成一个仍然合法的 dict，调用方拿它当正常参数用
（``agents/base.py:178``、``agents/conversational.py:519``），失败于是不可见；
这正面挡着 ``R-A2``「结构合法率 = 100%」（``docs/需求契约.md:30``）。本模块把
「解析 → 校验 → 重试一次 → 显式报错」这条链收成一处 owner，并**禁止 ``_raw`` 透传**：
原始文本只许进日志/审计（``outcome.text``），**不许当数据**（见 :data:`RAW_KEY`）。

三层 API，按需要挑：

* :func:`parse_json_payload` —— 只解析 + 校验，**返回结果对象、不抛错**（给"重试一次"
  的调用方用：先看失败原因，再决定怎么让模型重说）；
* :func:`parse_or_raise` —— 失败就抛 :class:`JsonPayloadError`（给"校验不过 = 未完成"
  的调用方用）；
* :func:`expect_json` —— 顶层必须是 JSON 对象（``dict``），否则抛错；给
  ``chat_tools`` 这类"参数必须是对象"的位置用。

错误一律带上**字段路径**：``candidates[0].intent`` 这种写法的报错能直接照着改，
这比"JSON 解析失败"有用得多（``tests/test_llm_schema.py`` 逐条钉住）。

本模块**不导入项目任何其它层**（与 N1 契约同一纪律：纯结构 + 纯函数，可单独回放）。
"""

from __future__ import annotations

import json
import math
import structlog
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence
from safelog import warn

log = structlog.get_logger()

#: 旧读法用来吞掉解析失败的那个键。它**不再是任何东西的入参**：出现即判不合法
#: （``_raw`` 透传是"静默失败 + 隐式契约"，见审计 ``G5-15``）。
RAW_KEY = "_raw"

#: ``types`` 参数里可用的类型名 → 接受的 Python 值。``object`` = 任意（含 None 与
#: 嵌套 dict/list）；``bool`` 单独列是因为 ``bool`` 是 ``int`` 的子类，
#: ``True`` 不该被当成合法的 ``int`` 参数。
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "str": lambda value: isinstance(value, str),
    "int": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "float": lambda value: (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    ),
    "bool": lambda value: isinstance(value, bool),
    "list": lambda value: isinstance(value, list),
    "dict": lambda value: isinstance(value, dict),
    "object": lambda value: True,
}


class JsonPayloadError(ValueError):
    """模型输出**不是**按 schema 校验通过的结构。

    继承 ``ValueError``：调用方历史上就用 ``except ValueError`` / ``except
    Exception`` 接解析失败，换个更清楚的名字不该让那些兜底路径失效
    （与 ``ReplyIntentError`` 同一取舍）。
    """

    def __init__(self, message: str, *, problems: Sequence["SchemaProblem"] = (),
                 text: str = "") -> None:
        super().__init__(message)
        self.problems: tuple[SchemaProblem, ...] = tuple(problems)
        # 原始文本**只**挂在异常上供日志/审计用（**不许**当数据往下传）。
        self.text: str = str(text or "")


@dataclass(frozen=True)
class SchemaProblem:
    """一条校验错误：``path`` 是能照着改的字段路径，``reason`` 是人话。"""

    path: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - 只为人读
        return f"{self.path}: {self.reason}" if self.path else self.reason


@dataclass(frozen=True)
class JsonParseOutcome:
    """一次解析的完整结果。

    * ``ok`` —— 解析 + 校验都过了，``payload`` 可信（**不含** ``_raw``）；
    * ``problems`` —— 没过的原因（``ok=False`` 时非空）；
    * ``attempts`` —— 解析尝试了几轮（``parse_payload_with_retry`` 用；"重试一次"
      在这里可读，不靠调用方自己数）；
    * ``text`` —— 模型给的原始文本；**只进日志/审计**，不是数据。
    """

    ok: bool = False
    payload: dict[str, Any] | None = None
    problems: tuple[SchemaProblem, ...] = ()
    attempts: int = 0
    text: str = ""

    def raise_for_status(self) -> dict[str, Any]:
        """过 → 返回 ``payload``；不过 → 抛 :class:`JsonPayloadError`。"""
        if not self.ok or self.payload is None:
            reason = "；".join(str(item) for item in self.problems) or "未知原因"
            raise JsonPayloadError(
                f"结构化输出校验失败（尝试 {self.attempts} 次）：{reason}",
                problems=self.problems,
                text=self.text,
            )
        return self.payload

    def to_audit(self) -> dict[str, Any]:
        """折成可落审计的字段（**不含** ``payload`` 之外的原文）。"""
        return {
            "ok": self.ok,
            "attempts": self.attempts,
            "problems": [str(item) for item in self.problems],
            "text_chars": len(self.text or ""),
        }


def _json_candidates(text: str) -> list[str]:
    """取出可能装着 JSON 的候选片段（围栏优先，其次整段、再退到首个 ``{…}``）。

    模型很少只输出 JSON：`````json {…} `````、``前缀 {…} 后缀`` 都出现过，
    所以这里按"最可能先"的顺序给候选，由调用方逐个 try。
    """
    raw = str(text or "")
    candidates: list[str] = []
    if "```json" in raw:
        for part in raw.split("```json")[1:]:
            candidates.append(part.split("```")[0])
    elif "```" in raw:
        for part in raw.split("```")[1:]:
            candidates.append(part)
    candidates.append(raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    return candidates


#: "没解析出任何东西"的哨兵：JSON 里 ``null`` 是合法值，不能拿 ``None`` 当失败标记。
_NO_JSON = object()


def _fenced_contents(raw: str) -> list[str]:
    """围栏里逐段内容（与 :func:`_json_candidates` 的"围栏优先"同口径）。"""
    if "```json" in raw:
        return [part.split("```")[0] for part in raw.split("```json")[1:]]
    if "```" in raw:
        return [part.split("```")[0] for part in raw.split("```")[1:]]
    return []


def _iter_json_fragments(raw: str) -> list[dict[str, Any]]:
    """按出现顺序收集文本里所有能独立解析的 JSON **对象**。

    背景（2026-10-13 实查）：N1 的模型经常把一次回答拆成多个对象/多个围栏块
    （意图段一个、``scene_read`` 一个）。``json.loads(整段)`` 对这种形状一定报
    ``Extra data``；旧读法只取**第一个能解析的候选**，后面的段就丢了——
    表现为 scene_read 整段落不了账、世界模型空转。

    实现按每个 ``{`` 起点 ``raw_decode``（坏片段跳过、解析到哪跳到哪），
    返回顶层 dict 片段，供 :func:`_merge_json_fragments` 合并。
    """
    decoder = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    pos = 0
    while True:
        start = raw.find("{", pos)
        if start < 0:
            return out
        try:
            value, end = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            warn("llm/schema.py:181 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
            pos = start + 1
            continue
        pos = max(int(end), start + 1)
        if isinstance(value, dict):
            out.append(value)


def _merge_json_fragments(fragments: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """多个顶层对象 → 一个 payload；**同键首次出现说了算**（保守，不覆盖已解出的段）。"""
    merged: dict[str, Any] = {}
    for fragment in fragments:
        for key, value in fragment.items():
            merged.setdefault(key, value)
    return merged


def _fragment_candidates(text: str) -> list[dict[str, Any]]:
    """多片段候选：围栏优先（有围栏就只看围栏里的对象），否则扫全段。"""
    raw = str(text or "")
    fragments: list[dict[str, Any]] = []
    for content in _fenced_contents(raw):
        fragments.extend(_iter_json_fragments(content))
    if not fragments:
        fragments = _iter_json_fragments(raw)
    return fragments


def _decode_json(text: str) -> tuple[Any, str | None]:
    """尝试把一段文本解析成 JSON；返回 ``(值, 错误)``（错误只用于报错文案）。

    顺序：**多片段先合并**（模型把回答拆成多个对象/多个围栏块）→ 原有单候选
    （围栏 → 整段 → 首个 ``{…}``）→ 单片段兜底（对象后面还有尾随文本）。
    """
    fragments = _fragment_candidates(text)
    if len(fragments) >= 2:
        return _merge_json_fragments(fragments), None

    last = "没有任何候选片段"
    tried = False
    for candidate in _json_candidates(text):
        cleaned = candidate.strip()
        if not cleaned:
            continue
        tried = True
        try:
            return json.loads(cleaned), None
        except json.JSONDecodeError as exc:
            warn("llm/schema.py:229 json.JSONDecodeError", err=repr(exc))
            last = str(exc)
        except (TypeError, ValueError) as exc:  # 非 str 输入等
            warn("llm/schema.py:231 (TypeError, ValueError)", err=repr(exc))
            last = str(exc)
    if len(fragments) == 1:
        # 单个对象 + 尾随文本/第二个坏片段：旧读法可能整段失败，这里保住已解出的对象。
        return fragments[0], None
    return _NO_JSON, (last if tried else "没有任何候选片段")


def validate_payload(
    payload: Any,
    *,
    required: Iterable[str] = (),
    types: dict[str, Any] | None = None,
    source: str = "payload",
) -> list[SchemaProblem]:
    """按 schema 校验一个**已解析**的值；返回全部问题（空 = 通过）。

    * ``required`` —— 必须存在的键（存在即可，值可以是 ``None``：那是"已知为空"，
      与"没给这个字段"是两件事）；
    * ``types`` —— ``{字段: 类型或类型元组}``，类型写 ``"str"`` / ``"int"`` /
      ``"float"`` / ``"bool"`` / ``"list"`` / ``"dict"`` / ``"object"``，
      也接受字面 Python 类型（``str`` / ``int`` / ``bool`` / ``list`` / ``dict``）；
    * ``source`` —— 报错里的路径前缀（``candidates[0]`` 这类）。

    顶层 ``_raw`` 键**一律判不合法**——这是"禁止 ``_raw`` 透传"的强制点：旧读法把
    解析失败的结果做成 ``{"_raw": …}`` 继续往下传，谁都能再写出来，所以这里把它
    变成显式错误（见模块 docstring 与 ``tests/test_llm_schema.py``）。
    """
    problems: list[SchemaProblem] = []
    if not isinstance(payload, dict):
        kind = type(payload).__name__
        problems.append(SchemaProblem(
            source, f"顶层必须是 JSON 对象（dict），收到 {kind}"
        ))
        return problems

    if RAW_KEY in payload:
        problems.append(SchemaProblem(
            f"{source}.{RAW_KEY}",
            "禁止 _raw 透传：解析失败必须显式报错，原始文本只许进日志/审计"
            "（审计 G5-15）",
        ))

    for name in required:
        if name not in payload:
            problems.append(SchemaProblem(
                f"{source}.{name}", "必填字段缺失（R-A2：结构合法率 = 100%）"
            ))

    for name, expected in (types or {}).items():
        if name not in payload:
            continue  # 缺失由 required 负责；这里只校验"给了的"
        allowed = _normalise_types(expected)
        if allowed is None:
            problems.append(SchemaProblem(
                f"{source}.{name}", f"schema 里写了未知类型 {expected!r}"
            ))
            continue
        value = payload[name]
        if not any(check(value) for check in allowed):
            problems.append(SchemaProblem(
                f"{source}.{name}",
                f"类型不符：期望 {_type_label(expected)}，收到 {type(value).__name__}"
                f"（{_brief(value)}）",
            ))
    return problems


def parse_json_payload(
    text: str,
    *,
    required: Iterable[str] = (),
    types: dict[str, Any] | None = None,
    source: str = "payload",
) -> JsonParseOutcome:
    """解析 + 校验，**返回结果对象**（不抛错）。

    给"失败就重试一次"的调用方（``chat_tools`` / N1 装配器）用：先拿到
    ``ok=False`` 与 ``problems``，再决定怎么让模型重说/走哪条降级。
    """
    raw = str(text or "")
    value, error = _decode_json(raw)
    if value is _NO_JSON:
        return JsonParseOutcome(
            ok=False, payload=None, attempts=1, text=raw,
            problems=(SchemaProblem(source, f"不是合法 JSON：{error}"),),
        )
    problems = tuple(validate_payload(
        value, required=required, types=types, source=source,
    ))
    if problems:
        return JsonParseOutcome(
            ok=False, payload=None, attempts=1, text=raw, problems=problems,
        )
    return JsonParseOutcome(
        ok=True, payload=dict(value), attempts=1, text=raw,
    )


def expect_json(
    text: str,
    *,
    required: Iterable[str] = (),
    types: dict[str, Any] | None = None,
    source: str = "payload",
) -> dict[str, Any]:
    """:func:`parse_json_payload` 的抛错版（顶层必须是对象）。

    用法：``expect_json(raw, required=("tool",), types={"tool": "str"})``。
    不合法 → :class:`JsonPayloadError`（**绝不**返回 ``{"_raw": …}``）。
    """
    outcome = parse_json_payload(
        text, required=required, types=types, source=source,
    )
    outcome.raise_for_status()
    assert outcome.payload is not None  # raise_for_status 已保证
    return outcome.payload


def parse_payload_with_retry(
    text: str,
    *,
    repair: Callable[[JsonParseOutcome], str] | None = None,
    required: Iterable[str] = (),
    types: dict[str, Any] | None = None,
    source: str = "payload",
    max_attempts: int = 2,
    agent: str = "",
) -> JsonParseOutcome:
    """按 schema 校验，失败**重试一次**，仍失败返回显式失败（``ok=False``）。

    重试 = 把失败原因（``problems``）交给 ``repair``，拿它给的**新文本**再校验一轮
    （``repair=None`` 时无法重试，只有一轮）。``max_attempts`` 默认 2 = "重试一次"。
    调用方拿到 ``ok=False`` 后**要么显式报错、要么走显式降级**，不许 ``_raw`` 兜底。

    每一轮都写一条 ``llm_schema_retry`` / ``llm_schema_invalid`` 日志——静默失败
    正是审计 ``G5-15`` 要根治的东西。
    """
    attempts = max(1, int(max_attempts or 1))
    outcome = parse_json_payload(
        text, required=required, types=types, source=source,
    )
    outcome = JsonParseOutcome(
        ok=outcome.ok, payload=outcome.payload, problems=outcome.problems,
        attempts=1, text=outcome.text,
    )
    while not outcome.ok and outcome.attempts < attempts and repair is not None:
        reason = "；".join(str(item) for item in outcome.problems)
        log.warning(
            "llm_schema_retry",
            agent=agent, source=source, attempt=outcome.attempts, problems=reason[:300],
        )
        repaired = str(repair(outcome) or "")
        again = parse_json_payload(
            repaired, required=required, types=types, source=source,
        )
        outcome = JsonParseOutcome(
            ok=again.ok, payload=again.payload, problems=again.problems,
            attempts=outcome.attempts + 1, text=again.text,
        )
    if not outcome.ok:
        log.warning(
            "llm_schema_invalid",
            agent=agent, source=source, attempts=outcome.attempts,
            problems="；".join(str(item) for item in outcome.problems)[:300],
        )
    return outcome


def parse_or_raise(
    text: str,
    *,
    required: Iterable[str] = (),
    types: dict[str, Any] | None = None,
    source: str = "payload",
) -> dict[str, Any]:
    """:func:`expect_json` 的同义词（"校验不过 = 未完成，显式抛错"读起来更直白）。"""
    return expect_json(text, required=required, types=types, source=source)


def json_dumps(value: Any) -> str:
    """紧凑 JSON（落审计用；``ensure_ascii=False`` 让中文可读）。"""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str 已兜住
        warn("llm/schema.py:415 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return str(value)


def _normalise_types(expected: Any) -> tuple[Callable[[Any], bool], ...] | None:
    """把 ``types`` 的一项折成若干检查函数；未知类型返回 ``None``。"""
    if isinstance(expected, tuple):
        items: tuple[Any, ...] = expected
    elif isinstance(expected, list):
        items = tuple(expected)
    else:
        items = (expected,)
    checks: list[Callable[[Any], bool]] = []
    for item in items:
        if isinstance(item, str):
            check = _TYPE_CHECKS.get(item.strip().lower())
            if check is None:
                return None
            checks.append(check)
            continue
        if item is str:
            checks.append(_TYPE_CHECKS["str"])
        elif item is bool:
            checks.append(_TYPE_CHECKS["bool"])
        elif item is int:
            checks.append(_TYPE_CHECKS["int"])
        elif item is float:
            checks.append(_TYPE_CHECKS["float"])
        elif item is list:
            checks.append(_TYPE_CHECKS["list"])
        elif item is dict:
            checks.append(_TYPE_CHECKS["dict"])
        elif item is object:
            checks.append(_TYPE_CHECKS["object"])
        else:
            return None
    return tuple(checks) or (_TYPE_CHECKS["object"],)


def _type_label(expected: Any) -> str:
    if isinstance(expected, (tuple, list)):
        return " / ".join(str(item) for item in expected)
    return str(expected)


def _brief(value: Any, limit: int = 60) -> str:
    text = " ".join(str(value).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def is_finite_number(value: Any) -> bool:
    """有限数（``NaN`` / ``inf`` 不算）——概率类字段的共用判据。

    **不接受数字字符串**：``float("0.5")`` 能过，但"概率写成字符串"本身就是没按
    schema 输出，替它转等于把一个结构错误咽下去（``G5-15`` 要治的正是这个）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


# 概率字段的常用 schema 片段（N1 与其它结构化输出共用，免得各写一份 [0,1] 检查）。
PROBABILITY = "float"


def number_in_range(value: Any, low: float = 0.0, high: float = 1.0) -> bool:
    """``value`` 是否是落在 ``[low, high]`` 的有限数（概率越界 = 不合法）。"""
    if not is_finite_number(value):
        return False
    return low <= float(value) <= high


__all__ = [
    "JsonParseOutcome",
    "JsonPayloadError",
    "PROBABILITY",
    "RAW_KEY",
    "SchemaProblem",
    "expect_json",
    "is_finite_number",
    "json_dumps",
    "number_in_range",
    "parse_json_payload",
    "parse_or_raise",
    "parse_payload_with_retry",
    "validate_payload",
]
