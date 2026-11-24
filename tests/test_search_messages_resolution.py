"""search_messages chat_id resolution regression.

The conversational agent naturally calls search_messages(chat_id="P6").
The raw column stores "wxid_..."; without resolution private history searches
look empty and the model concludes "我们没有私聊过".
"""

from __future__ import annotations

import asyncio
import os
import sys

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


def test_display_name_resolves_to_wxid() -> None:
    uq = _FakeUnifiedQuery()
    tool = SearchMessagesTool(repo=None, unified_query=uq)
    result = asyncio.run(tool.execute(
        chat_id="P6",
        chat_type="private",
        platform="wechat",
        account_id="wxid_bot",
    ))
    assert result.error == ""
    assert uq.search_kwargs is not None
    assert uq.search_kwargs["chat_id"] == "wxid_seed06", uq.search_kwargs


def test_unknown_name_stays_as_is() -> None:
    uq = _FakeUnifiedQuery()
    tool = SearchMessagesTool(repo=None, unified_query=uq)
    asyncio.run(tool.execute(
        chat_id="不存在的人",
        chat_type="private",
        platform="wechat",
        account_id="wxid_bot",
    ))
    assert uq.search_kwargs["chat_id"] == "不存在的人", uq.search_kwargs


def main() -> bool:
    tests = [test_display_name_resolves_to_wxid, test_unknown_name_stays_as_is]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"search_messages resolution checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
