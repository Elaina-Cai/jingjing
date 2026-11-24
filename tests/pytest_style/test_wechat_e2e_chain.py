"""WeChat P1 纵切：观察入库、稳定去重、范围过滤、记忆提交与重启恢复。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from core.memory.service import MemoryService
from core.terminal.event_model import Event, EventType
from core.terminal.event_store_db import TerminalEventStore
from core.terminal.principal import (
    ChatType,
    PrincipalContext,
    RelationLevel,
    VisibilityScope,
)
from db.meta_models import FeedbackSample, MemoryProjectionRow, TerminalEvent
from db.repo_memory_projections import MemoryProjectionRepository


P = PrincipalContext(
    "wechat", "a", "same", ChatType.PRIVATE, "person-a", "A", RelationLevel.OWNER
)
G = PrincipalContext(
    "wechat", "a", "room@chatroom", ChatType.GROUP, "person-a", "A", RelationLevel.OWNER
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from core.rules import engine as rules
    from core.terminal import event_runtime, store_port
    import asyncio

    path = tmp_path / "wechat.db"
    engine = create_engine("sqlite:///" + str(path))
    TerminalEvent.__table__.create(engine)
    MemoryProjectionRow.__table__.create(engine)
    FeedbackSample.__table__.create(engine)
    repo = MemoryProjectionRepository(engine)
    store = TerminalEventStore(engine)
    monkeypatch.setattr(event_runtime, "default_event_store", lambda: store)
    monkeypatch.setattr(store_port, "get_meta_repo", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(rules, "decide", lambda ctx: SimpleNamespace(allowed=True))

    async def inline(func, *a, **kw):
        return func(*a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", inline)
    yield SimpleNamespace(path=path, engine=engine, repo=repo, store=store)
    engine.dispose()


def _message(msg_id, content, *, at=None, platform="wechat", account="a", chat="room@chatroom", scope=None):
    return Event.new(
        event_type=EventType.MESSAGE,
        platform=platform,
        bot_account_id=account,
        chat_id=chat,
        chat_type="group",
        content=content,
        external_msg_id=msg_id,
        occurred_at=at,
        visibility_scope=scope or frozenset({VisibilityScope.GROUP_MEMBERS}),
        metadata={"group_id": chat},
    )


def test_observation_duplicate_late_visibility_and_restart(db):
    first = db.store.append(_message("wx-1", "原始群消息", at=datetime(2026, 11, 8, 12, 0)))
    replay = db.store.append(_message("wx-1", "原始群消息", at=datetime(2026, 11, 8, 12, 0)))
    assert replay.event_id == first.event_id
    assert replay.sequence == first.sequence
    assert db.store.count() == 1

    late = db.store.append(_message("wx-late", "迟到的旧消息", at=datetime(2026, 11, 8, 9, 0)))
    assert late.sequence > first.sequence
    assert late.occurred_at == datetime(2026, 11, 8, 9, 0)

    db.store.append(
        _message(
            "qq-1",
            "另一个平台的群消息",
            platform="qq",
            account="b",
            chat="room@chatroom",
        )
    )
    visible = db.store.query_visible(G)
    assert [event.external_msg_id for event in visible] == ["wx-1", "wx-late"]

    path = db.path
    db.engine.dispose()
    reopened = create_engine("sqlite:///" + str(path))
    try:
        store = TerminalEventStore(reopened)
        stored = store.get_by_external_msg_id(
            platform="wechat", bot_account_id="a", chat_id="room@chatroom", external_msg_id="wx-late"
        )
        assert stored is not None and stored.sequence == late.sequence
        assert stored.occurred_at == datetime(2026, 11, 8, 9, 0)
    finally:
        reopened.dispose()


def test_memory_write_receipt_survives_restart(db):
    from core.terminal import event_runtime, store_port

    # The service resolves the event store and projection repository from the
    # process-level ports; the fixture points both at this temp database.
    ctx = SimpleNamespace(
        principal=P,
        extra={"platform": "wechat", "account_id": "a", "chat_id": "same", "is_group": False},
    )
    item = MemoryService().remember(
        ctx, "主人确认的新信息", memory_type="semantic", importance=0.7, source="wechat_e2e"
    )
    assert item is not None
    assert item.source_event_ids
    event_id = item.source_event_ids[0]
    assert db.store.get(event_id) is not None
    assert db.repo.get(item.memory_id).content == "主人确认的新信息"
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM terminal_events")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM memory_projections")).scalar() == 1

    path = db.path
    db.engine.dispose()
    reopened = create_engine("sqlite:///" + str(path))
    try:
        store = TerminalEventStore(reopened)
        repo = MemoryProjectionRepository(reopened)
        assert store.get(event_id) is not None
        assert repo.get(item.memory_id).content == "主人确认的新信息"
        assert repo.get(item.memory_id).source_event_ids == (event_id,)
    finally:
        reopened.dispose()


def test_user_correction_feedback_links_event_projection_and_restart(db):
    from core.memory.explicit_write import commit_explicit
    from core.memory.feedback_commit import claim_feedback
    from db.repo_feedback import FeedbackRepository

    feedback = FeedbackRepository(db.engine)
    sample = feedback.add(
        source="correction",
        platform="wechat",
        bot_account_id="a",
        chat_id="same",
        chat_type="private",
        person_id="person-a",
        sample_text="主人改口说不吃香菜",
        created_at=datetime(2026, 11, 8, 12, 0),
    )
    claim = claim_feedback(sample, feedback)
    event = Event.new(
        event_id=claim.event_id,
        occurred_at=claim.created_at,
        event_type=EventType.MEMORY_WRITE,
        platform="wechat",
        bot_account_id="a",
        chat_id="same",
        chat_type="private",
        actor_person_id="person-a",
        content="主人不吃香菜",
        visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
        metadata={
            "memory_type": "fact",
            "importance": 0.8,
            "source": "user_correction",
            "explicit_remember": True,
        },
    )
    item = commit_explicit(event, store=db.store, repository=db.repo, feedback=claim)
    assert item.source_event_ids == (claim.event_id,)
    stored = db.store.get(claim.event_id)
    assert stored is not None
    assert stored.metadata["feedback"]["sample_id"] == sample.id
    with db.engine.connect() as conn:
        assert conn.execute(
            text("SELECT status FROM feedback_samples WHERE id=:id"), {"id": sample.id}
        ).scalar() == "learned"

    path = db.path
    db.engine.dispose()
    reopened = create_engine("sqlite:///" + str(path))
    try:
        store = TerminalEventStore(reopened)
        repo = MemoryProjectionRepository(reopened)
        assert store.get(claim.event_id) is not None
        assert repo.get(item.memory_id).content == "主人不吃香菜"
    finally:
        reopened.dispose()
