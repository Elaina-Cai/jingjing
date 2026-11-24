# -*- coding: utf-8 -*-
"""自指契约（B5）：别人以第三人称谈论她时，那说的是她本人。

事故原型（2026-09-24 12:40）：群里引用她的消息问「他为什么要回复我」，她回
「你问它为什么回复你，它自己都答不上来——我猜是手滑。」——把自己说成了第三方。

事故原型二（2026-09-30 18:34，本块的**反方向**偏置）：P3 说「@鲸鲸 对啊
对啊，他在霸凌我」（「他」= Elaina，她刚连拍四个人却没拍 P3），旧文案
「在指你时，说的都是你本人」被无条件读成「他=你」，她于是把别人的抱怨认领到
自己头上、回了「我拍没拍你，你自己心里没数吗」。修正：常驻块第 1 条收窄成
「**被点名 / 被引用 / 被明确提到鲸鲸**时，他/她/它才指你」，其余代词交给
``core.context.referent.bind_pronouns`` 的事实行（[指代事实]）去绑具体人。

WP-10 第 4 行退役（2026-10-10）：常驻块 ``SELF_BLOCK`` 与回合提醒
``self_reference_note`` 已删（prompt 散文退役）；确定性 owner 保留——出口守卫
``reply_speaks_of_self_in_third_person(reply)`` 命中时由出口改写
（原 ``ConversationalAgent.rerender_self_voice``，2026-10-23 退役：出口不再改写，
只观测 `self_voice_third_person`），不让她用旁观口吻说自己。

规则只认窄特征；拿不准不拦（假阳性会把正常第三人称叙述改坏，所以宁可窄）。
"""

from __future__ import annotations

import os
import re
from typing import Any, Sequence
from safelog import warn
from core.infra.envflags import text

BOT_NAMES = ("鲸鲸",)

_LLM_WORDS = r"(?<![A-Za-z])(LLM|AI)(?![A-Za-z])|人工智能|大模型|机器人|人机|智能体"
_META_MARKERS = re.compile(_LLM_WORDS, re.IGNORECASE)
# 输入侧：「他为什么要回复我」——第三人称 + 回复类动词 + 我/你/这/那。
_INPUT_TP = re.compile(
    r"[她他它].{0,8}(回复|回答|答|看到|看见|说|发|拍).{0,6}[我你这那]"
)
# 输入侧：议论她的触发/引用行为（「之前引用好像也可以」）。
_INPUT_BEHAVIOR = re.compile(
    r"(引用|艾特|触发|回复).{0,6}(可以|也行|也能|好像|是不是)"
)
# 出口侧：把她自己写成第三人称的窄特征。
_REPLY_SELF_TP = (
    re.compile(r"它自己.{0,6}(答|回复|说|看|发|想|做)"),
    re.compile(r"[他它].{0,4}(回复|回答|答|看到|看见).{0,4}[你我这那]"),
    re.compile(r"这(个)?(LLM|AI|大模型|机器人|人机|智能体)", re.IGNORECASE),
)

def self_reference_kind(
    text: str,
    *,
    persona_names: Sequence[str] = (),
) -> str:
    """How this turn's input refers to her, or "" (not about her)."""
    raw = str(text or "")
    if _META_MARKERS.search(raw):
        return "llm_marker"
    if _INPUT_TP.search(raw):
        return "third_person"
    if _INPUT_BEHAVIOR.search(raw):
        return "behavior"
    # 引用她的判定走 canonical（`[引用〔作者不明〕]` 不会被当成"引用她"）。
    from core.context.attribution import quoted_is_self

    names = [
        str(name).strip()
        for name in tuple(persona_names or ()) + BOT_NAMES
        if str(name or "").strip()
    ]
    if names and quoted_is_self(raw, self_names=names):
        return "quoted_self"
    return ""


_NORM_RE = re.compile(r"""[\s\u3002\uff0c\uff01\uff1f\uff1b\uff1a\u3001\uff08\uff09\u3010\u3011\u300a\u300b\u2026\u2014\uff5e\u00b7,.!?;:'\"()\[\]<>-]+""")


def continuity_mode() -> str:
    """YINYUE_FEATURE_SELF_CONTINUITY 三态：off / shadow / on；默认 off。"""
    try:
        from core import features

        return features.mode(features.SELF_CONTINUITY, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到按 off
        warn("core/context/self_contract.py:83 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def continuity_scope() -> tuple[str, ...]:
    """YINYUE_SELF_CONTINUITY_CHATS：生效会话白名单（空 = 全部）。"""
    raw = text("YINYUE_SELF_CONTINUITY_CHATS")
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


def in_continuity_scope(chat_id: Any) -> bool:
    """chat_id 是否在连续性生效范围内。"""
    scope = continuity_scope()
    if not scope:
        return True
    return str(chat_id or "") in scope


def recent_self_lines(model: Any, *, limit: int = 5, max_chars: int = 240) -> tuple[str, ...]:
    """她最近说过的话（② self_message 投影；旧 -> 新；超长截断）。"""
    try:
        from core.context.situation import project

        view = project(model.events)
        items = list(getattr(view, "self_messages", ()) or ())
    except Exception:  # noqa: BLE001 - 读不到就当没有
        warn("core/context/self_contract.py:108 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ()
    out: list[str] = []
    chars = 0
    for item in reversed(items):
        text = " ".join(str(getattr(item, "content", "") or "").split())
        if not text:
            continue
        out.append(text)
        chars += len(text)
        if len(out) >= limit or chars >= max_chars:
            break
    return tuple(reversed(out))


def duplicate_of(reply: str, lines: "Sequence[str]") -> str:
    """reply 是不是复读了自己最近说过的话：命中返回那条原文，否则空串。"""
    raw = str(reply or "").strip()
    if len(raw) < 4:
        return ""
    norm = _NORM_RE.sub("", raw)
    if len(norm) < 4:
        return ""
    for line in lines:
        line_norm = _NORM_RE.sub("", str(line))
        if len(line_norm) < 3:
            continue
        if norm == line_norm or (len(norm) >= 6 and len(norm) <= len(line_norm) and norm in line_norm):
            return str(line)
    return ""


def self_continuity_block(lines: "Sequence[str]") -> str:
    """生成前注入：你说过的话作数（不许自相矛盾）。"""
    items = [line for line in lines if str(line).strip()]
    if not items:
        return ""
    body = "\n".join(f"- {line}" for line in items)
    return f"[你说过·作数]\n{body}\n（这几条算数：不许和它们自相矛盾；真记不清就说记不清。）"


def reply_speaks_of_self_in_third_person(reply: str) -> bool:
    """Narrow detector for a reply that talks about herself in third person."""
    raw = str(reply or "")
    return any(pattern.search(raw) for pattern in _REPLY_SELF_TP)


__all__ = [
    "BOT_NAMES",
    "continuity_mode",
    "duplicate_of",
    "recent_self_lines",
    "reply_speaks_of_self_in_third_person",
    "self_continuity_block",
    "self_reference_kind",
]
