"""User-prompt blocks: named blocks, caps, assembly and trace.

Phase 1b unified the shared pieces; Phase 3 finishes the job: every part of the
user prompt is now a named :class:`ContextBlock`, joined by ``assemble`` with the
legacy ``"\\n"`` separator, and recorded in ``context_trace`` (``section="user"``)
with any cap decisions.  Byte-compatibility with the legacy builders is covered
by ``tests/test_context_user_prompt.py``.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from core.context.budget import AUX, HISTORY, MEMORY, PERSONA, BudgetDecision
from core.context.contracts import ContextBlock, ContextBundle
from safelog import warn

HISTORY_BUDGET = 1500
SUMMARY_BUDGET = 800
AFFECT_BUDGET = 400
GROUP_BUDGET = 1200
OBSERVATIONS_BUDGET = 1000


def budget_enabled() -> bool:
    try:
        from core import features

        return bool(features.enabled(features.CONTEXT_BUDGET, True))
    except Exception:
        warn("core/context/user_prompt.py:29 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def cap_head(text: str, budget: int, *, force: bool = False) -> str:
    if not force and not budget_enabled():
        return text
    value = str(text or "")
    return value if len(value) <= budget else value[:budget]


def cap_tail_lines(text: str, budget: int, *, force: bool = False) -> str:
    if not force and not budget_enabled():
        return text
    value = str(text or "")
    if len(value) <= budget:
        return value
    lines = value.splitlines()
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    return "\n".join(kept)


def cap_or_drop(text: str, budget: int, *, force: bool = False) -> str:
    if not force and not budget_enabled():
        return text
    value = str(text or "")
    return value if len(value) <= budget else ""


def text_block(
    name: str,
    source: str,
    text: str,
    *,
    priority: int = HISTORY,
    budget: int | None = None,
) -> ContextBlock:
    return ContextBlock(
        name=name, content=text, source=source, priority=priority, budget=budget
    )


def separator() -> ContextBlock:
    return ContextBlock(name="(separator)", content="", source="separator", priority=HISTORY)


def note_cap(
    name: str, source: str, priority: int, before: int, after: int
) -> BudgetDecision:
    return BudgetDecision(name, source, priority, before, after, "truncated")


def time_affect_prefix_blocks(
    *,
    message_time: Any = None,
    affect_block: str = "",
    now_value: Any = None,
) -> tuple[list[ContextBlock], list[BudgetDecision]]:
    from core import temporal

    blocks = [
        text_block("[时间感知]", "context.time", "[时间感知]", priority=PERSONA),
        text_block("当前时间", "context.time", temporal.now_line(now_value), priority=PERSONA),
        text_block(
            "消息时间",
            "context.time",
            temporal.message_time_line(message_time, now_value),
            priority=PERSONA,
        ),
        separator(),
    ]
    decisions: list[BudgetDecision] = []
    if affect_block:
        kept = cap_or_drop(affect_block, AFFECT_BUDGET)
        if kept:
            blocks.append(text_block("情绪", "affect.block", kept, priority=AUX))
            blocks.append(separator())
            if kept != affect_block:
                decisions.append(
                    note_cap("情绪", "affect.block", AUX, len(affect_block), len(kept))
                )
        else:
            decisions.append(
                BudgetDecision("情绪", "affect.block", AUX, len(affect_block), 0, "dropped")
            )
    return blocks, decisions


def time_affect_prefix(
    *,
    message_time: Any = None,
    affect_block: str = "",
    now_value: Any = None,
) -> list[str]:
    """Back-compat: the same parts as a plain list (tests/legacy callers)."""
    blocks, _ = time_affect_prefix_blocks(
        message_time=message_time, affect_block=affect_block, now_value=now_value
    )
    return [block.content for block in blocks]


def group_block_items(
    *,
    is_group: bool,
    group_recent_context: str = "",
    trigger_reason: str = "",
) -> tuple[list[ContextBlock], list[BudgetDecision]]:
    blocks: list[ContextBlock] = []
    decisions: list[BudgetDecision] = []
    if is_group and group_recent_context:
        capped = cap_tail_lines(group_recent_context, GROUP_BUDGET)
        blocks.append(
            text_block(
                "群聊最近消息",
                "session.group_recent",
                f"[群聊最近消息]\n{capped}\n",
                priority=HISTORY,
            )
        )
        if capped != group_recent_context:
            decisions.append(
                note_cap(
                    "群聊最近消息",
                    "session.group_recent",
                    HISTORY,
                    len(group_recent_context),
                    len(capped),
                )
            )
    if is_group and trigger_reason:
        blocks.append(
            text_block(
                "触发原因",
                "context.trigger",
                f"[触发原因: {trigger_reason}]\n",
                priority=AUX,
            )
        )
    return blocks, decisions


def group_blocks(
    *,
    is_group: bool,
    group_recent_context: str = "",
    trigger_reason: str = "",
) -> list[str]:
    """Back-compat: the same parts as a plain list."""
    blocks, _ = group_block_items(
        is_group=is_group,
        group_recent_context=group_recent_context,
        trigger_reason=trigger_reason,
    )
    return [block.content for block in blocks]


def current_message_line(
    text: str,
    *,
    is_group: bool = False,
    sender_name: str = "",
    chat_id: str = "",
) -> str:
    """「用户现在说」那一行的**唯一**渲染点（2026-10-13，谁说的修复）。

    群聊 + 说话人可知 + **``[读场]`` 注入同档**（``GROUP_SCENE=on`` 且会话在
    ``YINYUE_GROUP_SOCIAL_CHATS`` 作用域内）时，把说话人名写进标签：生成侧不再
    需要从窗口里反推"这句话是谁说的"（10:04 混人事故的土壤）。

    其余情况**逐字节**退回旧形 ``用户现在说: "..."``：off / **shadow** / 私聊 /
    作用域外一个字都不动——shadow 的合同是"只审计，不改 prompt"。
    """
    who = " ".join(str(sender_name or "").split())
    label = "用户现在说"
    if is_group and who:
        try:
            from core.context.act_view import act_view_enabled

            # 与 [读场] 注入同一个门：GROUP_SCENE=on + 群聊 + 作用域内。
            if act_view_enabled(chat_id, is_group):
                label = f"用户现在说（{who}）"
        except Exception:  # noqa: BLE001 - 读场不可用 → 保持旧形
            warn("core/context/user_prompt.py:217 Exception", err=repr(__import__("sys").exc_info()[1]))
    return f'{label}: "{text}"\n'


def _block_from_part(part: str) -> ContextBlock:
    """Name an already-rendered part so the trace stays readable."""
    text = str(part or "")
    if not text:
        return separator()
    first = text.strip().splitlines()[0] if text.strip() else ""
    if first.startswith("[") and "]" in first:
        return text_block(first.split("]")[0] + "]", "user.block", text)
    if first.startswith("用户现在说"):
        return text_block("用户现在说", "turn.user", text, priority=PERSONA)
    if first.startswith("用户说"):
        return text_block("用户说", "turn.user", text, priority=PERSONA)
    if first.startswith("最近对话") or first.startswith("最近的对话"):
        return text_block(first.split(":")[0], "session.history", text, priority=HISTORY)
    if first.startswith("基于以上所有信息") or first.startswith("回复要简洁"):
        return text_block(first[:12], "user.closing", text, priority=PERSONA)
    return text_block(first[:16] or "(part)", "user.aux", text)


def assemble_from_parts(
    parts: Sequence[str],
    *,
    context: Any = None,
    trace: bool = True,
    decisions: Iterable[BudgetDecision] = (),
) -> str:
    """Wrap an already-built parts list into blocks, trace it, and join with
    the legacy ``"\n"`` separator (empty parts are preserved as separators)."""
    parts = [str(part or "") for part in parts]
    if trace:
        try:
            from core.context.assembler import trace_bundle

            bundle = ContextBundle(blocks=tuple(_block_from_part(part) for part in parts))
            trace_bundle(
                bundle,
                section="user",
                context=context,
                decisions=list(decisions or ()),
            )
        except Exception:
            warn("core/context/user_prompt.py:262 Exception", err=repr(__import__("sys").exc_info()[1]))
    return "\n".join(parts)


def assemble(
    blocks: Sequence[ContextBlock],
    *,
    context: Any = None,
    trace: bool = True,
    decisions: Iterable[BudgetDecision] = (),
) -> str:
    """Join user-prompt blocks with the legacy ``"\\n"`` and record a trace row."""
    bundle = ContextBundle(blocks=tuple(blocks))
    if trace:
        try:
            from core.context.assembler import trace_bundle

            trace_bundle(
                bundle,
                section="user",
                context=context,
                decisions=list(decisions or ()),
            )
        except Exception:
            warn("core/context/user_prompt.py:286 Exception", err=repr(__import__("sys").exc_info()[1]))
    return bundle.render(separator="\n")


__all__ = [
    "HISTORY_BUDGET",
    "assemble_from_parts",
    "SUMMARY_BUDGET",
    "AFFECT_BUDGET",
    "GROUP_BUDGET",
    "OBSERVATIONS_BUDGET",
    "assemble",
    "budget_enabled",
    "cap_head",
    "cap_or_drop",
    "cap_tail_lines",
    "group_block_items",
    "group_blocks",
    "note_cap",
    "separator",
    "text_block",
    "time_affect_prefix",
    "time_affect_prefix_blocks",
]
