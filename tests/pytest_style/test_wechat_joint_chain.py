"""Joint WeChat loop/feedback path; only model and account-history IO are substituted."""
from datetime import datetime
from types import SimpleNamespace
import json
import asyncio

import pytest
from sqlalchemy import create_engine, text
from core.terminal.event_model import Event, EventType
from core.terminal.event_store_db import TerminalEventStore
from core.terminal.principal import VisibilityScope
from core.terminal import shadow
from core.quality import feedback, corrections, feedback_lease
from db.repo_feedback import FeedbackRepository
from db.repo_memory_projections import MemoryProjectionRepository
from test_wechat_e2e_chain import db
from test_appraisal_prompt_integration import runtime, Model, drive


@pytest.mark.parametrize("failure", ["none", "model_error", "cancel"])
def test_observe_real_loop_collect_consume_reopen_and_replay(runtime, db, monkeypatch, failure):
    brain, tmp = runtime
    account = tmp / "platforms/wechat/a"
    account.mkdir(parents=True, exist_ok=True)
    native = Event.new(event_type=EventType.MESSAGE, external_msg_id="wx-original", platform="wechat",
        bot_account_id="a", chat_id="same", chat_type="private", actor_person_id="person-a",
        content="请记住我不吃香菜", visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}))
    inbound = db.store.append(native)
    assert db.store.append(native).event_id == inbound.event_id
    monkeypatch.setattr(shadow, "get_default_store", lambda: db.store)
    llm = Model([{"tool_calls": [{"id":"remember", "name":"remember", "arguments":{"text":"我不吃香菜", "kind":"fact"}}]},
                 {"tool_calls": [], "content":""}])
    receipts = drive(brain.run_loop(str(account / "account.db"), inbound.content, llm=llm, dry_run=False,
        platform="wechat", chat_id="same", sender_id="person-a", focus="same",
        visible_event_ids=(inbound.event_id,), max_steps=2))
    saved = next(r for r in receipts if r.get("act") == "remember")
    assert saved["result"] == "ok"
    assert len(llm.prompts) == 2
    writes = db.store.query(event_types=(EventType.MEMORY_WRITE,))
    assert len(writes) == 1
    # The account-history IO boundary exposes exactly the observed records.
    incoming = {"chat_id":"same", "chat_type":"private", "msg_id":"wx-correct", "sender_id":"person-a",
                "content":"你记错了，我现在吃香菜"}
    correction_event = db.store.append(Event.new(event_type=EventType.MESSAGE, external_msg_id="wx-correct",
        platform="wechat", bot_account_id="a", chat_id="same", chat_type="private", actor_person_id="person-a",
        content=incoming["content"], visibility_scope=frozenset({VisibilityScope.OWNER_ONLY})))
    history = SimpleNamespace(messages=SimpleNamespace(get_by_chat=lambda *a, **k: [incoming]))
    repository = FeedbackRepository(db.engine)
    meta = SimpleNamespace(memory_projections=db.repo, feedback_samples=repository)
    assert feedback.record_feedback_signals([incoming], repo=history, meta_repo=meta, platform="wechat", account_id="a") == 1
    assert feedback.record_feedback_signals([incoming], repo=history, meta_repo=meta, platform="wechat", account_id="a") == 0
    sample = repository.list_pending(limit=10, source="correction")[0]
    assert sample.sample_text == correction_event.content
    assert (sample.platform, sample.bot_account_id, sample.chat_id, sample.person_id) == ("wechat", "a", "same", "person-a")
    monkeypatch.setattr(corrections, "_owner_ids", lambda platform: {"person-a"})
    corrections.reset_rate_for_tests()
    feedback_lease.ensure_schema(db.engine)
    class Extractor:
        calls = 0
        async def chat(self, **kwargs):
            self.calls += 1
            if failure == "cancel":
                raise asyncio.CancelledError()
            if failure == "model_error":
                raise RuntimeError("isolated model unavailable")
            return json.dumps({"action":"upsert", "fact":"用户现在吃香菜", "confidence":1})
        @staticmethod
        def parse_json(value, default=None):
            return json.loads(value)
    extractor = Extractor()
    pending = corrections.process_pending_corrections(meta_repo=meta, llm=extractor, lease=True)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            drive(pending)
    else:
        stats = drive(pending)
        assert stats["learned"] == (1 if failure == "none" else 0)
    assert extractor.calls == 1
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM feedback_leases")).scalar() == 0
        assert conn.execute(text("SELECT status FROM feedback_samples")).scalar() == ("learned" if failure == "none" else "new")
    # Reopen repositories, not an in-memory receipt cache.
    reopened = create_engine("sqlite:///" + str(db.path))
    try:
        store = TerminalEventStore(reopened)
        assert store.get(inbound.event_id) is not None and store.get(correction_event.event_id) is not None
        feedback_repo = FeedbackRepository(reopened)
        projection_repo = MemoryProjectionRepository(reopened)
        if failure == "none":
            assert any(item.content == "用户现在吃香菜" for item in projection_repo.query_candidates(limit=20))
            again = drive(corrections.process_pending_corrections(meta_repo=SimpleNamespace(
                memory_projections=projection_repo, feedback_samples=feedback_repo), llm=extractor, lease=True))
            assert again["scanned"] == 0 and extractor.calls == 1
    finally:
        reopened.dispose()


def test_feedback_dedup_and_reply_binding_use_complete_platform_account_scope(db):
    repository = FeedbackRepository(db.engine)
    meta = SimpleNamespace(feedback_samples=repository)
    row = {"chat_id":"same", "chat_type":"private", "msg_id":"correction", "sender_id":"person-a", "content":"你记错了，我吃香菜"}
    history = SimpleNamespace(messages=SimpleNamespace(get_by_chat=lambda *a, **kw: [row]))
    for platform, account in (("wechat","a"), ("wechat","b"), ("qq","a")):
        reply = db.store.append(Event.new(event_type=EventType.REPLY, platform=platform, bot_account_id=account,
            chat_id="same", content="local reply", visibility_scope=frozenset({VisibilityScope.OWNER_ONLY})))
        assert feedback.record_feedback_signals([row], repo=history, meta_repo=meta, platform=platform, account_id=account) == 1
        sample = repository.list_recent(limit=1)[0]
        assert sample.reply_event_id == reply.event_id
        assert feedback.record_feedback_signals([row], repo=history, meta_repo=meta, platform=platform, account_id=account) == 0
    # A later unrelated reply must not win the WeChat/a provenance lookup.
    native = [e for e in db.store.query(event_types=(EventType.REPLY,), platform="wechat", bot_account_id="a")][0]
    assert feedback._find_reply_event_id(chat_id="same", platform="wechat", account_id="a") == native.event_id
    assert feedback._find_reply_event_id(chat_id="same") == ""
