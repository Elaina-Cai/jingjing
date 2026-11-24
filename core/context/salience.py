# -*- coding: utf-8 -*-
"""显著性（salience）读数：从②事件流里挑「此刻最要紧的几件事」。

纯函数、无 IO、无 LLM、不写②；三态（SALIENCE）默认 off：
off 不计算；shadow 只审计；on 额外在 [局面] 里带一行「要紧」。
事件 kind 权重集中在本表（可审计、可回放）；衰减半衰期缺省 24h。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping
from safelog import warn

__all__ = [
    "DEFAULT_HALF_LIFE_HOURS",
    "KIND_WEIGHTS",
    "SalientItem",
    "event_salience",
    "salience_digest",
    "salience_mode",
    "top_salient",
]

DEFAULT_HALF_LIFE_HOURS = 24.0

KIND_WEIGHTS = {
    "emotion_observed": 1.0,
    "episode_opened": 0.8,
    "episode_closed": 0.7,
    "position_observed": 0.7,
    "hypothesis_confirmed": 0.7,
    "hypothesis_contradicted": 0.7,
    "hypothesis_formed": 0.6,
    "fact_asserted": 0.6,
    "intention_opened": 0.5,
    "intention_closed": 0.5,
    "episode_touched": 0.5,
    "self_message": 0.35,
    "message_observed": 0.25,
    "perception_observed": 0.15,
}
_FALLBACK_WEIGHT = 0.2
_LABEL_KEYS = ("key", "reason", "content", "text", "value", "topic", "verdict")


@dataclass(frozen=True)
class SalientItem:
    kind: str
    label: str = ""
    score: float = 0.0
    at: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    source_ids: tuple[str, ...] = field(default=(), compare=False, repr=False)


def salience_mode() -> str:
    """SALIENCE 三态：off / shadow / on；默认 off。"""
    try:
        from core import features

        return features.mode(features.SALIENCE, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到按 off，不阻断
        warn("core/context/salience.py:62 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def _age_hours(at: Any, now: datetime | None) -> float | None:
    text = str(at or "").strip()
    if not text or now is None:
        return None
    try:
        from core.temporal import parse_naive_local

        stamp = parse_naive_local(text)
    except Exception:  # noqa: BLE001 - 解析不可用 -> 不衰减
        warn("core/context/salience.py:74 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    if stamp is None:
        return None
    try:
        return max(0.0, (now - stamp).total_seconds() / 3600.0)
    except Exception:  # noqa: BLE001 - 时区怪值 -> 不衰减
        warn("core/context/salience.py:80 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def _source_ids_of(event: Any) -> tuple[str, ...]:
    payload = getattr(event, "payload", None)
    refs: list[str] = []
    if isinstance(payload, Mapping):
        raw = payload.get("evidence_refs") or ()
        if isinstance(raw, str):
            raw = (raw,)
        if isinstance(raw, (list, tuple, set, frozenset)):
            refs.extend(str(item).strip() for item in raw if str(item).strip())
    if refs:
        return tuple(dict.fromkeys(refs))
    raw = json.dumps(
        {
            "kind": str(getattr(event, "kind", "") or ""),
            "at": str(getattr(event, "at", "") or ""),
            "payload": payload if isinstance(payload, Mapping) else {},
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return ("world:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16],)


def _label_of(event: Any) -> str:
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return ""
    for key in _LABEL_KEYS:
        raw = payload.get(key)
        if raw:
            return " ".join(str(raw).split())[:60]
    return ""


def event_salience(
    event: Any,
    *,
    now: datetime | None = None,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
) -> float:
    """单条事件的激活度：kind 权重 × 0.5 ** (age/half_life)。"""
    kind = str(getattr(event, "kind", "") or "")
    weight = KIND_WEIGHTS.get(kind, _FALLBACK_WEIGHT)
    hours = _age_hours(getattr(event, "at", ""), now)
    if hours is None:
        return float(weight)
    half = half_life_hours if half_life_hours > 0 else DEFAULT_HALF_LIFE_HOURS
    return float(weight) * (0.5 ** (hours / half))


def top_salient(
    events: Iterable[Any],
    *,
    limit: int = 3,
    now: datetime | None = None,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
) -> tuple[SalientItem, ...]:
    """按激活度排序取前 limit 条（同分按 kind/at 稳定）。"""
    scored = [
        SalientItem(
            kind=str(getattr(event, "kind", "") or ""),
            label=_label_of(event),
            score=event_salience(event, now=now, half_life_hours=half_life_hours),
            at=str(getattr(event, "at", "") or ""),
            payload=dict(getattr(event, "payload", {}) or {}) if isinstance(getattr(event, "payload", None), Mapping) else {},
            source_ids=_source_ids_of(event),
        )
        for event in events or ()
    ]
    scored = [item for item in scored if item.kind]
    scored.sort(key=lambda item: (-item.score, item.kind, item.at))
    return tuple(scored[: max(0, int(limit))])


def salience_digest(
    model: Any,
    *,
    limit: int = 3,
    now: datetime | None = None,
) -> dict[str, Any]:
    """审计用读数（不含原文之外的加工）。"""
    items = top_salient(getattr(model, "events", ()) or (), limit=limit, now=now)
    return {
        "top": [
            {"kind": item.kind, "label": item.label, "score": round(item.score, 3)}
            for item in items
        ],
        "count": len(items),
    }
