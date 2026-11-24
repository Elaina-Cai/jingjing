"""Lightweight command/question/request classification for Phase 0 shadows.

This is intentionally heuristic and conservative: it only decides how to tag an
event.  It does not execute anything and does not change reply behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.terminal.event_model import EventType

_COMMAND_MARKERS = (
    "帮我发",
    "帮我查",
    "帮我找",
    "帮我做",
    "帮我处理",
    "帮我预约",
    "帮我联系",
    "帮我转告",
    "帮我告诉",
    "帮我提醒",
    "给我发",
    "给我查",
    "发个消息",
    "发消息给",
    "发送给",
    "转告",
    "通知一下",
    "提醒我",
    "提醒一下",
    "设置一个",
    "设置提醒",
    "安排一下",
    "帮我安排",
    "删除",
    "取消",
    "关闭",
    "打开",
    "执行",
    "去办",
    "记得",
)
_REQUEST_MARKERS = (
    "帮我",
    "帮忙",
    "麻烦",
    "请帮",
    "拜托",
    "能不能",
    "可不可以",
    "可以帮",
    "可以给",
)
_QUESTION_MARKERS = (
    "?",
    "？",
    "吗",
    "呢",
    "什么",
    "怎么",
    "为什么",
    "哪里",
    "哪个",
    "谁",
    "多少",
    "几点",
    "是否",
    "有没有",
    "是不是",
    "能不能",
    "可不可以",
)


@dataclass(frozen=True)
class IntentResult:
    event_type: EventType
    reasons: tuple[str, ...] = field(default_factory=tuple)


def classify_event_type(text: str) -> EventType:
    return classify_intent(text).event_type


def classify_intent(text: str) -> IntentResult:
    value = str(text or "").strip()
    if not value:
        return IntentResult(EventType.MESSAGE, ())
    command_hits = [m for m in _COMMAND_MARKERS if m in value]
    if command_hits:
        return IntentResult(EventType.COMMAND, tuple(command_hits[:3]))
    request_hits = [m for m in _REQUEST_MARKERS if m in value]
    if request_hits:
        return IntentResult(EventType.REQUEST, tuple(request_hits[:3]))
    question_hits = [m for m in _QUESTION_MARKERS if m in value]
    if question_hits:
        return IntentResult(EventType.QUESTION, tuple(question_hits[:3]))
    return IntentResult(EventType.MESSAGE, ())
