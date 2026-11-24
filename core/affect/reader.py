"""Render affect state into a prompt block.

Pure text assembly, mirroring ``core.people.reader.render_text``: no IO beyond
the settings object it is handed, so the wording can be reviewed and tested on
its own.

The copy, the bands and their thresholds all live in
``configs/speech_seed/affect_default.json`` (plus an optional
``affect_<persona>.json`` override) -- see :mod:`core.affect.settings`.  What
stays here is the structure and the three rules behind it:

* a band whose text is empty renders nothing.  The baseline band must be
  silent, otherwise every turn pays for a feature that has nothing to say.
* Every block carries an explicit behavioural ceiling ("正经事秒切靠谱").
  Those limits belong in the block that creates the feeling: a prompt that
  says "you feel neglected" without saying "do not act out" is how a companion
  turns into a guilt trip.
* The *state* is never scaled by personality; only the expression is.  She
  really does feel it either way -- what the persona trait decides is how much
  of it reaches the surface, which is the difference between being hurt and
  making a scene.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from core.affect.settings import AffectCopy
from core.contracts.affect import AffectState
from safelog import warn

DEFAULT_COPY = AffectCopy()

# Shipped value, kept as a module-level name for callers and tests; the
# authoritative number lives in the settings file.
REASON_TTL = timedelta(hours=DEFAULT_COPY.reason_ttl_hours)


def parse_traits(raw: Any) -> dict[str, float]:
    """Read the persona's trait weights from JSON text or a mapping.

    Tolerant by design: this sits on the reply path, and a persona whose
    traits are malformed must degrade to the neutral default rather than
    break a message.
    """
    if isinstance(raw, Mapping):
        data: Any = raw
    elif isinstance(raw, str) and raw.strip():
        import json

        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            warn("core/affect/reader.py:53 (ValueError, TypeError)", err=repr(__import__("sys").exc_info()[1]))
            return {}
    else:
        return {}
    if not isinstance(data, Mapping):
        return {}

    out: dict[str, float] = {}
    for key, value in data.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            warn("core/affect/reader.py:64 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            continue
    return out


def expression_level(
    traits: Mapping[str, float] | None = None,
    *,
    copy: AffectCopy | None = None,
) -> float:
    """How freely she shows a negative feeling, in 0..1."""
    cfg = copy or DEFAULT_COPY
    if not traits:
        return cfg.expression_default
    try:
        value = float(traits.get(cfg.expression_trait, cfg.expression_default))
    except (TypeError, ValueError):
        warn("core/affect/reader.py:80 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return cfg.expression_default
    return max(0.0, min(1.0, value))


def band_of(level: float, *, copy: AffectCopy | None = None) -> str:
    """Project the numeric level onto a configured band name."""
    return (copy or DEFAULT_COPY).band_for(level)


def _band_allows_expression(band: str, cfg: AffectCopy) -> bool:
    for declared in cfg.bands:
        if declared.name == band:
            return bool(declared.expression)
    # A band the config never declared has no permission to show feeling.
    return False


def _expression_line(level: float, cfg: AffectCopy) -> str:
    """Personality note: only where the band allows it and the level says so."""
    if level >= cfg.expression_show_at:
        return cfg.expression_high
    if level <= cfg.expression_hide_at:
        return cfg.expression_low
    return ""


def render_block(
    state: AffectState,
    *,
    band: str | None = None,
    now: datetime | None = None,
    traits: Mapping[str, float] | None = None,
    copy: AffectCopy | None = None,
) -> str:
    """Return the block to append to a reply prompt, or ``""``.

    Whether a turn may carry affect at all is a **scope** decision made by the
    caller, not here: only turns addressed to the owner reach this function, so
    in a group the block appears for him and never for anyone else.  An earlier
    version vetoed groups outright, which would silently undo that decision.
    """
    cfg = copy or DEFAULT_COPY
    resolved = band or cfg.band_for(state.level)
    body = cfg.text_for(resolved)
    if not body:
        return ""

    lines = [body]
    if _band_allows_expression(resolved, cfg):
        hint = _expression_line(expression_level(traits, copy=cfg), cfg)
        if hint:
            lines.append(hint)
    reason = fresh_reason(state, now, cfg)
    if reason:
        lines.append(f"{cfg.reason_prefix}{reason}")
    lines.append(cfg.trailer)
    return "\n".join(lines)


def fresh_reason(
    state: AffectState,
    now: datetime | None,
    cfg: AffectCopy | None = None,
) -> str:
    """The stated cause as a sentence, but only while it is still recent.

    ``state.reason`` holds a key (``"wait_day"``), never the sentence itself,
    so reworded copy applies to states that were already stored.
    """
    if not state.reason:
        return ""
    resolved = cfg or DEFAULT_COPY
    if state.updated_at is not None and now is not None:
        ttl = timedelta(hours=resolved.reason_ttl_hours)
        if now - state.updated_at > ttl:
            return ""
    return resolved.reason_for(state.reason)
