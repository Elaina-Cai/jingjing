# -*- coding: utf-8 -*-
"""⑤ 意愿契约（批D 拟人决策链步2，2026-10-15）。

Volition = 理解层（scene_read v2）+ 动因层（drive_of）合出来的"这一轮她怎么想"。
⑥ 只执行、⑦ 可最后改口；@ 权威仍是 AddresseeRef 管线，这里只带候选名。

纯结构 + 纯函数：不调 LLM、不读库、不落盘。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping
from safelog import warn

__all__ = [
    "IMPULSES",
    "POSTURES",
    "POSTURE_UNKNOWN",
    "TASK_STANCES",
    "DELIVERY_HINTS",
    "TaskStance",
    "Volition",
    "build_volition",
]

IMPULSES = ("answer", "proactive", "none")
POSTURES = ("答", "问", "帮", "护", "逗", "损", "接", "沉默")

#: P0-3：姿态「未判出」= 空串（不占语义格）。与 :class:`Volition.posture` 的既有默认同值，
#: 所以 off 档逐字不变；on 档没有判据时不再兜底「接」（那是猜的，不是判出来的）。
POSTURE_UNKNOWN = ""

TASK_STANCES = ("accept", "decline", "negotiate", "clarify_first")
DELIVERY_HINTS = ("short", "normal", "deliver")


@dataclass(frozen=True)
class TaskStance:
    value: str = ""  # accept / decline / negotiate / clarify_first；空 = 非任务轮
    reason: str = ""


@dataclass(frozen=True)
class Volition:
    speak: bool
    impulse: str = "none"
    posture: str = ""
    confidence: float = 0.0
    reason: str = ""
    target: str = ""
    hooks: tuple[str, ...] = ()
    about_subject: str = "unknown"
    about_her: bool | None = None
    genre: str = "unknown"
    mood_gate: float | None = None
    budget_allowed: bool | None = None
    needs_tools: bool = False
    must_not: tuple[str, ...] = ()
    task_stance: str = ""
    task_goal: str = ""
    task_next: str = ""
    delivery_hint: str = "normal"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _first(items: Any, default: str = "") -> str:
    if isinstance(items, (list, tuple)):
        return str(items[0]) if items else default
    return str(items or default)


def _posture_fallback(unknown_first_class: bool) -> str:
    """没有判据时的姿态：旧行为兜底「接」；P0-3 开 = 未判出（不占格）。"""

    return POSTURE_UNKNOWN if unknown_first_class else "接"


def build_volition(
    *,
    drive: Mapping[str, Any] | None = None,
    section: Mapping[str, Any] | None = None,
    mood: float | None = None,
    tools_on: bool = False,
    genre: str = "unknown",
    unknown_first_class: bool = False,
) -> Volition:
    """从 drive_of 载荷 + scene_read v2 段组装 Volition（确定性，无 LLM）。

    posture 映射（第一个命中生效）：
      沉默 -> 沉默；被叫到 -> 答；护主 -> 护；话题命中 -> 逗（玩笑/起哄）或接；
      path=answer -> 答；其余 speak -> 接。

    ``unknown_first_class=True``（P0-3）时，「没有判据 → 接」这一兜底改成
    :data:`POSTURE_UNKNOWN`（空串）；``False`` 逐字旧行为。
    """
    d = dict(drive or {})
    sec = dict(section or {})
    hooks = tuple(str(h) for h in d.get("hooks") or ())
    speak = bool(d.get("decision") == "speak")
    impulse = str(d.get("path") or ("answer" if speak else "none"))
    if impulse not in IMPULSES:
        impulse = "none"
    subject = str(sec.get("about") or "unknown")
    her = sec.get("about_her")
    if her is not None and not isinstance(her, bool):
        her = None
    try:
        confidence = round(float(sec.get("about_confidence") or 0.0), 2)
    except Exception:
        warn("core/contracts/volition.py:111 Exception", err=repr(__import__("sys").exc_info()[1]))
        confidence = 0.0
    posture = ""
    if not speak:
        posture = "沉默"
    elif "被叫到" in hooks:
        posture = "答"
    elif "护主" in hooks:
        posture = "护"
    elif "话题命中" in hooks:
        posture = "逗" if str(sec.get("genre") or genre or "") in ("玩笑", "起哄") else _posture_fallback(unknown_first_class)
    elif impulse == "answer":
        posture = "答"
    else:
        posture = _posture_fallback(unknown_first_class)
    delivery_hint = "normal"
    if str(d.get("task_goal") or "").strip() or str(d.get("task_stance") or "").strip():
        delivery_hint = "deliver"
    elif "话题命中" in hooks and str(sec.get("genre") or genre or "") in ("玩笑", "起哄"):
        delivery_hint = "short"
    elif impulse == "answer" and str(sec.get("genre") or genre or "") in ("玩笑", "起哄", "分享"):
        delivery_hint = "short"
    return Volition(
        speak=speak,
        impulse=impulse,
        posture=posture,
        confidence=confidence,
        reason=str(d.get("reason") or ""),
        target=_first(sec.get("targets")) or str(sec.get("target") or ""),
        hooks=hooks,
        about_subject=subject,
        about_her=her,
        genre=str(sec.get("genre") or genre or "unknown"),
        mood_gate=mood,
        budget_allowed=d.get("budget_allowed"),
        needs_tools=bool(tools_on),
        delivery_hint=delivery_hint,
    )
