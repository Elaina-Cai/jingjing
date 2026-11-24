"""Runtime overrides for addressee resolver weights and thresholds (P3).

Operators should not have to edit ``resolver.py`` defaults to apply a tuning
suggestion.  Precedence, lowest to highest:

1. resolver defaults;
2. ``YINYUE_CONVERSATION_TUNING_FILE`` -- the JSON produced by
   ``python -m core.conversation.tune``;
3. ``YINYUE_CONVERSATION_WEIGHTS`` -- a JSON object merged over (2);
4. explicit threshold variables:
   ``YINYUE_CONVERSATION_REPLY_THRESHOLD``,
   ``YINYUE_CONVERSATION_OPEN_REPLY_THRESHOLD``,
   ``YINYUE_CONVERSATION_OBSERVE_THRESHOLD``.

The loader is intentionally defensive: a malformed file or value is ignored
with a warning so a bad tuning experiment can never stop message routing.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from safelog import warn

log = logging.getLogger(__name__)

WEIGHTS_ENV = "YINYUE_CONVERSATION_WEIGHTS"
TUNING_FILE_ENV = "YINYUE_CONVERSATION_TUNING_FILE"
REPLY_THRESHOLD_ENV = "YINYUE_CONVERSATION_REPLY_THRESHOLD"
OPEN_REPLY_THRESHOLD_ENV = "YINYUE_CONVERSATION_OPEN_REPLY_THRESHOLD"
OBSERVE_THRESHOLD_ENV = "YINYUE_CONVERSATION_OBSERVE_THRESHOLD"

_FILE_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}


@dataclass(frozen=True)
class ResolverOverrides:
    weights: dict[str, float] = field(default_factory=dict)
    reply_threshold: float | None = None
    open_reply_threshold: float | None = None
    observe_threshold: float | None = None
    sources: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(
            self.weights
            or self.reply_threshold is not None
            or self.open_reply_threshold is not None
            or self.observe_threshold is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": dict(sorted(self.weights.items())),
            "reply_threshold": self.reply_threshold,
            "open_reply_threshold": self.open_reply_threshold,
            "observe_threshold": self.observe_threshold,
            "sources": list(self.sources),
        }


def _clean_weights(raw: Any) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            log.warning(
                "conversation_weight_ignored key=%s value=%s", key, value,
            )
            continue
        if number != number or number in (float("inf"), float("-inf")):
            continue
        out[str(key)] = round(max(-1.0, min(1.0, number)), 4)
    return out


def _clean_threshold(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        warn("core/conversation/config.py:87 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return None
    if number != number:
        return None
    if number < 0.0 or number > 1.0:
        log.warning("conversation_threshold_ignored value=%s", value)
        return None
    return round(number, 4)


def _read_tuning_file(raw_path: str) -> dict[str, Any]:
    path = Path(raw_path)
    try:
        stat = path.stat()
    except OSError as exc:
        log.warning(
            "conversation_tuning_file_unreadable path=%s error=%s",
            raw_path, str(exc)[:120],
        )
        return {}
    key = str(path.resolve())
    cached = _FILE_CACHE.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "conversation_tuning_file_invalid path=%s error=%s",
            raw_path, str(exc)[:120],
        )
        return {}
    if not isinstance(data, Mapping):
        return {}
    result = dict(data)
    _FILE_CACHE[key] = (stat.st_mtime, stat.st_size, result)
    return result


def load_overrides() -> ResolverOverrides:
    """Return the effective overrides for a new resolver instance."""
    weights: dict[str, float] = {}
    reply_threshold: float | None = None
    open_reply_threshold: float | None = None
    observe_threshold: float | None = None
    sources: list[str] = []

    # 2. tuning suggestion file
    raw_file = (os.environ.get(TUNING_FILE_ENV) or "").strip()
    if raw_file:
        data = _read_tuning_file(raw_file)
        if data:
            file_weights = _clean_weights(data.get("suggested_weights"))
            if file_weights:
                weights.update(file_weights)
            suggested = data.get("suggested_thresholds")
            if isinstance(suggested, Mapping):
                reply_threshold = _clean_threshold(
                    suggested.get("reply_threshold")
                )
                open_reply_threshold = _clean_threshold(
                    suggested.get("open_reply_threshold")
                )
                observe_threshold = _clean_threshold(
                    suggested.get("observe_threshold")
                )
            sources.append(f"file:{raw_file}")

    # 3. explicit JSON weights
    raw_weights = (os.environ.get(WEIGHTS_ENV) or "").strip()
    if raw_weights:
        try:
            env_weights = _clean_weights(json.loads(raw_weights))
        except json.JSONDecodeError as exc:
            log.warning("conversation_weights_invalid error=%s", str(exc)[:120])
            env_weights = {}
        if env_weights:
            weights.update(env_weights)
            sources.append(WEIGHTS_ENV)

    # 4. explicit thresholds
    for env_name in (
        REPLY_THRESHOLD_ENV,
        OPEN_REPLY_THRESHOLD_ENV,
        OBSERVE_THRESHOLD_ENV,
    ):
        raw = (os.environ.get(env_name) or "").strip()
        if not raw:
            continue
        parsed = _clean_threshold(raw)
        if parsed is None:
            continue
        if env_name == REPLY_THRESHOLD_ENV:
            reply_threshold = parsed
        elif env_name == OPEN_REPLY_THRESHOLD_ENV:
            open_reply_threshold = parsed
        else:
            observe_threshold = parsed
        sources.append(env_name)

    if not (weights or reply_threshold is not None
            or open_reply_threshold is not None
            or observe_threshold is not None):
        return ResolverOverrides()
    return ResolverOverrides(
        weights=weights,
        reply_threshold=reply_threshold,
        open_reply_threshold=open_reply_threshold,
        observe_threshold=observe_threshold,
        sources=tuple(sources),
    )


def clear_cache() -> None:
    """Testing/diagnostic helper: forget cached tuning-file contents."""
    _FILE_CACHE.clear()
