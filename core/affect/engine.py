"""Pure affect dynamics: observable signals in, decaying state out.

Everything here is a deterministic function of its arguments.  No IO, no
clock of its own (``now`` is always passed in), no model call, no config
file read -- the tunable numbers arrive as an :class:`AffectDynamics`.  That
is what makes the state replayable, unit-testable, and impossible for a user
to talk her into a feeling she did not earn.

The single axis implemented here is "how cared-for she currently feels":

* a turn where she waited a long time for an answer pushes the level down;
* a turn where the other person came to her unprompted pushes it up;
* between turns the level decays exponentially back to the baseline.

Adding a second axis later means adding a second ``level``, not rewriting
this file: the decay and clamp rules are axis-agnostic.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Iterable, Sequence

from core.affect.settings import AffectDynamics
from core.contracts.affect import AffectSignals, AffectState

DEFAULT_DYNAMICS = AffectDynamics()

# The shipped numbers, kept as module-level names for readability and tests.
# The authoritative values live in the settings file; these mirror its defaults.
MIN_LEVEL = DEFAULT_DYNAMICS.min_level
MAX_LEVEL = DEFAULT_DYNAMICS.max_level
BASELINE = DEFAULT_DYNAMICS.baseline
HALF_LIFE_HOURS = DEFAULT_DYNAMICS.half_life_hours

# ``AffectState.reason`` holds a reason *key*, not a sentence: the reader turns
# it into text, so a stored state stays meaningful when somebody rewords the
# copy or translates it.  Each key travels with its own step in the settings,
# so adding a rung to the ladder can never silently collapse onto another key.


def clamp(
    level: float,
    low: float = MIN_LEVEL,
    high: float = MAX_LEVEL,
) -> float:
    return max(low, min(high, float(level)))


def decay(
    state: AffectState,
    now: datetime,
    *,
    dynamics: AffectDynamics = DEFAULT_DYNAMICS,
) -> AffectState:
    """Fade the level toward the baseline according to real elapsed time.

    Decay is computed from the wall clock, never from a message count: this
    deployment is asynchronous, and three days of silence has to cost the
    same whether or not anything was said in between.
    """
    if state.updated_at is None:
        return replace(
            state,
            updated_at=now,
            level=clamp(state.level, dynamics.min_level, dynamics.max_level),
        )
    hours = (now - state.updated_at).total_seconds() / 3600.0
    if hours <= 0:
        return state
    # Exponential approach to the baseline, expressed as a half-life so the
    # constant stays human-readable.
    factor = 0.5 ** (hours / max(dynamics.half_life_hours, 1e-6))
    level = dynamics.baseline + (state.level - dynamics.baseline) * factor
    if abs(level - dynamics.baseline) < 1e-4:
        level = dynamics.baseline
    return replace(
        state, level=clamp(level, dynamics.min_level, dynamics.max_level),
        updated_at=now,
    )


def assess(
    signals: AffectSignals,
    *,
    dynamics: AffectDynamics = DEFAULT_DYNAMICS,
) -> tuple[float, str]:
    """Turn one turn's observable facts into a delta plus a reason key."""
    if signals.owner_initiated:
        step = dynamics.initiated_step
        return step.delta, step.key

    if not signals.replied_to_her:
        return 0.0, ""

    waited = max(0.0, float(signals.waited_minutes))
    for step in dynamics.wait_steps:
        if step.max_minutes is None:
            continue
        if waited <= step.max_minutes:
            return step.delta, step.key
    return dynamics.wait_fallback.delta, dynamics.wait_fallback.key


def observe(
    state: AffectState,
    signals: AffectSignals,
    now: datetime,
    *,
    dynamics: AffectDynamics = DEFAULT_DYNAMICS,
) -> AffectState:
    """Decay to ``now``, then apply this turn's signal.

    Order matters: decay first so a long silence that started before this
    turn is already accounted for, then the new delta on top.  A turn with no
    signal still advances ``updated_at`` so the decay clock stays honest.
    """
    faded = decay(state, now, dynamics=dynamics)
    delta, reason = assess(signals, dynamics=dynamics)
    if delta == 0.0:
        return faded

    level = clamp(
        faded.level + delta, dynamics.min_level, dynamics.max_level,
    )

    # The stored cause has to explain the level it is attached to.  A quick
    # reply after a three-day silence is a *positive* delta landing on a
    # still-negative level; citing it would print "你有点失落，起因：主人很快
    # 就回了" in one block.  So a cause is only adopted once the event that
    # produced it actually dominates the state; otherwise the older cause --
    # which is what the level is still made of -- is kept.
    if level * delta > 0:
        written_reason = reason
    else:
        written_reason = faded.reason or reason

    return AffectState(level=level, updated_at=now, reason=written_reason)


def derive_signals(
    turns: Sequence[Any] | Iterable[Any],
    now: datetime,
    *,
    in_group: bool = False,
) -> AffectSignals:
    """Infer this turn's signal from the history that preceded it.

    ``turns`` is the conversation *before* the current message, oldest first.
    Each item only needs ``.role`` (``"user"`` / ``"assistant"``) and
    ``.created_at``; anything else is ignored, which keeps ORM types out of
    this layer.

    In a one-to-one chat only two situations carry a signal:

    * the last turn was hers -> the other person is answering her, and the
      gap is how long she waited;
    * the last turn was theirs -> they are driving the conversation, so she
      was sought out rather than left hanging.

    An empty history says nothing, so a first contact never moves the level.

    ``in_group`` changes what silence *means*.  In a group, "she spoke and
    nobody answered" is simply what groups are like, not a signal about the
    relationship -- so the wait ladder is skipped entirely and the only thing
    that counts is that the other person came to her.  Callers only reach this
    for turns addressed to her, so that is exactly what happened.
    """
    if in_group:
        return AffectSignals(owner_initiated=True)

    history = list(turns)
    if not history:
        return AffectSignals()

    last = history[-1]
    if getattr(last, "role", "") != "assistant":
        return AffectSignals(owner_initiated=True)

    spoken_at = getattr(last, "created_at", None)
    if not isinstance(spoken_at, datetime):
        return AffectSignals(replied_to_her=True, waited_minutes=0.0)

    waited = (now - spoken_at).total_seconds() / 60.0
    return AffectSignals(
        replied_to_her=True,
        waited_minutes=max(0.0, waited),
    )
