"""Infrastructure adapter: LLMClient -> core.people.ports.TextCompletionPort.

The core extractor never imports ``llm``.  Only this adapter knows how the
concrete LLM client is invoked.
"""

from __future__ import annotations

from typing import Any
from safelog import warn


class LlmClientTextCompletion:
    def __init__(self, llm: Any, *, max_tokens: int = 1200) -> None:
        self._llm = llm
        self._max_tokens = max(64, int(max_tokens or 1200))

    async def complete(
        self,
        *,
        system: str,
        user: str,
        agent_name: str = "",
    ) -> str:
        if self._llm is None:
            return ""
        try:
            return await self._llm.chat(
                system=system,
                user=user,
                agent_name=agent_name or "person_memory",
                temperature=0.1,
                max_tokens=self._max_tokens,
            )
        except Exception:
            warn("llm/person_observation.py:34 Exception", err=repr(__import__("sys").exc_info()[1]))
            return ""
