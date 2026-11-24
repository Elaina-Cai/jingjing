"""工具清单进 prompt：把本轮能用的 ≤6 个 schema 压成一段（`docs/decisions/工具包.md` §5）。

为什么单独一个模块：这段文字是**注入视图**，不是系统能力。注册表可以有几十个工具，
进 prompt 的永远 ≤6 个（`TOOL_IN_PROMPT_MAX`），每个一行、整段有字数上限——
预算失控的表现不是报错，而是「判看不见世界面」。所以宁可不给第 7 个工具，
也不许把这段话写长。
"""

from __future__ import annotations

from collections.abc import Sequence

from core.tools.registry import TOOL_IN_PROMPT_MAX, ToolSpec
from safelog import warn

#: 块的抬头：判靠它认出这是工具清单，而不是世界面的一部分。
TOOL_BLOCK_HEADER = "[本轮可用的工具]"

#: 抬头之后的收尾一句：一回合两件、失败说没查到（与 §10/§11 同一口径）。
TOOL_BLOCK_FOOTER = (
    "（要查/要动手：工具名 + 一句要什么；一回合最多两件；失败就说没查到，不许编。\n"
    "主人让你替他做具体的事（发消息、查资料、看/点界面、记东西）→ 必须真的调工具；"
    "只用嘴答应算失职。）"
)

#: 单个工具一行最多多少字（≈ 60 token，含参数名）。
TOOL_LINE_MAX_CHARS = 120

#: 整段最多多少字（≈ 600 token）：超过就**整行丢弃**，绝不截半行。
TOOL_BLOCK_MAX_CHARS = 1200

#: 参数最多几个（§5「>4 个就拆」）。
TOOL_MAX_ARGS = 4


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def render_spec_line(spec: ToolSpec) -> str:
    """一个工具一行：``- name(arg1, arg2) → 返回什么``（参数超过 4 个就省略）。"""
    args = [str(a) for a in spec.args]
    if len(args) > TOOL_MAX_ARGS:
        shown = ", ".join(args[:TOOL_MAX_ARGS]) + ", …"
    else:
        shown = ", ".join(args)
    returns = _clip(spec.returns, 60)
    line = f"- {spec.name}({shown}) → {returns}" if shown else f"- {spec.name}() → {returns}"
    purpose = _clip(str(getattr(spec, "purpose", "") or "").strip(), 40)
    if purpose:
        line += " ｜" + purpose
    return _clip(line, TOOL_LINE_MAX_CHARS)


#: 进 prompt 的排序：**能动手的排在前面**。
#:
#: 真机教训（2026-10-21）：工具明明在注册表里、也在模型侧注册表里，但这段清单
#: 只取前 6 个（按注册顺序），`send_plan` 排在第 7 位之后被截掉——模型读到的是
#: 「只有查东西、记笔记」，于是照实回答「我没有往群里发消息的工具」。
#: 能力给到了、清单没给到，等于没给。
_EFFECT_RANK = {
    "external_send": 0,
    "ui_control": 1,
    "write_local": 2,
    "read": 3,
}


def _rank(spec: ToolSpec) -> tuple[int, str]:
    effects = getattr(spec, "effects", ()) or ()
    best = min((_EFFECT_RANK.get(str(getattr(e, "value", e)), 9) for e in effects), default=9)
    return (best, str(getattr(spec, "name", "")))


def render_tool_block(specs: Sequence[ToolSpec]) -> str:
    """把 schema 压成一段；空输入返回空串（不注入空块）。"""
    # 不再按个数截断：函数调用 schema 本来就是全量给模型的，这份人读清单只是索引，
    # 按个数截断会让「能动手的工具」在清单里消失（真机：send_plan 被截掉，她答「我没有
    # 发消息的工具」）。真正的闸只剩**字数上限**，超了从末尾（只读的）整行丢。
    specs = sorted(specs, key=_rank)
    if not specs:
        return ""
    lines = [TOOL_BLOCK_HEADER]
    budget = TOOL_BLOCK_MAX_CHARS - len(TOOL_BLOCK_FOOTER) - 1
    used = len(TOOL_BLOCK_HEADER) + 1
    kept = 0
    for spec in specs:
        line = render_spec_line(spec)
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
        kept += 1
    if kept == 0:
        # 连一行都放不下：宁可不注入，也不给一段只有抬头的空清单。
        return ""
    lines.append(TOOL_BLOCK_FOOTER)
    return "\n".join(lines)


def _default_registry():
    """工具包注册表（启动时 `core/tools/builtin.py::bootstrap` 装好的那一张）。

    延迟导入：`core/tools/` 不认识 `core/jobs/` 的其它东西，只借它存的那一个句柄。
    """
    from core.jobs.executor import default_registry

    return default_registry()


def build_tool_block(registry=None, *, limit: int | None = None) -> str:
    """取本轮该注入的 schema 并渲染；没有注册表/没有启用的工具就返回空串。"""
    reg = registry if registry is not None else _default_registry()
    if reg is None:
        return ""
    try:
        specs = reg.select_for_prompt(
            limit=TOOL_IN_PROMPT_MAX if limit is None else int(limit)
        )
    except Exception:  # noqa: BLE001 - 工具清单取不到，只是判少看见几行
        warn("core/tools/prompt.py:122 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    return render_tool_block(specs)
