"""Read/update the transient affect axis for one conversation partner.

This is the only module in ``core.affect`` that touches a repository.  It
follows the convention already used by ``core.speech.relationship``: the repo
is injected by the caller and ``db`` is imported lazily, so ``core`` keeps
working in tests that never open a database.

Where the state lives
---------------------
On the existing ``relationships`` row, in dedicated columns.  The distinction
that matters:

* ``intimacy`` answers *what are we to each other*.  It is slow and
  near-static on purpose, which is why the owner being pinned at 1.0 is
  correct rather than a bug.
* ``warmth`` answers *how do I feel right now*.  It moves every turn and
  decays back to zero on its own.

They share a row only so a lookup stays a single primary-key read; they are
never written by the same code path, and ``RelationshipRepository.upsert``
(structural) cannot clobber ``warmth``.

Which config applies
--------------------
The settings are resolved per persona: ``affect_default.json`` merged with
``affect_<persona key>.json`` when the caller knows the key.  Nothing about a
particular character is compiled into this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

import structlog

from core.contracts.affect import AffectContext, AffectSignals, AffectState
from core.affect import engine
from core.affect import settings as affect_settings
from core.affect.reader import expression_level, parse_traits, render_block
from core import features

log = structlog.get_logger()


class AffectManager:
    """Owns the lifetime of one person's affect state."""

    def __init__(self, persona_id: int = 1, *, persona_key: str = "") -> None:
        self.persona_id = persona_id
        self.persona_key = persona_key

    def settings(self, persona_key: str = ""):
        """Effective settings, re-resolved each call (mtime-cached inside)."""
        return affect_settings.load(persona_key or self.persona_key or None)

    # -- reads ---------------------------------------------------------------

    def read(self, repo, target_wxid: str) -> AffectState:
        """Load the stored axis, or a baseline state when there is no row."""
        if not target_wxid:
            return AffectState()
        try:
            row = repo.relationships.get(
                persona_id=self.persona_id, target_wxid=target_wxid,
            )
        except Exception as exc:  # noqa: BLE001 - never break a reply path
            log.warning("affect_read_failed", target=target_wxid, error=str(exc))
            return AffectState()
        if row is None:
            return AffectState()
        return AffectState(
            level=float(getattr(row, "warmth", 0.0) or 0.0),
            updated_at=getattr(row, "warmth_updated_at", None),
            reason=str(getattr(row, "warmth_reason", "") or ""),
        )

    # -- writes --------------------------------------------------------------

    def persist(
        self, repo, target_wxid: str, state: AffectState, now: datetime,
    ) -> bool:
        """Store the new level.  Returns False when nothing could be written."""
        setter = getattr(repo.relationships, "set_affect", None)
        if setter is None:
            # Older/meta repository without the affect columns.  Refusing to
            # write is the safe outcome: a state that cannot be remembered
            # would reset every turn and make her react to a single silence
            # as if it were the whole story.
            log.warning("affect_persist_unsupported", target=target_wxid)
            return False
        try:
            return bool(setter(
                persona_id=self.persona_id,
                target_wxid=target_wxid,
                warmth=state.level,
                reason=state.reason,
                at=now,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning("affect_persist_failed", target=target_wxid, error=str(exc))
            return False

    # -- the one call the reply path needs -----------------------------------

    def build(
        self,
        repo,
        *,
        target_wxid: str,
        signals: AffectSignals,
        now: datetime,
        traits: Any = None,
        persona_key: str = "",
        session_id: Any = None,
        turns: Sequence[Any] | None = None,
    ) -> AffectContext:
        """Decay, apply this turn's signal, persist, and render.

        ``traits`` is the persona's trait weights, either a mapping or raw
        JSON text.  It shapes only how much of the feeling is allowed into the
        prompt; the stored level is identical either way.

        Returns an empty context (no prompt block) whenever the state could
        not be persisted.  That keeps the invariant that matters: a deployment
        where affect cannot be stored behaves exactly like one where the
        feature is off, instead of behaving *differently* every turn.
        """
        previous = self.read(repo, target_wxid)
        if not target_wxid:
            # No conversation partner means no relationship to feel anything
            # about.  Returning here keeps a stray empty id from being written
            # to whichever row a repository happens to match.
            return AffectContext(state=previous)

        # Re-resolved every turn: the loader is mtime-cached, so a saved edit
        # to a settings file applies on the next reply without a restart.
        config = self.settings(persona_key)
        current = engine.observe(
            previous, signals, now, dynamics=config.dynamics,
        )

        if not self.persist(repo, target_wxid, current, now):
            return AffectContext(state=previous)

        band = config.copy.band_for(current.level)
        weights = parse_traits(traits)
        expression = expression_level(weights, copy=config.copy)
        block = render_block(
            current, band=band, now=now,
            traits=weights, copy=config.copy,
        )
        personhood_level = 0.0
        personhood_band = ""
        personhood_block = ""
        if turns:
            try:
                from core.affect import personhood

                personhood_level, personhood_band, personhood_block = (
                    personhood.evaluate(turns)
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("personhood_eval_failed", error=str(exc)[:120])
        if personhood_block:
            block = (
                block + "\n" + personhood_block
                if block else "[你此刻的心境]\n" + personhood_block
            )
        log.info(
            "affect_observed",
            target=target_wxid,
            session_id=session_id,
            affect_level=round(current.level, 3),
            band=band,
            expression=round(expression, 3),
            # ``rendered`` and ``injected`` are deliberately separate: the
            # caller decides whether the block reaches the prompt
            # (YINYUE_FEATURE_AFFECT_PROMPT), so a shadow run must not read as
            # if she had been affected.  The text itself is not logged -- the
            # analyser re-renders it from (band, expression, reason) with the
            # same code.
            rendered=bool(block),
            injected=bool(block) and features.enabled(features.AFFECT_PROMPT),
            block_chars=len(block),
            # The reason *key*, not the sentence: with level+band+expression+
            # reason the analyser can re-render the exact block offline.
            reason=current.reason,
            signals=signals.to_dict(),
            personhood_level=round(personhood_level, 3),
            personhood_band=personhood_band,
            personhood_rendered=bool(personhood_block),
        )
        return AffectContext(
            state=current, band=band, prompt_block=block,
            expression=expression, personhood_band=personhood_band,
        )
