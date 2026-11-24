import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine, text

from agents.auto_dream import AutoDream


def test_budget_uses_source_and_allocates_each_account_independently():
    now = datetime.now()
    threads = [NS(id=i, platform="wechat", account_id=aid, chat_id="room", context_budget=4096, scope="topic")
               for i, aid in [(1, "a"), (2, "b")]]
    refs = {t.id: [NS(thread_id=t.id, msg_id=7, platform=t.platform, account_id=t.account_id, chat_id=t.chat_id)] for t in threads}
    calls = []
    def search(**kw):
        calls.append(kw)
        # Even an incorrectly routed query result cannot inflate account b.
        return [NS(platform="wechat", account_id="a", data=NS(id=7, chat_id="room", created_at=now))]
    repo = NS(threads=NS(get_active=lambda *a, **kw: threads), thread_refs=NS(get_by_thread=lambda i, **kw: refs[i]))
    result = AutoDream(repo, unified_query=NS(search_messages=search)).detect_budget_candidates("room")
    by_id = {r["thread_id"]: r for r in result}
    assert by_id[1]["msg_rate"] == 0.17
    assert by_id[1]["share"] == 1.0
    assert by_id[1]["new_budget"] == 6144
    assert by_id[2]["msg_rate"] == 0
    assert by_id[2]["new_budget"] == 2048
    assert {(c["platform"], c["account_id"], c["chat_id"]) for c in calls} == {("wechat", "a", "room"), ("wechat", "b", "room")}


def test_budget_unknown_source_fails_before_query():
    calls = []
    repo = NS(threads=NS(get_active=lambda *a, **kw: [NS(id=1, platform="wechat", account_id="", chat_id="room")]))
    with pytest.raises(ValueError, match="source_mismatch"):
        AutoDream(repo, unified_query=NS(search_messages=lambda **kw: calls.append(kw))).detect_budget_candidates("room")
    assert calls == []


def test_summary_migration_preserves_old_rows_and_is_idempotent():
    from db.thread_scope import upgrade_thread_summaries
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("create table threads(id integer primary key, scope text)"))
        conn.execute(text("insert into threads values(1,'原文')"))
        upgrade_thread_summaries(conn)
        upgrade_thread_summaries(conn)
        assert tuple(conn.execute(text("select scope,summary from threads")).one()) == ("原文", None)
    engine.dispose()


def test_summary_upgrade_is_persisted_on_exact_thread_and_recalled(tmp_path):
    from db.meta_models import init_meta_db
    from db.models import Thread, ThreadMsgRef, Topic
    from db.repo_threads import ThreadRepository, ThreadMsgRefRepository, TopicRepository
    from core.memory.providers import TopicMemoryProvider
    engine = init_meta_db(str(tmp_path / "meta.db"))
    threads, refs = ThreadRepository(engine), ThreadMsgRefRepository(engine)
    parent = threads.create(Thread(platform="wechat", account_id="a", chat_id="room", scope="topic"))
    topics = TopicRepository(engine)
    old_topic = topics.create(Topic(chat_id="room", title="topic", summary="旧主题原文"))
    foreign = threads.create(Thread(platform="wechat", account_id="b", chat_id="room", scope="topic"))
    refs.add(ThreadMsgRef(thread_id=parent.id, platform="wechat", account_id="a", chat_id="room", msg_id=7))
    repo = NS(threads=threads, thread_refs=refs)
    async def chat(**kw):
        return "独特结论"
    body = NS(chat_id="room", content="原始讨论", msg_id="external-7", is_self=False, sender_id="peer")
    uq = NS(get_message_by_id=lambda *a, **kw: NS(platform="wechat", account_id="a", data=body))
    result = asyncio.run(AutoDream(repo, llm=NS(chat=chat), unified_query=uq).execute_summary_upgrade(parent.id))
    assert result["ok"] is True
    assert threads.get_by_id(parent.id).summary == "独特结论"
    assert threads.get_by_id(parent.id).summary_quality == "refined"
    assert threads.get_by_id(foreign.id).summary is None
    assert topics.get_by_id(old_topic.id).summary == "旧主题原文"
    principal = NS(platform="wechat", bot_account_id="a", chat_id="room", person_id="p", is_owner=False, is_group=True)
    items = TopicMemoryProvider(meta_repo=repo, unified_query=uq).recall(principal, "独特结论")
    assert len(items) == 1
    assert "独特结论" in items[0].content
    engine.dispose()
