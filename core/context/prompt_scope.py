# -*- coding: utf-8 -*-
"""system prompt 的出站白名单（2026-10-23 主人第二次点名后的口径）。

主人原话（m07351）：「人设删掉：[人格] [基本表达] [不确定与沉默] [沉默协议] [拿不准时]
[安全与边界] [语气风格] [遇事怎么反应] [强制规则] [主动触达规则] [表达能力] [纠错确认]
[你说过·作数]，仅保留『你是鲸鲸。。。。』这个；世界窗口也仅保留世界窗口，现在这一屏」。

落地方式：**不下沉去改 15 个产者**（那是 15 个改一半就会漏的点），而是在
``agents/conversational.py::_build_system_prompt`` 的返回处做一次出站过滤——
所有块都拼完之后过一遍，不在白名单的整段丢掉并记 ``prompt_block_dropped``。
这样「谁还在喂」永远可数、可查、可回滚（``YINYUE_PROMPT_SCOPE=all``）。

保留的只有四类的载体：
- 人设：**只留首行身份句**（`core/terminal/persona_brain.py:278` 的 `你是{name}。`），
  `[人格]`（身份映射 + 性格倾向）与全部规则块都不再出现；
- 世界窗口：`[世界窗口]`、`[现在这一屏]`；
- 记忆：`[事件记忆]`、`[按需记忆]`、`[场外补充]`、`[群成员名单]`、`[关系事实]`；
- 在手的事：`[在手的事]`、`[本轮步骤]`、`要真办就附…`、`[本轮可用的工具]`。

**已知代价**（写给下一个读代码的人，不是保留意见）：
丢掉 `[沉默协议]` / `[不确定与沉默]` 之后，模型手里不再有「什么时候可以不出声」的规矩，
`[SILENT]` 只剩工具契约能触发；丢掉 `[强制规则]`（瞎图不编描述）之后，图看不出来时
不再有硬约束。这两条若在生产上表现不对，回滚开关是 ``YINYUE_PROMPT_SCOPE=all``。
"""
from __future__ import annotations

import os
from typing import Any
from safelog import warn

SCOPE_ENV = "YINYUE_PROMPT_SCOPE"

#: 保留的块头前缀（与 `core/context/prompt_layers.py::BLOCK_LAYER` 同口径）。
KEEP_BLOCKS: frozenset[str] = frozenset(
    {
        # ── 世界窗口：只留这两块 ──────────────────────────────────────
        "[世界窗口]",
        "[现在这一屏]",
        # ── 记忆立方体 ───────────────────────────────────────────────
        "[事件记忆",
        "[按需记忆]",
        "[场外补充]",
        "[群成员名单",
        "[关系事实]",
        # ── 在手的事 ─────────────────────────────────────────────────
        "[在手的事]",
        "[本轮步骤]",
        "要真办就附",
        "[本轮可用的工具]",
    }
)

#: 人设层唯一保留物：没有方括号的身份首行（`你是鲸鲸。`）。它不是块，过滤器不动它。
IDENTITY_LINE_KEPT = True


def scope_mode() -> str:
    """``four``（默认，按主人点名保留）/ ``all``（回滚成全部注入）。"""
    raw = str(os.environ.get(SCOPE_ENV, "") or "").strip().lower()
    return "all" if raw == "all" else "four"


def _prefixes() -> tuple[str, ...]:
    from core.context.prompt_layers import BLOCK_LAYER, RETIRED_BLOCKS

    # 现役 + 已退役：退役的块头必须继续被认识，否则哪天有人把产者搬回来，
    # 它就绕过白名单直接进 prompt 了（闸门只会对这种"复活"失效）。
    return tuple(sorted(set(BLOCK_LAYER) | set(RETIRED_BLOCKS), key=len, reverse=True))


def prefix_of(line: str, prefixes: tuple[str, ...] | None = None) -> str:
    """这一行是不是某个已登记块的开头；是就返回那个前缀。"""
    text = str(line or "").strip()
    if not text.startswith("["):
        return ""
    for prefix in prefixes or _prefixes():
        if text.startswith(prefix):
            return prefix
    return ""


def apply_scope(text: str, *, log: Any = None) -> tuple[str, tuple[str, ...]]:
    """出站过滤：返回 (过滤后的 prompt, 被丢掉的块头元组)。"""
    body = str(text or "")
    if not body or scope_mode() == "all":
        return body, ()
    prefixes = _prefixes()
    kept: list[str] = []
    dropped: list[str] = []
    current_kept = True
    for line in body.split("\n"):
        head = prefix_of(line, prefixes)
        if head:
            current_kept = head in KEEP_BLOCKS
            if not current_kept:
                dropped.append(head)
        if current_kept:
            kept.append(line)
    out = "\n".join(kept)
    # 丢段之后会留下连续空行，压到最多两行（不裁剪内容行）。
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    out = out.strip()
    if dropped and log is not None:
        try:
            log.info(
                "prompt_block_dropped",
                count=len(dropped),
                blocks=dropped,
                scope=scope_mode(),
            )
        except Exception:  # noqa: BLE001 - 记账失败不许影响出话
            warn("core/context/prompt_scope.py:112 Exception", err=repr(__import__("sys").exc_info()[1]))
    return out, tuple(dropped)


__all__ = ["SCOPE_ENV", "KEEP_BLOCKS", "IDENTITY_LINE_KEPT", "scope_mode", "prefix_of", "apply_scope"]