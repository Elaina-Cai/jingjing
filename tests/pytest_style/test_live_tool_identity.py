import asyncio
import contextlib
import sqlite3
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from loop import ports


def test_world_read_separates_equal_account_names_across_platforms(monkeypatch):
    from db import world_db
    con = sqlite3.connect(":memory:")
    con.execute("create table inbound_event(platform text,account_id text,msg_id text,chat_id text,who text,text text,ts_utc text,is_self integer,quote_user text,media_path text,kind text,quote_id text,who_id text,platform_msg_id text)")
    for platform in ("wechat", "qq"):
        con.execute("insert into inbound_event values(?, 'bot', ?, '123', '显示名', ?, strftime('%Y-%m-%dT%H:%M:%S','now'), 0, '', '', 'text', '', ?, '')",
                    (platform, platform + ":bot:1", platform + "正文", platform + "_sender"))
    @contextlib.contextmanager
    def connection():
        yield con
    monkeypatch.setattr(world_db, "conn", connection)
    monkeypatch.setattr(world_db, "_read_conn", connection)
    rows = world_db.sense_rows("data/platforms/qq/bot/account.db")
    assert [r["content"] for r in rows] == ["qq正文"]
    assert rows[0]["sender_id"] == "qq_sender"
    con.close()


def test_batch_sender_requires_one_real_identity_in_current_chat():
    def row(sender, chat="room", own=False):
        return NS(sender_id=sender, chat_id=chat, is_self=own, speaker="主人")
    assert ports.turn_sender([row("a"), row("a")], "room") == "a"
    assert ports.turn_sender([row("a"), row("b")], "room") == ""
    assert ports.turn_sender([row("a"), row("")], "room") == ""
    assert ports.turn_sender([row("a", "other")], "room") == ""
    assert ports.turn_sender([row("a", own=True)], "room") == ""


@pytest.mark.parametrize("chat,sender,target,allowed", [
    ("room@chatroom", "peer", 1, True),
    ("room@chatroom", "owner", 2, False),
    ("wxid_owner", "owner", 2, True),
    ("wxid_owner", "owner", 3, False),
])
def test_program_scope_to_real_authority_to_thread_body(monkeypatch, tmp_path, chat, sender, target, allowed):
    import core.bootstrap as bootstrap
    import core.terminal.authority as authority
    from core.terminal.identity_service import IdentityService
    from core.terminal.principal import RelationshipState, RelationLevel
    from agents.tools.memory import RecallThreadTool
    from db.meta_models import init_meta_db
    from db.models import Thread, ThreadMsgRef
    from db.repo_threads import ThreadRepository, ThreadMsgRefRepository
    service = authority.AuthorityService(IdentityService(
        strong_identities={("wechat", "bot", "owner"): "owner-person", ("wechat", "bot", "peer"): "peer-person"},
        relationships={("wechat", "bot", "owner-person"): RelationshipState("wechat", "bot", "owner-person", RelationLevel.OWNER)}))
    monkeypatch.setattr(authority, "get_authority_service", lambda **kw: service)
    engine = init_meta_db(str(tmp_path / "meta.db"))
    threads, refs = ThreadRepository(engine), ThreadMsgRefRepository(engine)
    for account, room in [("bot", "room@chatroom"), ("bot", "other@chatroom"), ("foreign", "room@chatroom")]:
        t = threads.create(Thread(platform="wechat", account_id=account, chat_id=room, scope="题目"))
        refs.add(ThreadMsgRef(thread_id=t.id, platform="wechat", account_id=account, chat_id=room, msg_id=t.id, status="confirmed"))
    queries = []
    def get(mid, **kw):
        queries.append((mid, kw))
        t = threads.get_by_id(mid)
        return NS(platform="wechat", account_id=t.account_id, data=NS(chat_id=t.chat_id, content="授权原文", sender_id="peer", created_at=datetime.now()))
    contact_calls = []
    def get_contact(room, **kw):
        contact_calls.append(kw)
        return NS(platform="wechat", account_id="bot", data=NS(wxid=room, remark="本账号群名", nickname=""))
    tool = RecallThreadTool(NS(meta=NS(threads=threads, thread_refs=refs)), unified_query=NS(get_message_by_id=get, get_contact=get_contact))
    monkeypatch.setattr(bootstrap, "init_singletons", lambda **kw: None)
    monkeypatch.setattr(bootstrap, "get_tools", lambda: {"recall_thread": tool})
    principals = []
    @ports.scoped_turn
    async def turn(db, *, platform, chat_id, sender_id):
        result = await ports.act("recall_thread", {"thread_id": target, "_chat": "wxid_owner"})
        principals.append(tool.principal)
        return result
    result = asyncio.run(turn("data/platforms/wechat/bot/account.db", platform="wechat", chat_id=chat, sender_id=sender))
    assert result.ok is allowed
    assert ("授权原文" in result.text) is allowed
    assert bool(queries) is allowed
    assert bool(contact_calls) is allowed
    assert all(c == {"platform": "wechat", "account_id": "bot"} for c in contact_calls)
    assert principals[0].person_id == ("owner-person" if sender == "owner" else "peer-person")
    engine.dispose()


def test_tool_query_error_returns_failure_not_empty_success(monkeypatch):
    import core.bootstrap as bootstrap
    import core.terminal.authority as authority
    monkeypatch.setattr(authority, "get_authority_service", lambda **kw: None)
    class Broken:
        async def execute(self, **kw):
            raise RuntimeError("database unavailable")
    monkeypatch.setattr(bootstrap, "init_singletons", lambda **kw: None)
    monkeypatch.setattr(bootstrap, "get_tools", lambda: {"recall_thread": Broken()})
    @ports.scoped_turn
    async def turn(db, *, platform, chat_id):
        return await ports.act("recall_thread", {"thread_id": 1})
    result = asyncio.run(turn("data/platforms/wechat/bot/account.db", platform="wechat", chat_id="room@chatroom"))
    assert not result.ok
    assert "database unavailable" in result.error
