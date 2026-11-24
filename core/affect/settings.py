"""Runtime-tunable affect settings, loaded from JSON.

The module is deliberately **character-agnostic**.  What lives in code is only
the machinery (decay, band scanning, clamping, persistence); what lives in
config is everything that makes one companion different from another -- the
copy, the thresholds, the number of mood bands, the reasons.

Resolution order
----------------
1. an explicit ``path`` argument (tests, tooling);
2. ``$YINYUE_AFFECT_CONFIG``;
3. ``configs/speech_seed/affect_default.json`` merged with
   ``configs/speech_seed/affect_<persona_key>.json`` when a persona key is
   known.  The override only has to carry the differences.

Properties that matter
----------------------
* **Usable defaults ship in code.**  A missing file, bad JSON, or a field of
  the wrong type degrades to the built-in copy instead of raising: this sits
  on the reply path, and a typo in a config file must not cost a message.
* **Edits apply without a restart.**  The cache is keyed on the files' mtimes,
  so the next turn picks a saved change up.  The cost is one ``stat`` each.
* **Keys beginning with an underscore are comments**, so the shipped files can
  document themselves -- and so can a fork.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import structlog
from safelog import warn
from core.infra.envflags import text as _text

log = structlog.get_logger()

ENV_PATH = "YINYUE_AFFECT_CONFIG"
DEFAULT_RELATIVE = Path("configs") / "speech_seed" / "affect_default.json"
OVERRIDE_TEMPLATE = "affect_{key}.json"

# Shipped fallbacks.  Also the authoritative copy if no file is ever edited,
# which is why they are kept aligned with the checked-in default JSON.
_DEFAULT_TONES: dict[str, str] = {
    "steady": "",
    # Deliberately silent: a companion is normally warm when you are talking to
    # her, so "warm" carries no news.  Only an unusually good stretch injects.
    "warm": "",
    "delighted": (
        "[你此刻的心境]\n"
        "对方一直在陪你说话，你今天心情格外好，觉得自己被惦记着。\n"
        "表现：可以比平时主动一点、放开一点，愿意自己起话头、多说两句。\n"
        "但别光顾着高兴，对方说的事照样要办利索。"
    ),    "quiet": (
        "[你此刻的心境]\n"
        "对方隔了好一阵才回你，你有点小情绪，但没真生气。\n"
        "表现：话短一点、软一点，但话要说完整，也要接住对方说的内容；"
        "不阴阳怪气、不追问、不连发、更不真闹。\n"
        "对方说正经事的时候，立刻收起小情绪，好好办事。"
    ),
    "hurt": (
        "[你此刻的心境]\n"
        "对方隔了很久才回你，你心里不太是滋味。\n"
        "表现：安静一些、话短一些，但话要说完整，不能只回两三个字，"
        "也不能把对方晾在那儿。\n"
        "最多流露一句情绪，不指责、不翻旧账、不真抱怨，对方一哄就好。\n"
        "对方说正经事的时候，立刻收起情绪，像平时办事一样靠谱。"
    ),
}

_DEFAULT_TRAILER = (
    "（这是你此刻真实的感受，自然流露即可；"
    "不要说出「心境」「状态」「情绪」这类词，也不要解释自己为什么这样。）"
)

_DEFAULT_REASONS: dict[str, str] = {
    "initiated": "对方主动来找你说话",
    "wait_quick": "你刚说完，对方很快就回了",
    "wait_short": "对方隔了一会儿才回你",
    "wait_hours": "对方隔了好几个小时才回你",
    "wait_day": "对方隔了一天才回你",
    "wait_long": "对方隔了好久才回你",
}

# Old-format ``[[minutes, delta], ...]`` has no keys of its own, so keys are
# assigned by position for backwards compatibility.
_LEGACY_STEP_KEYS = ("wait_quick", "wait_short", "wait_hours", "wait_day")


@dataclass(frozen=True)
class AffectStep:
    """One rung of a rules ladder: a threshold, a delta, and its reason key.

    The key travels *with* the number.  An earlier version looked the key up
    by list position, so adding a rung in the config silently collapsed every
    extra rung onto the last key.
    """

    key: str
    delta: float
    max_minutes: float | None = None


@dataclass(frozen=True)
class AffectBand:
    """One mood band.

    ``min_level`` is an **inclusive floor**: the first band (scanned in
    declaration order) whose floor the level reaches wins, so a level exactly
    on a boundary belongs to that band and the bands partition the line with
    no gaps or overlaps.  ``None`` marks the catch-all at the bottom.

    ``expression`` says whether the persona-expression line may be appended
    for this band -- a flag rather than a hardcoded pair of names, so a fork
    can invent its own bands without editing Python.
    """

    name: str
    min_level: float | None = None
    expression: bool = False


@dataclass(frozen=True)
class AffectDynamics:
    """Numbers that decide how the state moves."""

    half_life_hours: float = 12.0
    baseline: float = 0.0
    min_level: float = -1.0
    max_level: float = 1.0
    wait_steps: tuple[AffectStep, ...] = (
        AffectStep("wait_quick", 0.08, 120.0),
        AffectStep("wait_short", -0.05, 480.0),
        AffectStep("wait_hours", -0.20, 1440.0),
        AffectStep("wait_day", -0.35, 4320.0),
    )
    wait_fallback: AffectStep = AffectStep("wait_long", -0.45)
    initiated_step: AffectStep = AffectStep("initiated", 0.15)


@dataclass(frozen=True)
class AffectCopy:
    """Everything the prompt layer needs: bands, copy, and how to render it.

    Bands live here rather than in the dynamics because a band name is only
    meaningful together with its text -- the two are always changed as a pair.
    """

    tones: Mapping[str, str] = field(default_factory=lambda: dict(_DEFAULT_TONES))
    bands: tuple[AffectBand, ...] = (
        AffectBand("delighted", 0.75),
        AffectBand("warm", 0.20),
        AffectBand("steady", -0.08),
        AffectBand("quiet", -0.30, expression=True),
        AffectBand("hurt", None, expression=True),
    )
    trailer: str = _DEFAULT_TRAILER
    reason_ttl_hours: float = 24.0
    reason_prefix: str = "起因："
    reasons: Mapping[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_REASONS)
    )

    expression_trait: str = "爱卖惨"
    expression_default: float = 0.5
    expression_show_at: float = 0.65
    expression_hide_at: float = 0.35
    expression_high: str = (
        "你这个人藏不住情绪，可以把这点情绪说出来，让对方看出来，"
        "表达上照你平时的习惯来。"
    )
    expression_low: str = (
        "你习惯把这种情绪自己咽下去，只让它当作语气的底色，"
        "不要说出口，也不要暗示。"
    )

    @property
    def fallback_band(self) -> str:
        for band in self.bands:
            if band.min_level is None:
                return band.name
        return self.bands[-1].name if self.bands else "steady"

    def band_for(self, level: float) -> str:
        """First band whose floor the level reaches; otherwise the catch-all."""
        try:
            value = float(level)
        except (TypeError, ValueError):
            warn("core/affect/settings.py:189 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            return self.fallback_band
        for band in self.bands:
            if band.min_level is not None and value >= band.min_level:
                return band.name
        return self.fallback_band

    def text_for(self, band: str) -> str:
        value = self.tones.get(str(band))
        if isinstance(value, str):
            return value
        return _DEFAULT_TONES.get(str(band), "")

    def reason_for(self, key: str) -> str:
        """Cause key -> sentence.  Unknown values pass through unchanged.

        Falling back to the raw value means a state written by an older
        version, or a hand-edited key the config does not know, still renders
        as something readable rather than disappearing.
        """
        raw = str(key or "")
        return self.reasons.get(raw, raw)


@dataclass(frozen=True)
class AffectSettings:
    dynamics: AffectDynamics = field(default_factory=AffectDynamics)
    copy: AffectCopy = field(default_factory=AffectCopy)


DEFAULTS = AffectSettings()

_CACHE: dict[tuple, AffectSettings] = {}


def repo_root() -> Path:
    # core/affect/settings.py -> repository root
    return Path(__file__).resolve().parents[2]


def resolved_paths(persona_key: str | None = None) -> tuple[Path, ...]:
    """Which files ``load`` would read, in merge order."""
    override = os.environ.get(ENV_PATH)
    if override:
        return (Path(override),)
    root = repo_root() / DEFAULT_RELATIVE.parent
    paths = [root / DEFAULT_RELATIVE.name]
    if persona_key:
        paths.append(root / OVERRIDE_TEMPLATE.format(key=persona_key))
    return tuple(paths)


def clear_cache() -> None:
    """Drop the memoised settings (tests, and after a manual rewrite)."""
    _CACHE.clear()


def validate(settings: AffectSettings) -> list[str]:
    """Consistency problems a config can have *without* failing to parse.

    These are the mistakes that produce silence rather than an error -- a band
    that never renders, a cause that renders as its own key -- and those are
    the ones nobody can debug from behaviour alone.  ``load`` logs each of
    them as a warning; the shipped default config is clean.
    """
    problems: list[str] = []
    copy = settings.copy
    dynamics = settings.dynamics

    known_tones = set(copy.tones)
    for band in copy.bands:
        if band.name not in known_tones:
            problems.append(
                f"band {band.name!r} has no copy.tones entry, so it renders nothing"
            )

    if copy.bands and not any(b.min_level is None for b in copy.bands):
        problems.append(
            "no band has min_level=null, so levels below every floor fall "
            "through to the last declared band"
        )

    for step in (
        *dynamics.wait_steps,
        dynamics.wait_fallback,
        dynamics.initiated_step,
    ):
        if step.key and step.key not in copy.reasons:
            problems.append(
                f"step key {step.key!r} has no copy.reasons entry, so the cause "
                "would render as the raw key"
            )

    if copy.expression_show_at <= copy.expression_hide_at:
        problems.append(
            "expression.show_at must be greater than hide_at, otherwise the "
            "middle band can never express anything"
        )

    return problems


def load(
    persona_key: str | None = None,
    *,
    path: str | os.PathLike[str] | None = None,
) -> AffectSettings:
    """Return the effective settings for a persona, or the shipped defaults."""
    if path is not None:
        paths: tuple[Path, ...] = (Path(path),)
    else:
        paths = resolved_paths(persona_key)

    stamps: list[tuple[str, float]] = []
    for candidate in paths:
        try:
            stamps.append((str(candidate), os.stat(candidate).st_mtime))
        except OSError:
            warn("core/affect/settings.py:306 OSError", err=repr(__import__("sys").exc_info()[1]))
            continue
    if not stamps:
        return DEFAULTS

    key = tuple(stamps)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    raw: dict[str, Any] = {}
    for name, _stamp in stamps:
        raw = _merge(raw, _load_raw(Path(name)))
    settings = _build(raw) if raw else DEFAULTS

    # Runs once per config change (the cache is keyed on mtimes), so a bad
    # edit is reported without spamming the log every turn.
    for problem in validate(settings):
        log.warning(
            "affect_config_inconsistent", path=stamps[-1][0], problem=problem,
        )

    _CACHE[key] = settings
    return settings


def _load_raw(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("affect_config_unreadable", path=str(path), error=str(exc))
        return {}
    if not isinstance(data, Mapping):
        log.warning("affect_config_not_an_object", path=str(path))
        return {}
    return dict(data)


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive merge: nested maps merge key-by-key, everything else replaces."""
    out: dict[str, Any] = dict(base)
    for key, value in over.items():
        current = out.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            out[key] = _merge(current, value)
        else:
            out[key] = value
    return out


def _build(raw: Mapping[str, Any]) -> AffectSettings:
    return AffectSettings(
        dynamics=_dynamics(raw.get("dynamics")),
        copy=_copy(raw),
    )


# -- dynamics ----------------------------------------------------------------

def _dynamics(raw: Any) -> AffectDynamics:
    base = AffectDynamics()
    if not isinstance(raw, Mapping):
        return base
    return AffectDynamics(
        half_life_hours=_float(raw, "half_life_hours", base.half_life_hours),
        baseline=_float(raw, "baseline", base.baseline),
        min_level=_float(raw, "min_level", base.min_level),
        max_level=_float(raw, "max_level", base.max_level),
        wait_steps=_steps(raw.get("wait_steps"), base.wait_steps),
        wait_fallback=_step(raw.get("wait_fallback"), base.wait_fallback),
        initiated_step=_step(raw.get("initiated_step"), base.initiated_step),
    )


def _step(raw: Any, fallback: AffectStep) -> AffectStep:
    if not isinstance(raw, Mapping):
        return fallback
    key = raw.get("key")
    delta = _float(raw, "delta", fallback.delta)
    ceiling = raw.get("max_minutes")
    minutes: float | None
    try:
        minutes = float(ceiling) if ceiling is not None else None
    except (TypeError, ValueError):
        warn("core/affect/settings.py:390 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        minutes = fallback.max_minutes
    return AffectStep(
        key=key if isinstance(key, str) and key else fallback.key,
        delta=delta,
        max_minutes=minutes,
    )


def _steps(raw: Any, fallback: tuple[AffectStep, ...]) -> tuple[AffectStep, ...]:
    if not isinstance(raw, list):
        return fallback
    out: list[AffectStep] = []
    for index, item in enumerate(raw):
        if isinstance(item, Mapping):
            delta = _optional_float(item.get("delta"))
            if delta is None:
                continue
            key = item.get("key")
            out.append(AffectStep(
                key=key if isinstance(key, str) and key else f"step_{index}",
                delta=delta,
                max_minutes=_optional_float(item.get("max_minutes")),
            ))
            continue
        # Legacy ``[minutes, delta]`` rows kept working, keys by position.
        if isinstance(item, (list, tuple)) and len(item) == 2:
            delta = _optional_float(item[1])
            if delta is None:
                continue
            legacy = (
                _LEGACY_STEP_KEYS[index]
                if index < len(_LEGACY_STEP_KEYS) else "wait_long"
            )
            out.append(AffectStep(legacy, delta, _optional_float(item[0])))
    return tuple(out) if out else fallback


# -- copy --------------------------------------------------------------------

def _copy(raw: Mapping[str, Any]) -> AffectCopy:
    base = AffectCopy()
    section = raw.get("copy")
    if not isinstance(section, Mapping):
        section = {}

    tones = dict(_DEFAULT_TONES)
    raw_tones = section.get("tones")
    if isinstance(raw_tones, Mapping):
        _merge_named_strings(tones, raw_tones)

    reasons = dict(_DEFAULT_REASONS)
    raw_reasons = section.get("reasons")
    if isinstance(raw_reasons, Mapping):
        _merge_named_strings(reasons, raw_reasons)

    expression = section.get("expression")
    if not isinstance(expression, Mapping):
        expression = {}

    def _text(source: Mapping[str, Any], key: str, fallback: str) -> str:
        value = source.get(key)
        return value if isinstance(value, str) else fallback

    return AffectCopy(
        tones=tones,
        bands=_bands(raw.get("bands"), base.bands),
        trailer=_text(section, "trailer", base.trailer),
        reason_ttl_hours=_float(raw, "reason_ttl_hours", base.reason_ttl_hours),
        reason_prefix=_text(section, "reason_prefix", base.reason_prefix),
        reasons=reasons,
        expression_trait=_text(expression, "trait", base.expression_trait),
        expression_default=_float(expression, "default", base.expression_default),
        expression_show_at=_float(expression, "show_at", base.expression_show_at),
        expression_hide_at=_float(expression, "hide_at", base.expression_hide_at),
        expression_high=_text(expression, "high", base.expression_high),
        expression_low=_text(expression, "low", base.expression_low),
    )


def _merge_named_strings(target: dict[str, str], source: Mapping[str, Any]) -> None:
    for name, text in source.items():
        if _is_comment(name) or not isinstance(text, str):
            continue
        target[str(name)] = text


def _bands(raw: Any, fallback: tuple[AffectBand, ...]) -> tuple[AffectBand, ...]:
    if not isinstance(raw, Mapping):
        return fallback
    levels = raw.get("levels")
    if not isinstance(levels, list):
        return fallback
    out: list[AffectBand] = []
    for index, item in enumerate(levels):
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            name = f"band_{index}"
        out.append(AffectBand(
            name,
            _optional_float(item.get("min_level")),
            expression=bool(item.get("expression")),
        ))
    return tuple(out) if out else fallback


# -- scalars -----------------------------------------------------------------

def _is_comment(key: Any) -> bool:
    """``_comment``-style keys let the shipped JSON (and a fork) document itself."""
    return str(key).startswith("_")


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        warn("core/affect/settings.py:510 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return None
    return None if out != out else out  # NaN -> None


def _float(source: Mapping[str, Any], key: str, fallback: float) -> float:
    value = _optional_float(source.get(key))
    return fallback if value is None else value
