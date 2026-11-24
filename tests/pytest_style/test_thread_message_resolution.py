from types import SimpleNamespace as NS

import pytest

from db.thread_scope import resolve_source_message, resolve_thread_ref


KEY = ("wechat", "a", "room")


@pytest.mark.parametrize("platform,account,chat", [("qq", "a", "room"), ("wechat", "b", "room"), ("wechat", "a", "elsewhere")])
def test_wrong_returned_source_cannot_supply_body(platform, account, chat):
    uq = NS(get_message_by_id=lambda *a, **kw: NS(platform=platform, account_id=account, data=NS(chat_id=chat, content="wrong")))
    assert resolve_source_message(uq, 7, KEY) is None


def test_exact_source_query_and_missing_source_does_not_query():
    calls = []
    body = NS(chat_id="room", content="right")
    def get(mid, **kw):
        calls.append((mid, kw))
        return NS(platform="wechat", account_id="a", data=body)
    uq = NS(get_message_by_id=get)
    assert resolve_source_message(uq, 7, ("wechat", "", "room")) is None
    assert calls == []
    assert resolve_source_message(uq, 7, KEY) is body
    assert calls == [(7, {"platform": "wechat", "account_id": "a"})]


def test_reference_must_belong_to_parent_before_query():
    calls = []
    uq = NS(get_message_by_id=lambda *a, **kw: calls.append(a))
    parent = NS(id=1, platform="wechat", account_id="a", chat_id="room")
    ref = NS(thread_id=2, msg_id=7, platform="wechat", account_id="a", chat_id="room")
    with pytest.raises(ValueError, match="parent_mismatch"):
        resolve_thread_ref(uq, parent, ref)
    assert calls == []


def test_query_failure_propagates_instead_of_looking_like_no_match():
    def fail(*a, **kw):
        raise RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        resolve_source_message(NS(get_message_by_id=fail), 7, KEY)


def test_summary_upgrade_uses_verified_body_without_legacy_fallback():
    from agents.auto_dream import AutoDream
    parent = NS(id=1, platform="wechat", account_id="a", chat_id="room", scope="topic")
    ref = NS(thread_id=1, msg_id=7, platform="wechat", account_id="a", chat_id="room")
    calls = []
    async def chat(**kw):
        calls.append(kw)
        return "summary"
    repo = NS(threads=NS(get_by_id=lambda i: parent), thread_refs=NS(get_by_thread=lambda *a, **kw: [ref]))
    uq = NS(get_message_by_id=lambda *a, **kw: NS(platform="wechat", account_id="b", data=NS(chat_id="room", content="foreign")))
    import asyncio
    result = asyncio.run(AutoDream(repo, llm=NS(chat=chat), unified_query=uq).execute_summary_upgrade(1))
    assert result["ok"] is False
    assert calls == []


def test_verified_summary_body_reaches_existing_llm_call():
    import asyncio
    from agents.auto_dream import AutoDream
    parent = NS(id=1, platform="wechat", account_id="a", chat_id="room", scope="topic")
    ref = NS(thread_id=1, msg_id=7, platform="wechat", account_id="a", chat_id="room")
    calls = []
    async def chat(**kw):
        calls.append(kw)
        raise RuntimeError("isolated stop after capture")
    repo = NS(threads=NS(get_by_id=lambda i: parent), thread_refs=NS(get_by_thread=lambda *a, **kw: [ref]))
    body = NS(chat_id="room", content="合法原文", is_self=False, sender_id="peer")
    uq = NS(get_message_by_id=lambda *a, **kw: NS(platform="wechat", account_id="a", data=body))
    result = asyncio.run(AutoDream(repo, llm=NS(chat=chat), unified_query=uq).execute_summary_upgrade(1))
    assert result["ok"] is False
    assert len(calls) == 1
    assert "合法原文" in calls[0]["user"]
