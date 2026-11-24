# -*- coding: utf-8 -*-
"""loop remember：长期记忆是唯一事实写口，常驻旧快照不可绕过当前版本。"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from core.terminal import event_runtime, store_port
from core.terminal.event_store_db import TerminalEventStore
from db.meta_models import MemoryProjectionRow, TerminalEvent
from db.repo_memory_projections import MemoryProjectionRepository
from loop import brain, notes, ports


@pytest.fixture()
def loop_env(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "loop.db"))
    TerminalEvent.__table__.create(engine)
    MemoryProjectionRow.__table__.create(engine)
    store = TerminalEventStore(engine)
    repo = MemoryProjectionRepository(engine)
    monkeypatch.setattr(event_runtime, "default_event_store", lambda: store)
    monkeypatch.setattr(store_port, "get_meta_repo", lambda: SimpleNamespace(engine=engine))
    monkeypatch.setattr(brain.ledger, "blocked_targets", lambda: set())
    receipts = []

    def capture(**kw):
        receipts.append(kw)
        return dict(kw)

    monkeypatch.setattr(brain.ledger, "append_receipt", capture)
    monkeypatch.setattr(notes, "ROOT", str(tmp_path))
    monkeypatch.setattr(notes, "PATH", str(tmp_path / "notes.jsonl"))
    os.makedirs(notes.ROOT, exist_ok=True)
    notes._CACHE["mtime"] = 0.0
    notes._CACHE["rows"] = []
    notes._LEGACY["mtime"] = 0.0
    notes._LEGACY["rows"] = []
    yield SimpleNamespace(engine=engine, store=store, repo=repo, receipts=receipts)
    engine.dispose()


def _run(scope):
    token = ports._READ_SCOPE.set(scope)
    try:
        return asyncio.run(
            brain._exec(
                "remember",
                {"text": "主人不吃香菜", "kind": "fact"},
                db="unused.db",
                dry_run=False,
                turn_id="t1",
                focus="chat-1",
            )
        )
    finally:
        ports._READ_SCOPE.reset(token)


def test_loop_remember_writes_canonical_memory_without_second_fact_copy(loop_env):
    observation, receipt = _run(
        ports.ReadScope("wechat", "acct", "chat-1", "hello", "owner-1")
    )
    assert receipt["result"] == "ok", (observation, receipt)
    events = loop_env.store.all()
    assert len(events) == 1
    event = events[0]
    item = loop_env.repo.get("proj:" + event.event_id)
    assert item is not None
    assert item.content == "主人不吃香菜"
    assert item.metadata.get("source") == "loop_remember"
    assert item.visibility_scope == ("owner_only",)
    assert notes.all_rows() == []


def test_loop_remember_trusted_group_write_uses_group_visibility(loop_env):
    observation, receipt = _run(
        ports.ReadScope("wechat", "acct", "group:123", "hello", "stranger-1")
    )
    assert receipt["result"] == "ok", (observation, receipt)
    events = loop_env.store.all()
    assert len(events) == 1
    item = loop_env.repo.get("proj:" + events[0].event_id)
    assert item is not None and item.visibility_scope == ("group_members",)


def test_loop_remember_without_read_scope_fails_closed(loop_env):
    observation, receipt = _run(None)
    assert receipt["result"] == "failed"
    assert receipt.get("blocked_reason") == "missing_read_scope"
    assert observation.startswith("[没记成]")
    assert not loop_env.store.all()
    assert not notes.all_rows()


def test_notebook_failure_cannot_affect_canonical_write(loop_env, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("remember must not create a second fact copy")
    monkeypatch.setattr(brain.memo, "add", forbidden)
    _, receipt = _run(ports.ReadScope("wechat", "acct", "chat-1", "hello", "owner-1"))
    assert receipt["result"] == "ok"
    assert len(loop_env.store.all()) == 1
    assert notes.all_rows() == []


def test_withdrawn_memory_cannot_reappear_through_notebook_or_prompt(loop_env, monkeypatch):
    from datetime import datetime, timedelta
    from core.memory.hub import MemoryHub
    from core.memory.providers import PersonalMemoryProvider
    from core.memory.service import MemoryService
    from core.terminal import memory_gateway as mg
    from core.terminal.principal import PrincipalContext, ChatType, RelationLevel
    _run(ports.ReadScope("wechat", "acct", "chat-1", "hello", "owner-1"))
    event = loop_env.store.all()[0]
    mid = "proj:" + event.event_id
    # Old releases already wrote such snapshots. Leave their bytes intact, retire the read path.
    notes.add("fact", "主人不吃香菜", target="chat-1", extra={"memory_id": mid})
    gateway = mg.MemoryGateway(projection_repository=loop_env.repo, event_store=loop_env.store)
    monkeypatch.setattr(mg, "_trace_recall", lambda *a, **kw: None)
    monkeypatch.setattr(mg, "memory_gate_enabled", lambda: False)
    service = MemoryService(MemoryHub([PersonalMemoryProvider(gateway)]))
    monkeypatch.setattr("core.memory.service.MemoryService", lambda: service)
    principal = PrincipalContext("wechat", "acct", "chat-1", ChatType.PRIVATE,
                                 "owner-1", "Owner", RelationLevel.OWNER)
    monkeypatch.setattr(brain, "_loop_memory_context", lambda: SimpleNamespace(principal=principal, extra={}))
    assert "主人不吃香菜" in brain._canonical_memory_block()
    assert notes.note_block("chat-1", 500) == []
    assert brain.memo.resident("chat-1") == ""
    loop_env.repo.invalidate([mid], at=datetime.now() - timedelta(seconds=1))
    assert "主人不吃香菜" not in brain._canonical_memory_block()
    assert notes.note_block("chat-1", 500) == []
    # Scope changes cannot reactivate the cached private fact either.
    from dataclasses import replace
    principal = replace(principal, chat_id="other-group", chat_type=ChatType.GROUP)
    assert brain._canonical_memory_block() == ""


def test_canonical_reader_failure_never_falls_back_to_snapshot(loop_env, monkeypatch):
    notes.add("fact", "旧缓存里的敏感内容", target="chat-1", extra={"memory_id": "proj:old"})
    monkeypatch.setattr(brain, "_loop_memory_context", lambda: SimpleNamespace())
    class BrokenService:
        def search(self, *args, **kwargs):
            raise OSError("projection unavailable")
    monkeypatch.setattr("core.memory.service.MemoryService", BrokenService)
    assert brain._canonical_memory_block() == ""
    assert notes.note_block("chat-1", 500) == []

def test_loop_remember_failure_is_reported_in_plain_words(loop_env, monkeypatch):
    from core.memory.legacy_write_freeze import LegacyProjectionFrozen
    from core.memory import service as memory_service

    def boom(*args, **kwargs):
        raise LegacyProjectionFrozen("legacy_projection_writes_frozen_use_cognitive_commit")

    monkeypatch.setattr(memory_service.MemoryService, "remember", boom)
    observation, receipt = _run(ports.ReadScope("wechat", "acct", "chat-1", "hello", "owner-1"))
    assert receipt["result"] == "failed"
    assert "没有写进去" in observation
    assert "LegacyProjectionFrozen" not in observation
