# -*- coding: utf-8 -*-
"""归属的**渲染/事实入口**（[消息归属] 块 + 窗口逐行后缀）。

判定不在这里：唯一 owner 是 :func:`core.conversation.addressing.resolve_attribution`
（读时判定、不落库、零迁移）。本模块只负责把事实变成模型读得懂的字：

* :func:`render_attribution_facts` → 常驻 ``[消息归属]`` 块（本轮的归属事实行），
  由 ``core/terminal/persona_brain`` 注入；
* :func:`attribution_suffix` → 窗口逐行的归属后缀（``（→ @Elaina）`` /
  ``[归属不明]``），由 ``core/context/chat_window._fmt_full`` 使用；
* :func:`render_attribution_line` → 单条事实行（topic_line / 记忆抽取 / 准入
  渲染共用同一口径）。

纪律（2026-09-30 19:08 事故后收口）：
  * 只有 ``exact`` / ``unresolved`` 两态；``unresolved`` 一律渲染成
    ``[归属不明]``，**不许**回落成"最近说话的人"；
  * 没有 @ / 引用 = 「没有明确目标」，是正常事实，不是失败，不加任何标记；
  * 名字只是显示；身份判等走 person_id（调用方显式注入
    ``person_identities`` 的索引）——重名不消歧、也不指派；
  * **纯函数、无 IO**：渲染路径不查库、不 import ``core.bootstrap``
    （那条路会把 ``.env`` 灌进 ``os.environ``，等于中途改开关）。

开关：``YINYUE_FEATURE_ATTRIBUTION``（默认开）。关掉 → 退回旧渲染路径
（不加块、不加后缀），不会半开半关。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from core.conversation.addressing import (
    CONFIDENCE_UNRESOLVED,
    SOURCE_QUOTE_BOT,
    Attribution,
    resolve_attribution,
)
from core.terminal.speaker import BOT_NAME
from safelog import warn

BLOCK_HEADER = "[消息归属]"
UNRESOLVED_MARK = "[归属不明]"
MAX_FACTS = 12          # 事实行上限（够看清最近一轮，不撑 prompt）
MAX_FACT_CHARS = 40     # 事实行里正文摘要的长度

def attribution_enabled() -> bool:
    """``YINYUE_FEATURE_ATTRIBUTION``（默认开；设 0 回退旧渲染路径）。"""
    try:
        from core import features

        return bool(features.enabled(features.ATTRIBUTION, True))
    except Exception:  # noqa: BLE001 - 开关读取失败按默认开
        warn("core/context/attribution.py:51 Exception", err=repr(__import__("sys").exc_info()[1]))
        return True


# ------------------------------------------------------------------ 判定上下文
@dataclass(frozen=True)
class AttributionContext:
    """一次渲染/判定用的口径（她自己是谁 + 身份索引 + 群/私聊）。"""

    self_name: str = BOT_NAME
    self_person_id: str = ""
    self_aliases: tuple[str, ...] = ()
    identities: Mapping[str, str] = field(default_factory=dict)
    display_names: Mapping[str, str] = field(default_factory=dict)
    is_group: bool | None = None

    def resolve(self, row: Any, *, parent: Any = None) -> Attribution | None:
        return resolve_attribution(
            row,
            self_name=self.self_name,
            self_person_id=self.self_person_id,
            self_aliases=self.self_aliases,
            identities=self.identities,
            display_names=self.display_names,
            is_group=self.is_group,
            parent=parent,
        )


def default_context(
    *,
    self_name: str = "",
    self_person_id: str = "",
    self_aliases: Sequence[str] = (),
    is_group: bool | None = None,
    identities: Mapping[str, str] | None = None,
    display_names: Mapping[str, str] | None = None,
) -> AttributionContext:
    """构造上下文（**纯函数、不查库**）。

    ``self_name`` 空 = 用 ``core.terminal.speaker.BOT_NAME``（她自己的稳定显示
    名）；知道人设名的调用方（persona_brain / router）显式传进来。身份索引
    只能由调用方**显式注入**——渲染路径绝不自己去 ``bootstrap`` 查
    ``person_identities``：那条路会把 ``.env`` 灌进 ``os.environ``
    （``core.bootstrap`` → ``core.env.load_yinyue_env_defaults``），等于在渲染
    中途改开关（2026-09-30 实测把 turns/uncertain_voice 的开关注入后，3 个用例
    的表现随调用顺序漂移）。名字照写、person_id 由调用方给，才是"名字只作显示"。
    """
    return AttributionContext(
        self_name=str(self_name or "").strip() or BOT_NAME,
        self_person_id=str(self_person_id or ""),
        self_aliases=tuple(
            str(a) for a in (self_aliases or ()) if str(a or "").strip()
        ),
        identities=dict(identities or {}),
        display_names=dict(display_names or {}),
        is_group=is_group,
    )


# ------------------------------------------------------------------ 文案渲染
def _stamp(row: Any) -> str:
    for key in ("occurred_at", "created_at", "timestamp", "at"):
        value = row.get(key) if isinstance(row, Mapping) else getattr(row, key, None)
        if isinstance(value, datetime):
            return value.strftime("%H:%M")
    return ""


def _clip(text: Any, limit: int = MAX_FACT_CHARS) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def attribution_target(attribution: Any) -> str:
    """收件人的渲染片段：``@A、@B`` / ``[归属不明]`` / ``""``（没有明确目标）。"""
    if attribution is None:
        return ""
    if attribution.confidence == CONFIDENCE_UNRESOLVED:
        return UNRESOLVED_MARK
    if not attribution.addressees:
        return ""
    return "、".join(f"@{name}" for name in attribution.addressee_names())


def render_attribution_line(
    row: Any, *, ctx: AttributionContext | None = None, attribution: Any = None,
    always: bool = False,
) -> str:
    """一条归属事实行：``- 19:10 P3 → @Elaina：「@Elaina 同一句」``。

    没有明确目标（无 @ 无引用）→ ``""``（那是正常事实，不必占一行）；
    认不出收件人 → ``→ [归属不明]``（显式失败，绝不指派）。
    ``always=True`` 时无目标的行走 ``- 19:10 P3：「今天天气不错」``
    （给"要把每条都列给模型看"的调用方，如话题线）。开关关掉 → ``""``。
    """
    if not attribution_enabled():
        return ""
    context = ctx or default_context()
    attr = attribution if attribution is not None else context.resolve(row)
    if attr is None:
        return ""
    who = "你" if attr.speaker.is_self else (attr.speaker.display_name or "?")
    stamp = _stamp(row)
    prefix = f"{stamp} " if stamp else ""
    content = _clip(
        row.get("content") if isinstance(row, Mapping) else getattr(row, "content", "")
    )
    target = attribution_target(attr)
    if not target and not always:
        return ""
    body = f"：「{content}」" if content else ""
    arrow = f" → {target}" if target else ""
    return f"- {prefix}{who}{arrow}{body}"


def render_attribution_facts(
    rows: Sequence[Any] | None,
    *,
    ctx: AttributionContext | None = None,
    limit: int = MAX_FACTS,
    is_group: bool | None = None,
) -> str:
    """``[消息归属]`` 块：本轮的归属事实行（默认最多 12 行）。

    WP-10 第 1 行（2026-10-10）：三条硬规则文字已退役，canonical 归属 +
    窗口逐行后缀是唯一 owner；这里只保留事实行。
    开关关掉 → ``""``（调用方什么都不注入，退回旧渲染），不会只注一半。
    """
    if not attribution_enabled():
        return ""
    context = ctx or default_context(is_group=is_group)
    lines: list[str] = []
    facts: list[str] = []
    for row in rows or ():
        line = render_attribution_line(row, ctx=context)
        if line:
            facts.append(line)
    if facts:
        lines.extend(facts[-max(1, int(limit)):])
    if not lines:
        # 只有块名、没有规则/事实：保持块存在但不留尾换行，避免装配处多一空行。
        return BLOCK_HEADER
    return BLOCK_HEADER + "\n" + "\n".join(lines)


def attribution_suffix(
    row: Any, *, ctx: AttributionContext | None = None,
) -> str:
    """窗口逐行的归属后缀：``（→ @Elaina）`` / `` [归属不明]`` / ``""``。

    * 没有明确目标 → ``""``（正常"无目标"，不是失败）；
    * 认不出 → `` [归属不明]``（显式说"不知道"，不许猜最近说话人）。
    """
    if not attribution_enabled():
        return ""
    context = ctx or default_context()
    attr = context.resolve(row)
    if attr is None:
        return ""
    if attr.confidence == CONFIDENCE_UNRESOLVED:
        return f" {UNRESOLVED_MARK}"
    if not attr.addressees:
        return ""
    return "（→ " + "、".join(f"@{name}" for name in attr.addressee_names()) + "）"


def quote_facts(
    content: Any, *, ctx: AttributionContext | None = None,
    self_names: Sequence[str] = (),
) -> tuple[Any, bool]:
    """``(引用事实 QuotedRef, 引用的是不是她自己)``——引用判定的唯一入口。

    ``QuotedRef.present`` 区分"没有引用"与"有引用但认不出作者"：
    后者（``[引用〔作者不明〕: …]`` / ``[引用: …]`` / 引用的是媒体）既不是
    "引用她"也不是"引用别人"，消费方必须按 unresolved 处理。
    """
    names = [str(n).strip() for n in (self_names or ()) if str(n or "").strip()]
    if ctx is None:
        # 引用判定只比"她自己的名字"，不查身份表（纯函数、可单测、无 IO）。
        ctx = default_context(
            self_name=names[0] if names else "",
            self_aliases=names[1:] if len(names) > 1 else (),
        )
    attr = ctx.resolve({"content": content})
    if attr is None:
        from core.conversation.addressing import QuotedRef

        return QuotedRef(), False
    is_self = any(
        addressee.is_self and addressee.source == SOURCE_QUOTE_BOT
        for addressee in attr.addressees
    )
    if not is_self and attr.quoted.resolvable and attr.quoted.author_name:
        is_self = attr.quoted.author_name in (set(ctx.self_aliases) | {ctx.self_name})
    return attr.quoted, bool(is_self)


def quoted_is_self(content: Any, *, ctx: AttributionContext | None = None,
                   self_names: Sequence[str] = ()) -> bool:
    """这条消息引用的**是不是她自己**（``[引用 鲸鲸: …]``）。

    给 self_contract 这类"窄例外"用：引用她 = 在跟她说，且不能被
    ``[引用〔作者不明〕]`` 冒充。
    """
    quoted, is_self = quote_facts(content, ctx=ctx, self_names=self_names)
    return bool(quoted.present and is_self)


__all__ = [
    "AttributionContext",
    "BLOCK_HEADER",
    "MAX_FACTS",
    "UNRESOLVED_MARK",
    "attribution_enabled",
    "attribution_suffix",
    "attribution_target",
    "default_context",
    "quote_facts",
    "quoted_is_self",
    "render_attribution_facts",
    "render_attribution_line",
]
