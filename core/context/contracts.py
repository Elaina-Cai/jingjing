"""Context block contracts (Phase 1).

A block is one rendered piece of the prompt (already including its header) plus
the metadata the assembler needs later for budgeting and tracing:

* ``priority`` -- 0 = never truncate (persona/safety), larger = dropped first;
* ``budget`` -- reserved for Phase 2; ``None`` means "no limit decided yet";
* ``source`` -- where the block came from, for the trace and for debugging
  ("she didn't see X" questions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContextBlock:
    name: str
    content: str
    source: str = ""
    priority: int = 50
    budget: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.content or "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "priority": self.priority,
            "budget": self.budget,
            "chars": self.chars,
        }


@dataclass(frozen=True)
class ContextBundle:
    blocks: tuple[ContextBlock, ...] = ()

    def render(self, separator: str = "\n\n") -> str:
        """Join blocks exactly like the legacy path did (``"\\n\\n"``)."""
        return separator.join(block.content for block in self.blocks if block.content)

    @property
    def total_chars(self) -> int:
        return sum(block.chars for block in self.blocks)

    def names(self) -> list[str]:
        return [block.name for block in self.blocks]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_chars": self.total_chars,
            "blocks": [block.as_dict() for block in self.blocks],
        }


__all__ = ["ContextBlock", "ContextBundle"]
