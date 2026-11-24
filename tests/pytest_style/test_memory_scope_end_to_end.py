"""P0 验收：私聊记忆不得经自动注入 / 搜索 / 按 ID 展开进入未授权群上下文。

四条路都走真实的 ``MemoryGateway`` + ``TerminalEventStore``（不替身过滤逻辑）：

1. 自动注入：``attach_to_context`` 的提示词块与索引快照；
2. 搜索：``search_knowledge(include_memory=True)`` / ``recall_memories``；
3. 按 ID 展开：``read_memory`` 的 key 与 memory_id 两种入参；
4. 坐标矛盾：同一 context 换成别的会话后，旧身份不得继续读、旧块不得复用。

夹具里同时放了本群、外群、私聊与思维链四类事件，断言的是「只出现本群那条」，
不是「恰好没出现私聊那条」——后者可能因为压根没召回而假绿。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agents.tools.memory_index import ReadMemoryTool
from agents.tools.memory import RecallMemoriesTool
from agents.tools.search import SearchKnowledgeTool
from core.memory.recall_router import recall_for_context
from core.execution.data_channels import recall_hub_items
from core.terminal.event_model import Event, EventType, Sensitivity
from core.terminal.event_store_db import TerminalEventStore
from core.terminal.identity_service import IdentityService
from core.terminal.memory_gateway import (
    MemoryGateway,
    install_memory_gateway,
    reset_memory_gateway,
)
from core.terminal.principal import (
    ChatType,
    GroupMembershipPeriod,
    RelationLevel,
    RelationshipState,
    VisibilityScope,
)
from db.meta_repo import MetaRepositoryManager
from db.migrations import run_migrations

W = "wechat"
UTC = timezone.utc
_PRIVATE = "私聊原文ALPHA：主人明天去医院复查，别外传"
_GROUP = "本群原文BETA：周末聚会定在周六下午"
_FOREIGN = "外群原文GAMMA：另一个群的内部安排"
_THOUGHT = "私聊思维链THOUGHT：永远不能进记忆"


def _dt(day: int = 1, hour: int = 10) -> datetime:
    return datetime(2026, 1, day, hour, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    """召回闸门关掉，只看权限；索引模式显式打开。"""
    monkeypatch.setenv("YINYUE_FEATURE_MEMORY_GATE", "0")
    monkeypatch.setenv("YINYUE_MEMORY_INDEX", "1")


@pytest.fixture()
def gateway_env(tmp_path):
    db_path = tmp_path / "memory.db"
    meta = MetaRepositoryManager(str(db_path))
    run_migrations(str(db_path))
    store = TerminalEventStore(meta.engine)
    try:
        _seed(store)
        yield MemoryGateway(event_store=store, identity_service=_identity())
    finally:
        meta.engine.dispose()


def _identity() -> IdentityService:
    return IdentityService(
        relationships={
            (W, "bot_a", "p_owner"): RelationshipState(
                W, "bot_a", "p_owner", RelationLevel.OWNER, 1.0, "seed"
            ),
            (W, "bot_a", "p_member"): RelationshipState(
                W, "bot_a", "p_member", RelationLevel.ORDINARY, 0.4, "seed"
            ),
        },
        person_identities={
            (W, "owner_raw"): "p_owner",
            (W, "member_raw"): "p_member",
        },
        group_memberships={
            (W, "g1@bot_a", "p_member"): [
                GroupMembershipPeriod(start_at=_dt(1) - timedelta(days=1))
            ],
        },
    )


def _seed(store: TerminalEventStore) -> None:
    store.append(Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=_dt(1, 9),
        platform=W,
        bot_account_id="bot_a",
        chat_id="owner_raw",
        chat_type="private",
        actor_person_id="p_owner",
        content=_PRIVATE,
        visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
        metadata={"sender_name": "主人", "group_id": ""},
    ))
    store.append(Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=_dt(1, 10),
        platform=W,
        bot_account_id="bot_a",
        chat_id="g1",
        chat_type="group",
        actor_person_id="p_member",
        content=_GROUP,
        visibility_scope=frozenset({VisibilityScope.GROUP_MEMBERS}),
        metadata={"sender_name": "群友", "group_id": "g1"},
    ))
    store.append(Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=_dt(1, 11),
        platform=W,
        bot_account_id="bot_a",
        chat_id="g2",
        chat_type="group",
        actor_person_id="p_member",
        content=_FOREIGN,
        visibility_scope=frozenset({VisibilityScope.GROUP_MEMBERS}),
        metadata={"sender_name": "群人", "group_id": "g2"},
    ))
    store.append(Event.new(
        event_type=EventType.THOUGHT,
        occurred_at=_dt(1, 12),
        platform=W,
        bot_account_id="bot_a",
        chat_id="owner_raw",
        chat_type="private",
        actor_person_id="p_owner",
        content=_THOUGHT,
        sensitivity=Sensitivity.OWNER_CHAIN_OF_THOUGHT,
        visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
        metadata={"group_id": ""},
    ))


def _principal(identity: IdentityService, *, raw_id: str, group: bool = False):
    return identity.resolve_principal(
        platform=W,
        bot_account_id="bot_a",
        chat_id="g1" if group else raw_id,
        chat_type="group" if group else "private",
        sender_id=raw_id,
        sender_name="群友" if group else "主人",
        occurred_at=_dt(2),
    )


def _group_context(identity: IdentityService, *, chat_id: str = "g1"):
    principal = _principal(identity, raw_id="member_raw", group=True)
    return SimpleNamespace(
        principal=principal,
        chat_id=chat_id,
        is_group=True,
        sender_name="群友",
        extra={
            "platform": W,
            "account_id": "bot_a",
            "chat_id": chat_id,
            "chat_type": "group",
            "group_id": chat_id,
            "is_group": True,
            "sender_id": "member_raw",
            "sender_name": "群友",
        },
    )


def _context_for(principal, *, chat_id: str, chat_type: str, sender_id: str):
    extra = {
        "platform": principal.platform,
        "account_id": principal.bot_account_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
    }
    if chat_type == "group":
        extra.update({"is_group": True, "group_id": chat_id})
    return SimpleNamespace(
        principal=principal,
        chat_id=chat_id,
        is_group=chat_type == "group",
        sender_name="群友",
        extra=extra,
    )


class _NoLegacyVectorStore:
    """旧 memories 向量通道一旦被打开就报错；知识库通道返回空。"""

    def __init__(self) -> None:
        self.collections: list[str] = []

    def search(self, *args, **kwargs):
        collection = str(kwargs.get("collection") or "")
        self.collections.append(collection)
        assert collection != "memories", "记忆不允许走旧向量库"
        return []


class _Embedding:
    async def embed(self, _text: str):
        return [0.1, 0.2, 0.3]


def _read_with(ctx, value: str):
    tool = ReadMemoryTool()
    tool.set_context(ctx)
    return asyncio.run(tool.execute(keys=value))


def test_auto_injection_only_carries_current_group_memory(gateway_env):
    gateway = gateway_env
    identity = gateway.identity_service
    ctx = _group_context(identity)

    block = gateway.attach_to_context(ctx, limit=6)

    assert _GROUP in block, block
    assert _PRIVATE not in block
    assert _FOREIGN not in block
    assert _THOUGHT not in block
    assert "memory_index" not in ctx.extra, "不得再往 extra 留未绑定身份的正文副本"

    from core.memory.index_scope import bound_index

    snapshot = bound_index(ctx)
    assert snapshot is not None
    entries = snapshot.entries_for(ctx)
    assert [entry["content"] for entry in entries] == [_GROUP]


def test_search_tools_cannot_reach_private_or_foreign_memory(gateway_env):
    gateway = gateway_env
    ctx = _group_context(gateway.identity_service)
    install_memory_gateway(gateway)
    try:
        recall = RecallMemoriesTool(SimpleNamespace(), _NoLegacyVectorStore(), _Embedding())
        recall.set_context(ctx)
        recalled = asyncio.run(recall.execute(query="医院复查", top_k=5))
        assert _PRIVATE not in recalled.output
        assert _FOREIGN not in recalled.output

        knowledge = SearchKnowledgeTool(_NoLegacyVectorStore(), _Embedding())
        knowledge.set_context(ctx)
        searched = asyncio.run(
            knowledge.execute(query="医院复查 聚会", top_k=5, include_memory=True)
        )
        assert _PRIVATE not in searched.output
        assert _FOREIGN not in searched.output
    finally:
        reset_memory_gateway()


def test_read_memory_by_key_and_memory_id_stays_in_scope(gateway_env):
    gateway = gateway_env
    identity = gateway.identity_service
    owner = _principal(identity, raw_id="owner_raw")
    # 先证明这条私聊记忆真实存在且主人读得到，避免「本来就没有」的假绿。
    owner_hits = gateway.recall(owner, limit=6)
    private_hit = next(hit for hit in owner_hits if _PRIVATE in hit.content)

    ctx = _group_context(identity)
    gateway.attach_to_context(ctx, limit=6)

    by_key = _read_with(ctx, "m1")
    assert _GROUP in by_key.output
    assert _PRIVATE not in by_key.output

    by_id = _read_with(ctx, private_hit.memory_id)
    assert by_id.error == "unknown_memory_key", by_id.error
    assert _PRIVATE not in by_id.output
    assert private_hit.memory_id not in (by_id.data or {}).get("memory_ids", [])


def test_changed_coordinates_cannot_reuse_old_block_or_recall(gateway_env, monkeypatch):
    gateway = gateway_env
    identity = gateway.identity_service
    ctx = _group_context(identity)
    assert _GROUP in gateway.attach_to_context(ctx, limit=6)

    # 同一个 context 被换成另一个群：重新解析身份，旧索引不得复用。
    ctx.extra["chat_id"] = "g2"
    ctx.extra["group_id"] = "g2"
    ctx.chat_id = "g2"

    assert gateway.attach_to_context(ctx, limit=6) == ""
    from core.memory.index_scope import bound_index

    snapshot = bound_index(ctx)
    assert snapshot is not None
    assert snapshot.entries_for(ctx) == [], "换会话后只能绑到新身份的空索引"
    assert _read_with(ctx, "m1").error == "no_memory_index"
    assert _GROUP not in _read_with(ctx, "m1").output

    # 不重新 attach、保留旧身份时，两个读入口都必须直接拒绝（连 hub 都不问）。
    stale = _group_context(identity)
    gateway.attach_to_context(stale, limit=6)
    stale.extra["chat_id"] = "g2"
    stale.extra["group_id"] = "g2"
    stale.chat_id = "g2"

    def _hub_must_not_run():
        raise AssertionError("坐标矛盾时不得进入记忆中心")

    monkeypatch.setattr("core.memory.hub.get_memory_hub", _hub_must_not_run)
    assert recall_for_context(stale, "聚会", limit=6) == []
    assert recall_hub_items(stale.principal, "聚会", context=stale) == []


def test_unchanged_coordinates_keep_group_memory_readable(gateway_env, monkeypatch):
    """正向对照：闸门只挡矛盾，不误伤正常的本群读取。"""
    gateway = gateway_env
    identity = gateway.identity_service
    ctx = _group_context(identity)
    gateway.attach_to_context(ctx, limit=6)

    # 网关层：本群那条读得到，私聊/外群读不到。
    hits = gateway.recall(ctx.principal, query="聚会", limit=6)
    assert any(_GROUP in hit.content for hit in hits)
    assert all(_PRIVATE not in hit.content for hit in hits)

    # 显式面：坐标一致时闸门放行，并且把同一个 context 交给记忆中心。
    calls = []

    class _Hub:
        def recall(self, principal, query, **kwargs):
            calls.append((principal, query, kwargs.get("context")))
            return ["hit"]

    monkeypatch.setattr("core.memory.hub.get_memory_hub", lambda: _Hub())
    assert recall_hub_items(ctx.principal, "聚会", context=ctx) == ["hit"]
    assert calls and calls[0][2] is ctx
