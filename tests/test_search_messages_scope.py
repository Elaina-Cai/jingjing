"""Search-message conversation-scope guard regression tests.

Owner private chat may traverse chats.  Groups and ordinary private contacts
may only search their current conversation.
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.tools.search import SearchMessagesTool


class _FakeContact:
    wxid = "wxid_seed06"
    nickname = "P6"


class _FakeResult:
    data = _FakeContact()


class _FakeUnifiedQuery:
    def __init__(self):
        self.search_kwargs = None

    def search_contacts(self, query, **kwargs):
        if query == "P6":
            return [_FakeResult()]
        return []

    def search_messages(self, **kwargs):
        self.search_kwargs = kwargs
        return []


def _tool(extra: dict, owner: bool) -> tuple[SearchMessagesTool, _FakeUnifiedQuery, object]:
    uq = _FakeUnifiedQuery()
    tool = SearchMessagesTool(repo=None, unified_query=uq)
    if extra is not None:
        tool.set_context(SimpleNamespace(extra=extra))
    original = SearchMessagesTool._is_owner
    SearchMessagesTool._is_owner = lambda self, platform, account_id, sender_id: owner
    return tool, uq, original


def _run(tool, **kwargs):
    return asyncio.run(tool.execute(**kwargs))


def test_group_context_rejects_other_chat() -> None:
    tool, uq, original = _tool({
        "chat_id": "10000000001@chatroom",
        "is_group": True,
        "sender_id": "wxid_guest",
        "platform": "wechat",
        "account_id": "wxid_bot",
    }, owner=False)
    try:
        result = _run(tool, chat_id="P6", chat_type="private")
        assert result.is_error
        assert "仅限当前群聊" in result.error
        assert uq.search_kwargs is None
    finally:
        SearchMessagesTool._is_owner = original


def test_group_context_without_target_searches_current_group() -> None:
    tool, uq, original = _tool({
        "chat_id": "10000000001@chatroom",
        "is_group": True,
        "sender_id": "wxid_guest",
        "platform": "wechat",
        "account_id": "wxid_bot",
    }, owner=True)
    try:
        _run(tool, keyword="活动")
        assert uq.search_kwargs["chat_id"] == "10000000001@chatroom"
        assert uq.search_kwargs["chat_type"] == "group"
    finally:
        SearchMessagesTool._is_owner = original


def test_non_owner_private_rejects_other_chat() -> None:
    tool, uq, original = _tool({
        "chat_id": "wxid_seed06",
        "is_group": False,
        "sender_id": "wxid_seed06",
        "platform": "wechat",
        "account_id": "wxid_bot",
    }, owner=False)
    try:
        result = _run(tool, chat_id="P6", chat_type="private")
        # It is the same private chat, so this should be allowed, not rejected.
        assert not result.is_error
        assert uq.search_kwargs["chat_id"] == "wxid_seed06"

        uq.search_kwargs = None
        result = _run(tool, chat_id="别人", chat_type="private")
        assert result.is_error
        assert "普通联系人" in result.error
        assert uq.search_kwargs is None
    finally:
        SearchMessagesTool._is_owner = original


def test_owner_private_can_search_another_chat() -> None:
    tool, uq, original = _tool({
        "chat_id": "wxid_seed01",
        "is_group": False,
        "sender_id": "wxid_seed01",
        "platform": "wechat",
        "account_id": "wxid_bot",
    }, owner=True)
    try:
        result = _run(tool, chat_id="P6", chat_type="private")
        assert not result.is_error
        assert uq.search_kwargs["chat_id"] == "wxid_seed06"
    finally:
        SearchMessagesTool._is_owner = original


def test_context_account_wins_over_model_kwargs() -> None:
    tool, uq, original = _tool({
        "chat_id": "wxid_seed06",
        "is_group": False,
        "sender_id": "wxid_owner",
        "platform": "wechat",
        "account_id": "wxid_bot",
    }, owner=True)
    try:
        _run(
            tool,
            chat_id="P6",
            chat_type="private",
            platform="qq",
            account_id="other-account",
        )
        assert uq.search_kwargs["platform"] == "wechat", uq.search_kwargs
        assert uq.search_kwargs["account_id"] == "wxid_bot", uq.search_kwargs
    finally:
        SearchMessagesTool._is_owner = original


def main() -> bool:
    tests = [
        test_group_context_rejects_other_chat,
        test_group_context_without_target_searches_current_group,
        test_non_owner_private_rejects_other_chat,
        test_owner_private_can_search_another_chat,
        test_context_account_wins_over_model_kwargs,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"search_messages scope checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
