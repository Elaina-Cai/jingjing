"""回合内工具调用闭环：判说要用哪个工具 → 调它 → 结果进留痕与下一轮。

**它是"手"，不是"嘴"**：这个模块不判要不要开口、不写出口、不碰账本。它只做三件事：
① 从判决载荷里解析 ``tools``（最多两条，每条必须有 ``purpose``）；
② 调工具（只经 ``ToolRegistry``，于是 effects/白名单/每回合上限全在原处生效）；
③ 把结果排成一小块文字，交给**下一轮**提示词，并写进完整留痕的 ``tools`` 字段。

为什么不做成"回合内多轮工具循环"：判无状态、模型点唯一（R2），一次回合最多一次
模型调用之后就是投递。工具结果因此只能进**下一轮**，不进本轮。这是纪律，不是省事。

三档（``features.TOOL_LOOP``）：off 逐字不变 / shadow 只解析并记日志、一个工具都不调 /
on 才真调。默认 off。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from core import features
from safelog import warn

log = logging.getLogger(__name__)

#: 一回合最多两次（``工具包.md`` §11）；含失败。
TOOL_CALLS_MAX = 2
#: ``purpose`` 必填且 ≤20 字（同上 §11）：它是「该查没查」的分母。
PURPOSE_MAX_CHARS = 20
#: 单条结果进提示词的字数上限（同 §11 的「工具返回合计 ≤600 字」）。
RESULT_MAX_CHARS = 600
#: 整块结果的字数上限。
RESULTS_BLOCK_MAX_CHARS = 1200
BLOCK_HEADER = "[工具结果]"
BLOCK_FOOTER = "（查不到就说没查到，不许编）"

#: 进程内"下一轮"信箱：``chat_id -> 已经排好的块``。只留一回合，读到就取走。
_PENDING: dict[str, str] = {}

#: 最近一次落地判决的会话（拼接提示词那一侧手上没有 chat_id 时用它兜底）。
#: 为什么允许这个兜底：判决落地一定在**本轮模型调用之后**、下一轮拼接之前，
#: 中间没有别的会话能插进来；带上它换来的是「不用在提示词装配路径上穿一根线」。
_LAST_CHAT: str = ""


SCOPE_ENV = "YINYUE_TOOL_SCOPE_CHATS"


def tool_scope() -> tuple[str, ...]:
    """``YINYUE_TOOL_SCOPE_CHATS``：工具闭环生效的会话（逗号分隔；空 = 全部生效）。

    为什么要这一层：工具清单会进提示词、工具结果会进下一轮——**这是行为改动**。
    让它在少数会话里先跑，其余会话逐字走旧主干，出问题只需改一行 env。
    """
    import os

    raw = (os.environ.get(SCOPE_ENV) or "").strip()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def in_tool_scope(chat_id: str = "") -> bool:
    """该会话是否允许用工具闭环；名单为空 = 全部允许（向后兼容）。"""
    scope = tool_scope()
    if not scope:
        return True
    return str(chat_id or "") in scope


def loop_mode() -> str:
    """三档开关的**唯一读点**。"""
    return str(features.mode(features.TOOL_LOOP, features.MODE_OFF) or features.MODE_OFF)


@dataclass(frozen=True)
class ToolCall:
    name: str
    purpose: str
    args: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolOutcome:
    name: str
    purpose: str
    status: str
    content: str = ""
    source: str = ""
    truncated: bool = False


@dataclass(frozen=True)
class ToolReport:
    calls: tuple[ToolCall, ...] = ()
    outcomes: tuple[ToolOutcome, ...] = ()
    block: str = ""

    def as_detail(self) -> list[dict[str, Any]]:
        return [
            {"name": o.name, "status": o.status, "purpose": o.purpose}
            for o in self.outcomes
        ]


def _clip(text: str, limit: int) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


def parse_tool_calls(payload: Mapping[str, Any] | None) -> tuple[ToolCall, ...]:
    """从判决载荷里取 ``tools``；形状不对的整条丢掉，绝不猜。"""
    if not isinstance(payload, Mapping):
        return ()
    raw = payload.get("tools")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        return ()
    out: list[ToolCall] = []
    for item in raw:
        if len(out) >= TOOL_CALLS_MAX:
            break
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        purpose = str(item.get("purpose") or "").strip()
        if not name or not purpose or len(purpose) > PURPOSE_MAX_CHARS:
            continue
        args = item.get("args")
        if args is None:
            args = {}
        if not isinstance(args, Mapping):
            continue
        out.append(ToolCall(name=name, purpose=purpose, args=dict(args)))
    return tuple(out)


def _hint_for(status: str) -> str:
    """失败码 → 「下一步」。延迟 import：registry 依赖 tools 包本身。"""
    try:
        from core.tools.registry import hint_for

        return hint_for(status)
    except Exception:  # noqa: BLE001 - 提示词不该因它炸
        warn("core/tools/turnloop.py:140 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def render_results(
    outcomes: Sequence[ToolOutcome], *, max_chars: int = RESULTS_BLOCK_MAX_CHARS
) -> str:
    """排成一小块文字：``- 名字(用途): 状态 | 正文``。

    放不下就**整行丢弃**（与 ``core/tools/prompt.py`` 同一纪律）：宁可少一条，
    也不要半行——半行读起来像工具吐了半句话。
    """
    items = tuple(outcomes or ())
    if not items:
        return ""
    budget = max(0, int(max_chars)) - len(BLOCK_HEADER) - len(BLOCK_FOOTER) - 2
    lines: list[str] = []
    used = 0
    for o in items:
        body = _clip(" ".join(str(o.content or "").split()), RESULT_MAX_CHARS)
        line = f"- {o.name}({o.purpose}): {o.status}"
        hint = _hint_for(o.status)
        if hint:
            line += "（下一步：" + hint + "）"
        if body:
            line += f" | {body}"
        if used + len(line) + 1 > budget:
            continue
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return "\n".join((BLOCK_HEADER, *lines, BLOCK_FOOTER))


def _registry_or_none(registry: Any) -> Any:
    if registry is not None:
        return registry
    try:
        from core.jobs.executor import default_registry

        return default_registry()
    except Exception:  # noqa: BLE001 - 没有注册表就当"没有工具"
        warn("core/tools/turnloop.py:182 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def run_calls(
    calls: Sequence[ToolCall], *, registry: Any = None, turn_id: str = ""
) -> tuple[ToolOutcome, ...]:
    """逐个调；一条失败不影响下一条，**每次只算一次调用**（``observe`` 内部记账）。"""
    reg = _registry_or_none(registry)
    out: list[ToolOutcome] = []
    for call in tuple(calls or ())[:TOOL_CALLS_MAX]:
        if reg is None:
            out.append(ToolOutcome(call.name, call.purpose, "not_enabled"))
            continue
        spec = reg.get(call.name)
        if spec is None:
            out.append(ToolOutcome(call.name, call.purpose, "unknown_tool"))
            continue
        unknown = set(call.args or {}) - set(spec.args or {})
        if unknown:
            out.append(ToolOutcome(call.name, call.purpose, "bad_args"))
            continue
        params = {"tool": call.name, "turn_id": turn_id}
        params.update(dict(call.args or {}))
        try:
            res = reg.observe(target="", what=call.purpose, params=params)
        except Exception as exc:  # noqa: BLE001 - 工具炸了算这一步失败
            log.info("tool_loop_call_failed", tool=call.name, error=type(exc).__name__)
            out.append(ToolOutcome(call.name, call.purpose, "failed"))
            continue
        out.append(
            ToolOutcome(
                name=call.name,
                purpose=call.purpose,
                status=str(getattr(res, "status", "") or "failed"),
                content=str(getattr(res, "content", "") or ""),
                source=str(getattr(res, "source", "") or ""),
                truncated=bool(getattr(res, "truncated", False)),
            )
        )
    return tuple(out)


def _write_trace(turn_id: str, chat_id: str, report: ToolReport) -> None:
    if not turn_id or not report.outcomes:
        return
    try:
        from core.terminal.turn_trace import record_full_detail

        record_full_detail(
            turn_id=turn_id, chat_id=chat_id, detail={"tools": report.as_detail()}
        )
    except Exception:  # noqa: BLE001 - 旁注失败绝不阻断主链
        warn("core/tools/turnloop.py:234 Exception", err=repr(__import__("sys").exc_info()[1]))


def handle_verdict(
    payload: Mapping[str, Any] | None,
    *,
    turn_id: str = "",
    chat_id: str = "",
    registry: Any = None,
) -> ToolReport:
    """判决载荷落地时调一次：解析 → （on 才）调 → 排块 → 存下一轮信箱。"""
    if not in_tool_scope(chat_id):
        # 不在作用域内：逐字走旧主干（连解析都不做、日志也不写，免得污染衡量）
        return None
    mode = loop_mode()
    if mode == features.MODE_OFF:
        return ToolReport()
    calls = parse_tool_calls(payload)
    if not calls:
        return ToolReport()
    if mode == features.MODE_SHADOW:
        log.info("tool_loop_shadow", calls=len(calls), tools=[c.name for c in calls])
        return ToolReport(calls=calls)
    outcomes = run_calls(calls, registry=registry, turn_id=turn_id)
    block = render_results(outcomes)
    report = ToolReport(calls=calls, outcomes=outcomes, block=block)
    global _LAST_CHAT
    _LAST_CHAT = str(chat_id or "")
    if block:
        _PENDING[str(chat_id or "")] = block
    _write_trace(turn_id, chat_id, report)
    log.info(
        "tool_loop_done",
        calls=len(calls),
        ran=len(outcomes),
        statuses=[o.status for o in outcomes],
    )
    return report


def consume_block(chat_id: str = "") -> str:
    """取走这个会话"上一轮留下的工具结果"（取一次就没了）。

    ``chat_id`` 传空时用最近一次判决的会话兜底（见 :data:`_LAST_CHAT`）。
    """
    key = str(chat_id or "") or _LAST_CHAT
    return _PENDING.pop(key, "")


def reset() -> None:
    global _LAST_CHAT
    _PENDING.clear()
    _LAST_CHAT = ""

#: 任务回合优先留的工具：能动手的 + 两把最常用的查询。
#: 真机（2026-10-21）：20 个 schema 一起给，模型 `calls=0`——噪声里该用的那个被淹了。
#: 只在「看起来是任务」的回合收窄，平时全量不变。
NARROW_KEEP: tuple[str, ...] = (
    "send_plan",   # 把「替他做」开成一件事
    "gui_state",   # 看当前界面
    "gui_click",
    "gui_input",
    "gui_navigate",
    "web_search",  # 查资料
    "search_messages",  # 指回原文
)


def narrow_names(names: Iterable[str], *, keep: Sequence[str] = NARROW_KEEP) -> list[str]:
    """按「能不能动手」收窄工具名单；一个都不剩就原样返回（宁可不收窄）。"""
    ordered = [n for n in keep if n in set(names)]
    return ordered or list(names)


def narrow_schemas(
    schemas: Sequence[dict[str, Any]], *, keep: Sequence[str] = NARROW_KEEP
) -> list[dict[str, Any]]:
    """按 name 收窄 schema 列表（保持 keep 的顺序）。"""
    by_name = {str((sc or {}).get("name") or ""): sc for sc in schemas}
    picked = [by_name[n] for n in keep if n in by_name]
    return picked or list(schemas)
