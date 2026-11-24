from __future__ import annotations

from db.models import Conversation
from db.repository import RepositoryManager
from llm.client import LLMClient
# [去向量 2026-11-02 收尾] EmbeddingClient/VectorStore 随向量能力移除；
# 构造函数里两个死参数与幽灵注解也已清（见 MemoryManager.__init__）。
from safelog import warn


class MemoryManager:
    """Tiered memory helper used by conversational prompt assembly.

    Phase 5 Track C/E 收口后本类只保留两条仍在用的职责：

      - Recall Memory(回忆记忆): ``load_recall`` 返回当前会话的摘要与最近若干轮
        对话，用于拼当前对话上下文（不是跨会话召回）；
      - Archival Memory(归档记忆): ``retrieve_relevant`` 是给旧调用方的兼容入口，
        自动召回统一走 ``MemoryGateway``(terminal_events + memory_projections)，
        本类不再直读旧 ``memories`` 表 / 旧向量库。

    Core Memory(核心记忆) 的读取已经统一到 ``CoreMemoryProvider`` /
    ``MemoryService``，需要时请从那里取。
    """

    RECENT_TURNS = 12
    ARCHIVAL_TOP_K = 4

    def __init__(
        self,
        repo: RepositoryManager,
        llm: LLMClient,
    ):
        # [2026-11-02 去向量收尾] embedding/vector_store 两个参数随向量能力一起移除：
        # 赋了值却从来没人读，注解里的 EmbeddingClient/VectorStore 也成了幽灵名字。
        self.repo = repo
        self.llm = llm

        try:
            from core.config.settings import config

            self.ARCHIVAL_TOP_K = getattr(config, "archival_top_k", 4)
        except Exception as _:
            warn("agents/memory.py:44 Exception", err=repr(_))

    # ------------------------------------------------------------------ recall
    def load_recall(self, session_id: int) -> tuple[str, list[Conversation]]:
        """返回 (旧消息摘要, 最近对话列表). 模拟 Letta 的 FIFO 队列 + 压缩."""
        sess = self.repo.sessions.get(session_id)
        summary = (sess.summary if sess else "") or ""

        recent = list(reversed(self.repo.conversations.get_by_session(
            session_id, limit=self.RECENT_TURNS
        )))
        return summary, recent

    # ------------------------------------------------------------------ archival
    async def retrieve_relevant(
        self,
        user_message: str,
        persona_id: int,
        session_context: str = "",
        scope: dict | None = None,
        context=None,
    ) -> str:
        """Event-sourced automatic recall for legacy callers.

        Phase 5 D2 removes the old vector-store fallback: recall is always
        filtered by the deterministic PrincipalContext.  Callers without a
        context receive an empty string (never a broader legacy result).
        """
        if context is None:
            return ""
        from core.execution.data_channels import recall_for_context, render_hits

        hits = recall_for_context(
            context, user_message, limit=self.ARCHIVAL_TOP_K
        )
        if not hits:
            return ""
        return render_hits(hits, header="[相关历史记忆]")
