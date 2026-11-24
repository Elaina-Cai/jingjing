from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event as sqlalchemy_event, select
from sqlalchemy.orm import Session

from core.memory.explicit_write import commit_explicit
from core.memory.hub import MemoryHub, build_default_memory_hub
from core.memory.contracts import MemoryItem
from core.memory.recall_router import recall_items_for_context
from core.memory.service import MemoryService
from core.memory.terminal_explicit import TerminalExplicitMemoryProvider
from core.terminal import event_runtime, store_port
from core.terminal.event_model import Event, EventType
from core.terminal.event_store_db import TerminalEventStore
from core.terminal.principal import ChatType, PrincipalContext, RelationLevel, VisibilityScope
from db.meta_models import MemoryProjectionRow, TerminalEvent
from db.repo_memory_projections import MemoryProjectionRepository


def _install_freeze(engine):
    from core.memory.legacy_write_freeze import TRIGGERS

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE legacy_projection_write_owner("
            "singleton INTEGER PRIMARY KEY,source_id TEXT,scope_key TEXT,target_fingerprint TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO legacy_projection_write_owner VALUES(1,?,?,?)",
            ("test", json.dumps(["wechat", "acct", "chat-1"]), "target-fingerprint"),
        )
        for sql in TRIGGERS.values():
            conn.exec_driver_sql(sql)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "explicit-terminal.db"))
    TerminalEvent.__table__.create(engine)
    MemoryProjectionRow.__table__.create(engine)
    _install_freeze(engine)
    repo = MemoryProjectionRepository(engine)
    store = TerminalEventStore(engine)
    monkeypatch.setattr(event_runtime, "default_event_store", lambda: store)
    monkeypatch.setattr(store_port, "get_meta_repo", lambda: SimpleNamespace(engine=engine))
    from core.rules import engine as rules

    monkeypatch.setattr(rules, "decide", lambda ctx: SimpleNamespace(allowed=True))
    yield SimpleNamespace(engine=engine, repo=repo, store=store)
    engine.dispose()


def _owner(chat_id="chat-1"):
    return PrincipalContext(
        platform="wechat",
        bot_account_id="acct",
        chat_id=chat_id,
        chat_type=ChatType.PRIVATE,
        person_id="owner-1",
        display_name="Owner",
        relation_to_bot=RelationLevel.OWNER,
    )


def _group_member(chat_id="group-1", person_id="member-1"):
    return PrincipalContext(
        platform="wechat",
        bot_account_id="acct",
        chat_id=chat_id,
        chat_type=ChatType.GROUP,
        person_id=person_id,
        display_name="Member",
        relation_to_bot=RelationLevel.ORDINARY,
        group_id=chat_id,
    )


def _context(principal):
    chat_type = getattr(principal.chat_type, "value", principal.chat_type)
    extra = {
        "platform": principal.platform,
        "account_id": principal.bot_account_id,
        "chat_id": principal.chat_id,
        "chat_type": chat_type,
        "sender_id": principal.person_id,
    }
    if principal.is_group:
        extra.update({"is_group": True, "group_id": principal.group_id or principal.chat_id})
    return SimpleNamespace(principal=principal, extra=extra)


def _event(
    event_id,
    content,
    *,
    chat_id="chat-1",
    account_id="acct",
    actor_person_id="owner-1",
    chat_type="private",
    visibility=VisibilityScope.OWNER_ONLY,
):
    return Event.new(
        event_type=EventType.MEMORY_WRITE,
        event_id=event_id,
        occurred_at=datetime(2026, 11, 24, 12, 0),
        platform="wechat",
        bot_account_id=account_id,
        chat_id=chat_id,
        chat_type=chat_type,
        actor_person_id=actor_person_id,
        content=content,
        visibility_scope=frozenset({visibility}),
        metadata={
            "memory_type": "semantic",
            "importance": 0.8,
            "source": "test",
            "explicit_remember": True,
        },
    )


def _count(env, model):
    with Session(env.engine) as session:
        return len(session.execute(select(model)).all())


def test_frozen_projection_still_writes_one_idempotent_event(env):
    principal = _owner()
    item = MemoryService().remember(_context(principal), "主人不吃香菜", source="test")
    assert isinstance(item, MemoryItem)
    assert item.content == "主人不吃香菜"
    assert (item.platform, item.bot_account_id, item.chat_id) == ("wechat", "acct", "chat-1")
    assert item.visibility_scope == ("owner_only",)
    assert _count(env, TerminalEvent) == 1
    assert _count(env, MemoryProjectionRow) == 0

    stored = env.store.all()[0]
    receipt = {}
    duplicate = commit_explicit(stored, store=env.store, repository=env.repo, receipt=receipt)
    assert receipt["duplicate"] is True
    assert duplicate.memory_id == item.memory_id
    assert _count(env, TerminalEvent) == 1
    assert _count(env, MemoryProjectionRow) == 0


def test_terminal_provider_recalls_the_write_through_the_hub(env):
    principal = _owner()
    written = MemoryService().remember(_context(principal), "主人不吃香菜", source="test")
    assert written is not None
    provider = TerminalExplicitMemoryProvider(env.engine)
    hits = provider.recall(principal, "香菜", limit=5)
    assert [hit.memory_id for hit in hits] == [written.memory_id]
    assert hits[0].content == "主人不吃香菜"
    assert provider.recall(principal, "", limit=1)[0].memory_id == written.memory_id

    hub_hits = recall_items_for_context(
        _context(principal),
        "香菜",
        hub=MemoryHub([provider]),
    )
    assert [hit.memory_id for hit in hub_hits] == [written.memory_id]


def test_chat_and_account_isolation(env):
    commit_explicit(_event("same-chat", "同一句暗号"), store=env.store, repository=env.repo)
    commit_explicit(
        _event("other-chat", "同一句暗号", chat_id="chat-2"),
        store=env.store,
        repository=env.repo,
    )
    commit_explicit(
        _event("other-account", "同一句暗号", account_id="other-acct"),
        store=env.store,
        repository=env.repo,
    )
    hits = TerminalExplicitMemoryProvider(env.engine).recall(_owner(), "暗号")
    assert [hit.content for hit in hits] == ["同一句暗号"]
    assert hits[0].memory_id == "proj:same-chat"


def test_owner_only_and_group_member_visibility(env):
    commit_explicit(
        _event("owner-note", "只给主人看", visibility=VisibilityScope.OWNER_ONLY),
        store=env.store,
        repository=env.repo,
    )
    commit_explicit(
        _event(
            "group-note",
            "群里一起记住",
            chat_id="group-1",
            actor_person_id="member-1",
            chat_type="group",
            visibility=VisibilityScope.GROUP_MEMBERS,
        ),
        store=env.store,
        repository=env.repo,
    )
    provider = TerminalExplicitMemoryProvider(env.engine)
    nonowner_private = PrincipalContext(
        platform="wechat",
        bot_account_id="acct",
        chat_id="chat-1",
        chat_type=ChatType.PRIVATE,
        person_id="member-2",
        display_name="Member",
        relation_to_bot=RelationLevel.ORDINARY,
    )
    assert provider.recall(nonowner_private, "只给主人看") == []
    assert [hit.memory_id for hit in provider.recall(_owner(), "只给主人看")] == ["proj:owner-note"]
    assert [hit.memory_id for hit in provider.recall(_group_member(), "群里一起记住")] == ["proj:group-note"]

    private_outsider = PrincipalContext(
        platform="wechat",
        bot_account_id="acct",
        chat_id="group-1",
        chat_type=ChatType.PRIVATE,
        person_id="outsider-1",
        display_name="Outsider",
        relation_to_bot=RelationLevel.ORDINARY,
    )
    assert provider.recall(private_outsider, "群里一起记住") == []


def test_no_hit_returns_empty_and_sql_is_bounded(env):
    commit_explicit(_event("noise", "今天只是普通天气"), store=env.store, repository=env.repo)
    provider = TerminalExplicitMemoryProvider(env.engine)
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    sqlalchemy_event.listen(env.engine, "before_cursor_execute", capture)
    try:
        assert provider.recall(_owner(), "不存在的词") == []
    finally:
        sqlalchemy_event.remove(env.engine, "before_cursor_execute", capture)
    assert provider.SCAN_LIMIT == 500
    assert any("LIMIT" in statement.upper() for statement in statements)


def test_default_hub_registers_terminal_explicit_provider():
    hub = build_default_memory_hub()
    assert any(
        isinstance(provider, TerminalExplicitMemoryProvider)
        for provider in hub._providers.get("personal", ())
    )