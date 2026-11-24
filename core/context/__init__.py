"""Context assembly layer (Phase 1: equivalent refactor).

``contracts`` defines the block/bundle shape; ``assembler`` is the single entry
that turns the current inputs into the system prompt.  Phase 1 keeps the output
byte-identical to the legacy path (``build_persona_system_prompt``) while adding
block structure and a trace; Phase 2 will move ordering/budget ownership here.
"""

from core.context.contracts import ContextBlock, ContextBundle

__all__ = ["ContextBlock", "ContextBundle"]
