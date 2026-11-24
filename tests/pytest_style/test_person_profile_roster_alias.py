"""群昵称 -> wxid 的兜底：people 表只认 wxid/备注时，按当前群名单解析一次。

2026-11-24：账本里 3 次 `get_person_profile 失败，找不到联系人: P5`。
"""
from types import SimpleNamespace

from agents.tools import person_profile as pp
from loop import brief


class _People:
    def __init__(self):
        self.calls = []

    def find_persons(self, query, *, platform="", account_id="", limit=5):
        self.calls.append((query, platform, account_id))
        if query == "wxid_seed05":
            return [SimpleNamespace(person_id="p-1", display_name="P5",
                                    platform=platform, account_id=account_id)]
        return []


def test_group_nickname_resolves_to_wxid_when_people_table_misses(monkeypatch):
    monkeypatch.setattr(brief, "_roster_rows", lambda chat_id: [
        ("wxid_other", "小池", 0), ("wxid_seed05", "P5", 0),
    ])
    people = _People()
    tool = pp.GetPersonProfileTool()
    got = tool._resolve_via_roster(people, "P5", chat_id="room@chatroom",
                                   platform="wechat", account_id="acct")
    assert got is not None and got.person_id == "p-1"
    assert people.calls == [("wxid_seed05", "wechat", "acct")]


def test_unknown_nickname_stays_unresolved(monkeypatch):
    monkeypatch.setattr(brief, "_roster_rows", lambda chat_id: [("wxid_seed05", "P5", 0)])
    assert pp.GetPersonProfileTool()._resolve_via_roster(
        _People(), "查无此人", chat_id="room@chatroom", platform="wechat", account_id="acct") is None


def test_roster_lookup_failure_does_not_raise(monkeypatch):
    def boom(chat_id):
        raise RuntimeError("db locked")

    monkeypatch.setattr(brief, "_roster_rows", boom)
    assert pp.GetPersonProfileTool()._resolve_via_roster(
        _People(), "P5", chat_id="room@chatroom", platform="wechat", account_id="acct") is None
