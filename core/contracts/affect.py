"""Platform-neutral contracts for the companion's transient affect state.

Dependency-free, like the rest of ``core.contracts``: no DB, no LLM, no
platform code.

The point of this module is that "how she currently feels" is an explicit,
inspectable value instead of something the model improvises inside its own
prompt.  Message *content* is deliberately not an input: the axis below is
derived from observable behaviour (who spoke last, how long the gap was),
which is also why no wording from a user can talk her into a feeling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AffectSignals:
    """Observable facts about one turn.

    ``waited_minutes`` only carries meaning when ``replied_to_her`` is true:
    she spoke last, and this is how long she waited for an answer.
    """

    replied_to_her: bool = False
    waited_minutes: float = 0.0
    owner_initiated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "replied_to_her": self.replied_to_her,
            "waited_minutes": round(self.waited_minutes, 1),
            "owner_initiated": self.owner_initiated,
        }


@dataclass(frozen=True)
class AffectState:
    """One slow axis: how cared-for she currently feels.

    ``level`` runs -1.0 (被冷落) .. +1.0 (被重视), with 0.0 as the baseline it
    decays back to.  ``updated_at`` is when the level last *changed*; decay is
    computed from it, so a state nobody touches still fades on its own.
    """

    level: float = 0.0
    updated_at: datetime | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "level": round(self.level, 4),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AffectContext:
    """Result of an affect lookup, ready for prompt assembly.

    ``band`` is a plain string, not an enum: which bands exist, what they are
    called and where their thresholds sit is configuration, so this module
    cannot enumerate them.
    """

    state: AffectState
    band: str = "steady"
    prompt_block: str = ""
    # How visible the feeling is allowed to be, taken from the persona's
    # traits.  The state itself is never scaled by it.
    expression: float = 0.5
    # 第二轴「被当成一个人 vs 被当成工具」的 band 名；空 = 本轮无第二轴。
    personhood_band: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.to_dict(),
            "band": self.band,
            "has_block": bool(self.prompt_block),
            "expression": round(self.expression, 3),
            "personhood_band": self.personhood_band,
        }
