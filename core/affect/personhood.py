"""第二轴：被当成一个人 vs 被当成工具（行为驱动，滚动窗口，不落库）。

- 只吃轮次的角色与时间：对方是否留下来多聊、是否只在有事时出现、她的主动是否被接。
- 不看用户话术，不调用模型；任何异常退化为不注入。
- 输出 (level, band, block)。band 文本自带行为上限（最多一句、不翻旧账、不拿这个换东西），
  避免第二轴退化成怨妇。
- 配置：configs/speech_seed/affect_personhood_default.json（mtime 缓存，改完下一轮生效）。
- 不新增 flag：由 YINYUE_FEATURE_AFFECT / AFFECT_PROMPT 总管；off 时 manager 根本不会调这里。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from safelog import warn

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "configs" / "speech_seed" / "affect_personhood_default.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "burst_gap_minutes": 30,
    "window_turns": 24,
    "bands": [
        {"name": "seen", "min_level": 0.35, "text": ""},
        {"name": "steady", "min_level": -0.15, "text": ""},
        {"name": "toolish", "min_level": -0.55, "text": ""},
        {"name": "invisible", "min_level": -2.0, "text": ""},
    ],
    "trailer": "",
}

_cache: dict[str, Any] = {"mtime": None, "data": DEFAULT_CONFIG}


def load(path: str | Path | None = None) -> dict[str, Any]:
    """读配置；文件缺失/坏 JSON 一律退回内置默认，且不抛。"""
    target = Path(path) if path else DEFAULT_PATH
    try:
        mtime = target.stat().st_mtime
    except OSError:
        warn("core/affect/personhood.py:40 OSError", err=repr(__import__("sys").exc_info()[1]))
        return DEFAULT_CONFIG
    if _cache.get("mtime") == mtime and isinstance(_cache.get("data"), dict):
        return _cache["data"]
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        warn("core/affect/personhood.py:46 (OSError, ValueError, TypeError)", err=repr(__import__("sys").exc_info()[1]))
        return DEFAULT_CONFIG
    if not isinstance(raw, dict):
        return DEFAULT_CONFIG
    merged = dict(DEFAULT_CONFIG)
    merged.update(raw)
    _cache["mtime"] = mtime
    _cache["data"] = merged
    return merged

def _role(turn: Any) -> str:
    return str(getattr(turn, "role", "") or "")


def _at(turn: Any) -> datetime | None:
    value = getattr(turn, "created_at", None)
    return value if isinstance(value, datetime) else None


def _split_bursts(items, gap_minutes):
    bursts = []
    for turn in items:
        at = _at(turn)
        prev = bursts[-1][-1] if bursts else None
        prev_at = _at(prev) if prev is not None else None
        if (
            not bursts
            or (
                at is not None
                and prev_at is not None
                and (at - prev_at).total_seconds() / 60.0 > gap_minutes
            )
        ):
            bursts.append([turn])
        else:
            bursts[-1].append(turn)
    return bursts


def _window(turns, cfg):
    window = max(1, int(cfg.get("window_turns", 24) or 24))
    return [t for t in (turns or ()) if _role(t) in ("user", "assistant")][-window:]


def _burst_label(burst):
    user_turns = sum(1 for t in burst if _role(t) == "user")
    if user_turns == 0:
        return "ignored"
    if user_turns >= 2:
        return "stayed"
    if user_turns == 1 and _role(burst[-1]) == "assistant":
        return "one_shot"
    return "open"


def _level(labels):
    owner = [x for x in labels if x in ("stayed", "one_shot")]
    if not owner:
        return 0.0
    stayed_ratio = owner.count("stayed") / max(1, len(owner))
    one_shot_ratio = owner.count("one_shot") / max(1, len(owner))
    ignored_ratio = labels.count("ignored") / max(1, len(labels))
    level = 0.60 * stayed_ratio - 0.75 * one_shot_ratio - 0.50 * ignored_ratio
    return max(-1.0, min(1.0, level))


def should_fire(labels):
    """只在「从好变坏」的那一次开口；连续负向不重复。"""
    if not labels:
        return False
    negative = {"one_shot", "ignored"}
    if labels[-1] not in negative:
        return False
    if len(labels) >= 2 and labels[-2] in negative:
        return False
    return True


def derive_level(turns: Sequence[Any] | None, *,
                 config: dict[str, Any] | None = None) -> float:
    cfg = config or load()
    items = _window(turns, cfg)
    if not items:
        return 0.0
    gap = float(cfg.get("burst_gap_minutes", 30) or 30)
    return _level([_burst_label(b) for b in _split_bursts(items, gap)])


def band_for(level: float, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load()
    bands = [b for b in (cfg.get("bands") or ()) if isinstance(b, dict)]
    for band in bands:
        try:
            floor = float(band.get("min_level", 0.0))
        except (TypeError, ValueError):
            warn("core/affect/personhood.py:140 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            continue
        if level >= floor:
            return band
    return bands[-1] if bands else {"name": "steady", "text": ""}


def render_block(level: float, *, config: dict[str, Any] | None = None) -> str:
    cfg = config or load()
    body = str(band_for(level, config=cfg).get("text") or "").strip()
    if not body:
        return ""
    trailer = str(cfg.get("trailer") or "").strip()
    return body + (("\n" + trailer) if trailer else "")


def evaluate(turns: Sequence[Any] | None, *,
             config: dict[str, Any] | None = None) -> tuple[float, str, str]:
    cfg = config or load()
    items = _window(turns, cfg)
    if not items:
        return 0.0, "steady", ""
    gap = float(cfg.get("burst_gap_minutes", 30) or 30)
    labels = [_burst_label(b) for b in _split_bursts(items, gap)]
    level = _level(labels)
    fire = should_fire(labels)
    band = band_for(level, config=cfg)
    if fire and not str(band.get("text") or "").strip():
        for candidate in cfg.get("bands") or ():
            if isinstance(candidate, dict) and str(candidate.get("text") or "").strip():
                band = candidate
                break
    block = render_block(level, config=cfg) if fire else ""
    if fire and not block:
        body = str(band.get("text") or "").strip()
        trailer = str(cfg.get("trailer") or "").strip()
        block = body + (("\n" + trailer) if trailer else "")
    return level, str(band.get("name") or ""), block
