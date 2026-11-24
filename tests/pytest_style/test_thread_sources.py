from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine, text

from core.memory.providers import TopicMemoryProvider


def principal(account="a", chat="room", owner=False, group=True):
    return NS(platform="wechat", bot_account_id=account, chat_id=chat, is_owner=owner, is_group=group, person_id="p")


def thread(id=1, account="a", chat="room"):
    return NS(id=id, platform="wechat", account_id=account, chat_id=chat, scope="线程正文", status="active")


def provider(rows):
    meta = NS(threads=NS(
        find_scoped=lambda *a, **kw: rows,
        get_active=lambda *a, **kw: rows,
        get_all_active=lambda **kw: rows,
        get_by_id=lambda id: next((r for r in rows if r.id == id), None)),
        thread_refs=NS(get_by_thread=lambda *a, **kw: []))
    return TopicMemoryProvider(meta_repo=meta, unified_query=NS())


def test_explicit_chat_search_does_not_bypass_tenant_or_group():
    p = provider([thread(), thread(2, account="b"), thread(3, chat="other")])
    assert [t.id for t in p.find_threads(principal(), chat_id="room")] == [1]
    assert p.find_threads(principal(), chat_id="other") == []
    assert p.find_threads(None, chat_id="room") == []


def test_owner_private_can_read_other_chat_in_same_account():
    p = provider([thread(), thread(2, account="b"), thread(3, chat="other")])
    assert [t.id for t in p.find_threads(principal(owner=True, group=False))] == [1, 3]


def test_id_lookup_and_refs_check_scope():
    p = provider([thread(1, account="b"), thread(2)])
    assert p.get_thread(1, principal=principal()) is None
    assert p.get_thread(2, principal=principal()).id == 2
    assert p.get_thread_refs(1, principal=principal()) == []


def test_unknown_thread_source_is_not_assigned_to_current_account():
    p = provider([thread(account="")])
    assert p.find_threads(principal(), chat_id="room") == []


def test_message_resolution_uses_full_source_and_verifies_response():
    p = provider([thread()])
    calls = []

    def get(id, **kwargs):
        calls.append((id, kwargs))
        return NS(platform="wechat", account_id="a", data=NS(chat_id="room", content="合法正文"))

    p._uq = NS(get_message_by_id=get)
    ref = NS(msg_id=7, platform="wechat", account_id="a", chat_id="room")
    assert p.resolve_ref(ref, principal()).content == "合法正文"
    assert calls[0][1] == {"platform": "wechat", "account_id": "a"}
    ref.account_id = "b"
    assert p.resolve_ref(ref, principal()) is None
    assert len(calls) == 1
    ref.account_id = "a"
    p._uq = NS(get_message_by_id=lambda *a, **kw: NS(platform="wechat", account_id="a", data=NS(chat_id="other", content="越界正文")))
    assert p.resolve_ref(ref, principal()) is None


def test_migration_is_idempotent_and_leaves_legacy_unknown(tmp_path):
    from db.thread_scope import upgrade_thread_sources
    engine = create_engine("sqlite:///" + str(tmp_path / "old.db"))
    with engine.begin() as con:
        con.execute(text("create table threads(id integer primary key, chat_id text, scope text)"))
        con.execute(text("create table thread_msg_ref(id integer primary key, thread_id integer, msg_id integer)"))
        con.execute(text("insert into threads values(1,'room','历史原文')"))
        con.execute(text("insert into thread_msg_ref values(1,1,7)"))
        upgrade_thread_sources(con)
        upgrade_thread_sources(con)
        row = con.execute(text("select platform,account_id,scope from threads")).one()
        assert tuple(row) == ("", "", "历史原文")
        assert con.execute(text("select msg_id from thread_msg_ref")).scalar() == 7
    engine.dispose()


def test_schema_extension_does_not_create_thread_tables_in_account_db():
    from db.thread_scope import upgrade_thread_sources
    engine = create_engine("sqlite://")
    with engine.begin() as con:
        con.execute(text("create table messages(id integer primary key)"))
        upgrade_thread_sources(con)
        assert set(con.execute(text("select name from sqlite_master where type='table'")).scalars()) == {"messages"}
    engine.dispose()


def test_source_columns_match_new_meta_and_legacy_models(tmp_path):
    from sqlalchemy import inspect
    from db import models, meta_models
    from db.thread_scope import SOURCE_COLUMNS
    engine = meta_models.init_meta_db(str(tmp_path / "new-meta.db"))
    for table, names in SOURCE_COLUMNS.items():
        actual = {c["name"] for c in inspect(engine).get_columns(table)}
        assert set(names) <= actual
        for metadata in (models.Base.metadata, meta_models.MetaBase.metadata):
            for name in names:
                assert metadata.tables[table].c[name].nullable is False
    engine.dispose()


def test_meta_extension_does_not_skip_application_migration_chain(tmp_path):
    from db.migrations import run_meta_migrations, run_migrations
    path = str(tmp_path / "old-meta.db")
    engine = create_engine("sqlite:///" + path)
    with engine.begin() as con:
        con.execute(text("create table _schema_version(version integer primary key,description text)"))
        con.execute(text("insert into _schema_version values(67,'existing')"))
        con.execute(text("create table threads(id integer primary key, chat_id text)"))
    run_meta_migrations(path)
    with engine.connect() as con:
        assert con.execute(text("select max(version) from _schema_version")).scalar() == 67
        assert "platform" in {r[1] for r in con.execute(text("pragma table_info(threads)"))}
    run_migrations(path)
    with engine.connect() as con:
        assert con.execute(text("select max(version) from _schema_version")).scalar() == 69
    engine.dispose()


def test_repository_rejects_ref_with_another_source(tmp_path):
    from db.models import Thread, ThreadMsgRef
    from db.meta_models import init_meta_db
    from db.repo_threads import ThreadRepository, ThreadMsgRefRepository
    engine = init_meta_db(str(tmp_path / "meta.db"))
    parent = ThreadRepository(engine).create(Thread(platform="wechat", account_id="a", chat_id="room", scope="题目"))
    refs = ThreadMsgRefRepository(engine)
    with pytest.raises(ValueError):
        refs.add(ThreadMsgRef(thread_id=parent.id, msg_id=7, platform="wechat", account_id="b", chat_id="room"))
    assert refs.get_by_thread(parent.id) == []
    refs.add(ThreadMsgRef(thread_id=parent.id, msg_id=7, platform="wechat", account_id="a", chat_id="room"))
    assert len(refs.get_by_thread(parent.id)) == 1
    engine.dispose()
