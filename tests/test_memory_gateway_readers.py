"""Phase 5 Track C: gateway routing for automatic memory readers.

Run directly:
    python tests/test_memory_gateway_readers.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# 数据库隔离（照 tests/test_event_chain_links.py / test_command_service_db.py）：
# 本文件会经 core.bootstrap.init_singletons() 触发 init_meta_db + run_migrations
# （bootstrap 兜底、runtime 访问器等），不隔离就把 pending 迁移落到**真实**
# meta / account 库。2026-09-29 实测：Migration 51 被本套测试落到了生产库
# （data/yinyue_meta.db + 4 个账号库，applied_at 14:58:19Z）。必须在 import core.*
# 之前设好：PathSettings 在 core.config.settings **首次**导入时就取值。
# ---------------------------------------------------------------------------
_ISO_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "_test_memory_gateway_readers_iso"
)
os.makedirs(_ISO_ROOT, exist_ok=True)
os.environ["META_DB_PATH"] = os.path.join(_ISO_ROOT, "meta.db")
os.environ["PLATFORMS_DIR"] = os.path.join(_ISO_ROOT, "platforms")
os.environ["DATA_DIR"] = os.path.join(_ISO_ROOT, "data")

from core.config.settings import config as _config  # noqa: E402

# 守卫：env 只在 core.config.settings 首次导入前设好才有效，否则真实路径已经
# 固化进 config。这里硬失败，绝不静默把迁移/写操作落到生产库。
assert os.path.realpath(_config.paths.meta_db_path) != os.path.realpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "yinyue_meta.db"
    )
), f"测试数据库隔离失效：meta_db_path={_config.paths.meta_db_path}"


from core import features  # noqa: E402
from core.memory.recall_router import recall_for_context  # noqa: E402
from core.terminal.memory_gateway import (  # noqa: E402
    MemoryHit,
    install_memory_gateway,
    reset_memory_gateway,
)
from core.terminal.principal import (  # noqa: E402
    ChatType,
    PrincipalContext,
    RelationLevel,
)
from agents.memory import MemoryManager  # noqa: E402
from agents.tools.memory import RecallMemoriesTool  # noqa: E402
from agents.tools.search import SearchKnowledgeTool  # noqa: E402

UTC = timezone.utc


class _FakeGateway:
    def __init__(self) -> None:
        self.calls = []

    def recall(self, principal, *, query="", limit=6, **_kwargs):
        self.calls.append(
            {
                "person_id": getattr(principal, "person_id", ""),
                "query": query,
                "limit": limit,
            }
        )
        return [
            MemoryHit(
                memory_id="event:1",
                content="事件流里的长期记忆",
                occurred_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
                event_type="message",
                speaker="P6",
                platform="wechat",
                bot_account_id="bot_a",
                source_event_ids=("evt_1",),
                score=0.91,
            )
        ]


class _ExplodingVectorStore:
    def __init__(self) -> None:
        self.collections: list[str] = []

    def search(self, *args, **kwargs) -> list:
        collection = str(kwargs.get("collection") or "")
        self.collections.append(collection)
        if collection == "memories":
            raise AssertionError("legacy memories vector search must not run")
        return []


class _Embedding:
    async def embed(self, _text: str):
        return [0.1, 0.2, 0.3]


def _owner_principal() -> PrincipalContext:
    return PrincipalContext(
        platform="wechat",
        bot_account_id="bot_a",
        chat_id="u1",
        chat_type=ChatType.PRIVATE,
        person_id="p_owner",
        display_name="老板",
        relation_to_bot=RelationLevel.OWNER,
        capabilities=frozenset(),
        memory_scopes=frozenset(),
    )


def _context(principal: PrincipalContext):
    """带真实坐标的上下文：读记忆要求上下文坐标与已附身份一致。"""
    return SimpleNamespace(
        principal=principal,
        chat_id=principal.chat_id,
        is_group=principal.chat_type is ChatType.GROUP,
        extra={
            "platform": principal.platform,
            "account_id": principal.bot_account_id,
            "chat_id": principal.chat_id,
            "chat_type": principal.chat_type.value,
        },
    )


def _with_gateway(test) -> None:
    gateway = _FakeGateway()
    install_memory_gateway(gateway)
    try:
        test(gateway)
    finally:
        reset_memory_gateway()


def test_recall_memories_tool_uses_gateway() -> None:
    def run(gateway: _FakeGateway) -> None:
        vector_store = _ExplodingVectorStore()
        tool = RecallMemoriesTool(SimpleNamespace(), vector_store, _Embedding())
        tool.set_context(_context(_owner_principal()))
        result = asyncio.run(tool.execute(query="还记得什么", top_k=3))
        assert not result.is_error
        assert "事件流里的长期记忆" in result.output
        assert result.data and result.data[0]["source_event_ids"] == ["evt_1"]
        assert vector_store.collections == []
        assert gateway.calls and gateway.calls[0]["person_id"] == "p_owner"

    _with_gateway(run)


def test_search_knowledge_include_memory_uses_gateway() -> None:
    def run(gateway: _FakeGateway) -> None:
        vector_store = _ExplodingVectorStore()
        tool = SearchKnowledgeTool(vector_store, _Embedding())
        tool.set_context(_context(_owner_principal()))
        result = asyncio.run(
            tool.execute(query="结合记忆查一下", top_k=3, include_memory=True)
        )
        assert not result.is_error
        assert "事件流里的长期记忆" in result.output
        assert "memories" not in vector_store.collections
        assert "persona_knowledge" in vector_store.collections
        assert gateway.calls

    _with_gateway(run)


def test_memory_manager_retrieve_relevant_uses_gateway() -> None:
    def run(gateway: _FakeGateway) -> None:
        manager = MemoryManager(
            repo=SimpleNamespace(),
            llm=SimpleNamespace(),
                    )
        output = asyncio.run(
            manager.retrieve_relevant(
                "之前聊过什么",
                1,
                context=_context(_owner_principal()),
            )
        )
        assert "事件流里的长期记忆" in output
        assert gateway.calls

    _with_gateway(run)


def test_gateway_enabled_without_context_reads_no_legacy_store() -> None:
    def run(gateway: _FakeGateway) -> None:
        vector_store = _ExplodingVectorStore()
        tool = RecallMemoriesTool(SimpleNamespace(), vector_store, _Embedding())
        # No ctx / principal supplied.  The gateway path must stay bounded and
        # must not touch the legacy vector store.
        tool.set_context(None)
        result = asyncio.run(tool.execute(query="还记得什么", top_k=3))
        assert not result.is_error
        assert vector_store.collections == []
        # No context means no principal: the router returns no hits directly
        # instead of falling back to the legacy vector store.
        assert gateway.calls == []

    _with_gateway(run)


def test_router_returns_empty_without_context() -> None:
    assert recall_for_context(None, "x") == []


def main() -> int:
    tests = [
        test_recall_memories_tool_uses_gateway,
        test_search_knowledge_include_memory_uses_gateway,
        test_memory_manager_retrieve_relevant_uses_gateway,
        test_gateway_enabled_without_context_reads_no_legacy_store,
        test_router_returns_empty_without_context,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL {test.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
