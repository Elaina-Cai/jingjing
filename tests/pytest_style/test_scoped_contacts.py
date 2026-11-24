import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from core.memory.contact_scope import ScopedContacts


def principal(chat="room@chatroom", owner=False):
    from core.terminal.principal import PrincipalContext, ChatType, RelationLevel
    return PrincipalContext(platform="wechat", bot_account_id="bot", chat_id=chat,
        chat_type=ChatType.GROUP if chat.endswith("@chatroom") else ChatType.PRIVATE,
        person_id="p", display_name="显示名", relation_to_bot=RelationLevel.OWNER if owner else RelationLevel.ORDINARY)


def result(account="bot", wxid="room@chatroom", name="本账号群"):
    return NS(platform="wechat", account_id=account, data=NS(wxid=wxid, remark=name, nickname="", is_group=True))


def test_names_query_exact_tenant_and_reject_foreign_return():
    calls = []
    def get(raw, **kw):
        calls.append((raw, kw))
        return result(account="foreign", name="另一个账号的秘密群名")
    contacts = ScopedContacts(NS(get_contact=get), principal())
    assert contacts.display("room@chatroom") == "room@chatroom"
    assert calls == [("room@chatroom", {"platform": "wechat", "account_id": "bot"})]


def test_name_ambiguity_is_explicit_and_never_selects_first():
    query = NS(get_contact=lambda *a, **kw: None,
               search_contacts=lambda **kw: [result(wxid="a@chatroom"), result(wxid="b@chatroom")])
    with pytest.raises(ValueError, match="ambiguous"):
        ScopedContacts(query, principal()).resolve("同名群")


def test_unique_local_name_remains_resolvable():
    calls = []
    def search(**kw):
        calls.append(kw)
        return [result(), result(account="foreign", wxid="foreign@chatroom")]
    contacts = ScopedContacts(NS(get_contact=lambda *a, **kw: None, search_contacts=search), principal())
    assert contacts.resolve("群名", group_only=True).wxid == "room@chatroom"
    assert calls == [{"query": "群名", "platform": "wechat", "account_id": "bot"}]


def test_explicit_unresolved_thread_chat_does_not_broaden_search():
    from agents.tools.search import SearchThreadsTool
    tool = SearchThreadsTool(NS(), unified_query=NS(get_contact=lambda *a, **kw: None, search_contacts=lambda **kw: []))
    # No repo/thread capability supplied: broadening would fail this test.
    tool._ctx = NS(principal=principal())
    output = asyncio.run(tool.execute(query="项目", chat_id="不存在的名字"))
    assert output.error and "contact_not_found" in output.error


@pytest.mark.parametrize("chat,owner,allowed", [("room@chatroom", True, False), ("wxid_owner", True, True), ("other@chatroom", False, True)])
def test_contact_recent_messages_obey_same_chat_policy(chat, owner, allowed):
    from agents.tools.contact import GetContactInfoTool
    calls = []
    def messages(**kw):
        calls.append(kw)
        return [NS(platform="wechat", account_id="bot", data=NS(chat_id="other@chatroom", content="合法正文", created_at=datetime.now())),
                NS(platform="wechat", account_id="foreign", data=NS(chat_id="other@chatroom", content="越界正文", created_at=datetime.now()))]
    query = NS(get_contact=lambda *a, **kw: result(wxid="other@chatroom"), get_chat_messages=messages)
    tool = GetContactInfoTool(NS(), unified_query=query)
    # Use actual principal type required by the authority adapter.
    from core.terminal.principal import PrincipalContext, ChatType, RelationLevel
    p = PrincipalContext(platform="wechat", bot_account_id="bot", chat_id=chat,
                         chat_type=ChatType.GROUP if chat.endswith("@chatroom") else ChatType.PRIVATE,
                         person_id="p", display_name="主人", relation_to_bot=RelationLevel.OWNER if owner else RelationLevel.ORDINARY)
    tool._ctx = NS(principal=p)
    output = asyncio.run(tool.execute(wxid="other@chatroom"))
    assert bool(output.error) is not allowed
    assert bool(calls) is allowed
    assert ("合法正文" in output.output) is allowed
    assert "越界正文" not in output.output
