"""Phase 2 budget policy: priority + per-block budgets + drop/trim rules.

Priority (lower runs first, and is protected last):

* ``PERSONA``  (0)  -- never truncated: persona/traits/safety/basic voice.
* ``RELATION`` (10) -- relation facts, vision rule, correction rule: 800 chars.
* ``MEMORY``   (20) -- recall block, adaptive: up to 1500 chars (head-cut, header kept).
* ``HISTORY``  (30) -- history / group context: up to 1200 chars, oldest lines trimmed.
* ``AUX``      (40) -- proactive/trigger/affect helpers: 400 chars, dropped entirely.

Measured baselines (2026-09-22, 20 golden scenarios): the biggest real block is
~650 chars and a whole prompt is ~3.3k chars, so budgets only bite once the data
grows (long group context, long session history, many recall hits).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

import structlog

from core.context.contracts import ContextBlock, ContextBundle

log = structlog.get_logger()

PERSONA = 0
RELATION = 10
MEMORY = 20
HISTORY = 30
AUX = 40

DEFAULT_BUDGETS: dict[int, int | None] = {
    PERSONA: None,
    RELATION: 800,
    MEMORY: 1500,
    HISTORY: 1200,
    AUX: 400,
}

# ---------------------------------------------------------------- cut contract
# 2026-09-24 契约化：把"优先级 → 怎么处理"从散落的 if（>=AUX 丢 / ==HISTORY 保尾
# / 其余截头）收成**一张显式表**，并给每条截断/丢弃留证据（omitted_chars +
# context_block_truncated/dropped 日志）。此前两次事故（时间块被 AUX 预算整块
# 裁掉、名单块被静默砍尾）都是"处理方式藏在数字比较里"的后果。
CUT_NONE = "none"  # 不截断（budget=None 的块）
CUT_HEAD = "head"  # 保头：块名行 + 尽量多正文
CUT_TAIL = "tail"  # 保尾：块名行 + 最新若干行（历史类内容最旧在前）
CUT_DROP = "drop"  # 整块丢弃（辅助块，超预算即丢）

CUT_POLICY: dict[int, str] = {
    PERSONA: CUT_NONE,
    RELATION: CUT_HEAD,
    MEMORY: CUT_HEAD,
    HISTORY: CUT_TAIL,
    AUX: CUT_DROP,
}


def cut_strategy(priority: int) -> str:
    """优先级对应的处理方式。

    契约外的优先级（未登记）保守按 CUT_HEAD 处理：宁可截头也不静默丢整块——
    真丢了一条规则/一段记忆是事故，截断至少内容里有头、trace 里有账。
    """
    return CUT_POLICY.get(int(priority), CUT_HEAD)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: CJK ≈1 token/char, others ≈1 token/4 chars."""
    value = str(text or "")
    cjk = sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff")
    other = len(value) - cjk
    return int(cjk + other / 4) + 1


@dataclass(frozen=True)
class BudgetDecision:
    name: str
    source: str
    priority: int
    chars_before: int
    chars_after: int
    action: str  # full | truncated | dropped
    omitted_chars: int = 0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "priority": self.priority,
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "action": self.action,
            "omitted_chars": int(self.omitted_chars),
        }


def _budget_for(block: ContextBlock) -> int | None:
    if block.budget is not None:
        return block.budget
    return DEFAULT_BUDGETS.get(block.priority)


def _head_cut(text: str, budget: int) -> str:
    """Keep the header line plus as much body as fits the budget."""
    lines = text.split("\n", 1)
    header = lines[0] if lines else ""
    if len(header) >= budget:
        return header[:budget]
    room = budget - len(header) - 1
    body = lines[1] if len(lines) > 1 else ""
    return header + "\n" + body[: max(0, room)]


def _tail_lines(text: str, budget: int) -> str:
    """Keep the newest lines (history is oldest-first) within the budget."""
    lines = text.splitlines()
    if not lines:
        return text
    header = lines[0]
    body = lines[1:] if len(lines) > 1 else []
    kept: list[str] = []
    used = len(header) + 1
    for line in reversed(body):
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    return "\n".join([header, *kept]) if kept else header


def apply_budget(
    blocks: Sequence[ContextBlock],
    *,
    total_budget: int | None = None,
) -> tuple[ContextBundle, list[BudgetDecision]]:
    """Apply the per-block policy; returns kept blocks + one decision per block.

    处理方式来自 ``CUT_POLICY``（显式契约）；任何截断/丢弃都写进 decision 的
    ``omitted_chars`` 并打一条日志，不再"静默砍"。
    """
    decisions: list[BudgetDecision] = []
    kept: list[ContextBlock] = []

    for block in blocks:
        budget = _budget_for(block)
        size = block.chars
        strategy = cut_strategy(block.priority)
        if budget is None or size <= budget or strategy == CUT_NONE:
            kept.append(block)
            decisions.append(
                BudgetDecision(
                    block.name, block.source, block.priority, size, size, "full"
                )
            )
            continue

        if strategy == CUT_DROP:
            decisions.append(
                BudgetDecision(
                    block.name, block.source, block.priority, size, 0, "dropped",
                    omitted_chars=size,
                )
            )
            log.warning(
                "context_block_dropped",
                name=block.name,
                source=block.source,
                priority=block.priority,
                chars=size,
                budget=budget,
                reason="per_block_budget",
            )
            continue

        trimmed = (
            _tail_lines(block.content, budget)
            if strategy == CUT_TAIL
            else _head_cut(block.content, budget)
        )
        kept.append(replace(block, content=trimmed))
        decisions.append(
            BudgetDecision(
                block.name,
                block.source,
                block.priority,
                size,
                len(trimmed),
                "truncated",
                omitted_chars=max(0, size - len(trimmed)),
            )
        )
        log.info(
            "context_block_truncated",
            name=block.name,
            source=block.source,
            priority=block.priority,
            chars_before=size,
            chars_after=len(trimmed),
            budget=budget,
        )

    bundle = ContextBundle(blocks=tuple(kept))
    if total_budget and bundle.total_chars > total_budget:
        # Global guard: drop AUX first, then trim HISTORY from the oldest side.
        bundle, extra = _enforce_total(bundle, total_budget)
        decisions.extend(extra)
    return bundle, decisions


def _enforce_total(
    bundle: ContextBundle, total_budget: int
) -> tuple[ContextBundle, list[BudgetDecision]]:
    kept: list[ContextBlock] = []
    extra: list[BudgetDecision] = []
    for block in bundle.blocks:
        if bundle.total_chars <= total_budget:
            kept.append(block)
            continue
        strategy = cut_strategy(block.priority)
        if strategy == CUT_DROP:
            extra.append(
                BudgetDecision(
                    block.name, block.source, block.priority, block.chars, 0,
                    "dropped", omitted_chars=block.chars,
                )
            )
            log.warning(
                "context_block_dropped",
                name=block.name,
                source=block.source,
                priority=block.priority,
                chars=block.chars,
                reason="total_budget",
            )
            continue
        if strategy == CUT_TAIL:
            trimmed = _tail_lines(block.content, max(200, block.chars // 2))
            extra.append(
                BudgetDecision(
                    block.name,
                    block.source,
                    block.priority,
                    block.chars,
                    len(trimmed),
                    "truncated",
                    omitted_chars=max(0, block.chars - len(trimmed)),
                )
            )
            kept.append(replace(block, content=trimmed))
            continue
        kept.append(block)
    return ContextBundle(blocks=tuple(kept)), extra


__all__ = [
    "BudgetDecision",
    "DEFAULT_BUDGETS",
    "PERSONA",
    "RELATION",
    "MEMORY",
    "HISTORY",
    "AUX",
    "CUT_NONE",
    "CUT_HEAD",
    "CUT_TAIL",
    "CUT_DROP",
    "CUT_POLICY",
    "cut_strategy",
    "apply_budget",
]
