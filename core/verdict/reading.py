"""Reading：模型只回答「他要什么」（5 个标量），不回答「这有多危险」。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from core.intake.observation import Observation
from safelog import warn

log = logging.getLogger(__name__)

MAX_QUOTES = 3
QUOTE_MAX_CHARS = 60
MAX_SLOTS = 6
SLOT_NAME_MAX_CHARS = 24
TIMING_NOW, TIMING_LATER, TIMING_CONDITION, TIMING_FOREVER = 0, 1, 2, 3
TIMINGS = (TIMING_NOW, TIMING_LATER, TIMING_CONDITION, TIMING_FOREVER)
LOW_CONFIDENCE = 0.5

READING_SYSTEM = (
    "你是判读器。只输出一个 JSON 对象，不要解释。字段：\n"
    '{"quotes":["消息里能指回的原话片段"],"slots":{"参数名":"值"},'
    '"gaps":缺几个必填参数,"timing":0立刻1定时2条件3长期,"affect":-1到1,"confidence":0到1}\n'
    "quotes 必须是原文片段（不许编）；指不回原文就留空数组。"
    "slots 只放消息里已经出现的参数；缺的参数不要猜，把它计入 gaps。"
    "affect 是说话人的情绪（负=难受），不是你的情绪。"
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_OBJ_RE = re.compile(r"\{.*\}", re.S)


@dataclass(frozen=True)
class Reading:
    """判读结果（模型能说的全部）。"""

    quotes: tuple[str, ...] = ()
    slots: dict = field(default_factory=dict)
    gaps: int = 0
    timing: int = TIMING_NOW
    affect: float = 0.0
    confidence: float = LOW_CONFIDENCE

    def has_quote(self) -> bool:
        """有没有能指回原文的引用。"""
        return bool(self.quotes)

    def wants_action(self) -> bool:
        """是不是要动手（有槽位或有缺口）。"""
        return bool(self.slots) or self.gaps > 0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_reading(payload: Any) -> "Reading | None":
    """把模型输出解析成 Reading（坏 JSON/坏类型 -> None，不抛）。"""
    data = payload
    if isinstance(payload, str):
        text = payload.strip()
        found = _FENCE_RE.search(text) or _OBJ_RE.search(text)
        if not found:
            log.info("reading_parse_failed reason=no_json")
            return None
        try:
            data = json.loads(found.group(1) if found.groups() else found.group(0))
        except Exception as exc:
            log.info("reading_parse_failed reason=bad_json error=%s", exc)
            return None
    if not isinstance(data, Mapping):
        log.info("reading_parse_failed reason=not_object")
        return None

    quotes: list[str] = []
    for item in list(data.get("quotes") or [])[:MAX_QUOTES]:
        text = " ".join(str(item or "").split())[:QUOTE_MAX_CHARS]
        if text:
            quotes.append(text)

    slots: dict = {}
    raw_slots = data.get("slots") or {}
    if isinstance(raw_slots, Mapping):
        for key, value in list(raw_slots.items())[:MAX_SLOTS]:
            name = str(key or "").strip()[:SLOT_NAME_MAX_CHARS]
            if name:
                slots[name] = str(value if value is not None else "")[:200]

    try:
        gaps = max(0, int(data.get("gaps") or 0))
    except Exception:
        warn("core/verdict/reading.py:94 Exception", err=repr(__import__("sys").exc_info()[1]))
        gaps = 0
    try:
        timing = int(data.get("timing") or 0)
    except Exception:
        warn("core/verdict/reading.py:98 Exception", err=repr(__import__("sys").exc_info()[1]))
        timing = TIMING_NOW
    if timing not in TIMINGS:
        timing = TIMING_NOW
    try:
        affect = _clamp(float(data.get("affect") or 0.0), -1.0, 1.0)
    except Exception:
        warn("core/verdict/reading.py:104 Exception", err=repr(__import__("sys").exc_info()[1]))
        affect = 0.0
    try:
        confidence = _clamp(float(data.get("confidence") or 0.0), 0.0, 1.0)
    except Exception:
        warn("core/verdict/reading.py:108 Exception", err=repr(__import__("sys").exc_info()[1]))
        confidence = 0.0

    return Reading(
        quotes=tuple(quotes),
        slots=slots,
        gaps=gaps,
        timing=timing,
        affect=affect,
        confidence=confidence,
    )


def build_reading_prompt(obs: Observation, *, capability_hint: str = "") -> list[dict]:
    """组装一次判读调用（不走工具、只出 JSON）。"""
    body = obs.text or "（无正文）"
    parts = [f"场景：{'群聊' if obs.is_group() else '私聊'}", f"说话人：{obs.by or '未知'}"]
    if obs.mention_to_me:
        parts.append("他点了我")
    if obs.form != "text":
        parts.append(f"形式：{obs.form}")
    if obs.quoted:
        parts.append(f"引用：{obs.quoted.get('text', '')[:120]}")
    if capability_hint:
        parts.append(f"我能做的事：{capability_hint[:200]}")
    parts.append(f"原话：{body}")
    return [
        {"role": "system", "content": READING_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
