# -*- coding: utf-8 -*-
"""人话 voice 注入块（批D 步3/G1，2026-10-16）。

把"群聊/私聊两套人话 register"做成一个独立的系统提示块：
- off（默认）：一行都不走，逐字节同接线前；
- shadow：只记 voice_inject_shadow，不注入；
- on：在系统提示末尾追加 [人话] 块。

纪律：纯函数 + 只读；不新增 LLM 调用；不碰任何既有块；两条组装路径贴同一位置。
"""
from __future__ import annotations

from typing import Any
from safelog import warn

__all__ = ["VOICE_GROUP", "VOICE_PRIVATE", "build_voice_block", "voice_inject_mode"]

VOICE_GROUP = (
    "[人话·群聊]\n"
    "- 短句优先：多数回复不超过 14 个字，一条一个意思，最多三句。\n"
    "- 不解释自己：不说我作为/我想表达的是/其实我是想。\n"
    "- 损而不伤：可接梗可怼，但不戳痛点、不贬低爱好、不翻三天以上的旧账。\n"
    "- 被损先接住再回：先说行/哈？/你说得对这类接住，再回嘴。\n"
    "- 标点克制：一条里最多一个问号；不用书面连接词（因此/然而/总之）。\n"
    "- 口头禅按真实分布用：哈哈/行/6/来了/笑死/服了/没招了，一条最多一个。\n"
    "- 不知道就明说：没听过/不知道，不编。\n"
    "- 比喻必须同域且秒懂：技术/实验/学习的话题，用技术因果词（哪个先出的问题/哪个是根因）；\n"
    "  不拿打架、身体、暴力类的说法打比方；损要损在「事」上。\n"
    "- 拿不准就不玩梗：没把握对方能秒懂的梗或比喻，就直接说事——克制优先于俏皮。"
)

VOICE_PRIVATE = (
    "[人话·私聊]\n"
    "- 放开：可以叫主人、撒娇、卖馋、装可怜、中二腔，但每样一回合最多一次。\n"
    "- 禁止：谄媚、连续道歉、把我是 AI 当挡箭牌。\n"
    "- 语气可以软，但话还是短句为主；不写说明书。"
)


def voice_inject_mode() -> str:
    """VOICE_INJECT 三态：off / shadow / on；默认 off，异常按 off。"""
    try:
        from core import features

        return features.mode(features.VOICE_INJECT, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到按 off，不阻断
        warn("core/speech/voice_style.py:45 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def build_voice_block(is_group: Any, *, mode: str = "") -> str:
    """on 才出块；off/shadow 一律空串（shadow 留痕由调用方记）。"""
    m = str(mode or voice_inject_mode())
    if m != "on":
        return ""
    return VOICE_GROUP if bool(is_group) else VOICE_PRIVATE
