"""说话前认人：名字要认得出、认重了不许猜、没来往过的不许发（2026-11-04 全 C 地基③）。

这些用例都在行为层：造一个真的 sqlite 账库，然后看 `loop/targets.py` 与
`loop/ports.py::resolve_say_target` 到底放不放行。
"""
from __future__ import annotations

import sqlite3

import pytest

from loop import ports, targets


def _make_db(path, *, contacts=(), chats=(), senders=()):
    con = sqlite3.connect(str(path))
    try:
        con.execute("create table contacts (wxid text, remark text, nickname text)")
        con.execute("create table messages (id integer primary key, chat_id text, content text, "
                    "is_self integer, created_at text, sender_name text, sender_id text)")
        con.executemany("insert into contacts (wxid, remark, nickname) values (?, ?, ?)",
                        list(contacts))
        rows = []
        for i, cid in enumerate(chats, 1):
            rows.append((i, cid, "在吗", 0, "2026-11-04 12:00:00"))
        con.executemany("insert into messages (id, chat_id, content, is_self, created_at) "
                        "values (?, ?, ?, ?, ?)", rows)
        srows = []
        for i, (sname, sid, scid) in enumerate(senders, 1000):
            srows.append((i, scid, "在吗", 0, "2026-11-04 12:00:00", sname, sid))
        con.executemany(
            "insert into messages (id, chat_id, content, is_self, created_at, "
            "sender_name, sender_id) values (?, ?, ?, ?, ?, ?, ?)", srows)
        con.commit()
    finally:
        con.close()
    return str(path)


@pytest.fixture()
def db(tmp_path):
    return _make_db(tmp_path / "account.db",
                    contacts=[("wxid_k", "小K", "小K同学"),
                              ("wxid_fish", "大肥鱼", "大肥鱼"),
                              ("10000000001@chatroom", "银月的测试群", "银月的测试群"),
                              ("wxid_ghost", "幽灵", "幽灵")],
                    chats=["wxid_k", "10000000001@chatroom", "wxid_fish"])


def test_a_known_name_with_history_resolves(db):
    got = targets.whois("小K", db=db)
    assert got.ok is True
    assert got.chat_id == "wxid_k"


def test_the_nickname_also_works(db):
    got = targets.whois("小K同学", db=db)
    assert got.ok is True
    assert got.chat_id == "wxid_k"


def test_full_width_and_spaces_are_normalized(db):
    got = targets.whois(" 小Ｋ ", db=db)
    assert got.ok is True
    assert got.chat_id == "wxid_k"


def test_a_contact_we_never_talked_to_is_refused(db):
    got = targets.whois("幽灵", db=db)
    assert got.ok is False
    assert "从没跟这个会话说过话" in got.why


def test_an_unknown_name_is_refused(db):
    got = targets.whois("王五", db=db)
    assert got.ok is False
    assert "认不出" in got.why


def test_a_chat_id_itself_resolves_when_there_is_history(db):
    got = targets.whois("10000000001@chatroom", db=db)
    assert got.ok is True
    assert got.chat_id == "10000000001@chatroom"


def test_two_people_sharing_a_remark_are_ambiguous(tmp_path):
    path = _make_db(tmp_path / "dup.db",
                    contacts=[("wxid_a", "班长", "甲"), ("wxid_b", "班长", "乙")],
                    chats=["wxid_a", "wxid_b"])
    got = targets.whois("班长", db=path)
    assert got.ok is False
    assert "不敢猜" in got.why
    assert set(got.candidates) == {"wxid_a", "wxid_b"}


def test_an_unreadable_db_never_allows_anything(tmp_path):
    missing = str(tmp_path / "nope.db")
    assert targets.history_depth("wxid_k", db=missing) == 0
    assert targets.allowed("wxid_k", db=missing) is False


def test_one_message_is_enough_for_the_whitelist(db):
    assert targets.history_depth("wxid_k", db=db) == 1
    assert targets.allowed("wxid_k", db=db) is True
    assert targets.allowed("wxid_ghost", db=db) is False


def test_say_target_refuses_a_chat_with_no_history(db, monkeypatch):
    monkeypatch.setattr(ports, "_account_db", lambda: db)
    monkeypatch.setattr(targets, "allowed", lambda cid, **kw: False)
    cid, why = ports.resolve_say_target("银月的测试群")
    assert cid == ""
    assert "认得出" in why
    assert "不敢主动发过去" in why


def test_say_target_accepts_a_chat_with_history(db, monkeypatch):
    monkeypatch.setattr(ports, "_account_db", lambda: db)
    cid, why = ports.resolve_say_target("银月的测试群")
    assert cid == "10000000001@chatroom"
    assert why == ""


def test_a_raw_chat_id_does_not_skip_the_whitelist(db, monkeypatch):
    monkeypatch.setattr(ports, "_account_db", lambda: db)
    monkeypatch.setattr(targets, "allowed", lambda cid, **kw: False)
    cid, why = ports.resolve_say_target("wxid_ghost")
    assert cid == ""
    assert "不敢主动发过去" in why


def test_a_group_display_name_maps_to_its_wxid(tmp_path):
    """群里叫「P5」的人真身是 wxid_p，而且跟他单独说过话 -> 认得出、也能发。"""
    path = _make_db(tmp_path / "group.db",
                    chats=["wxid_p", "10000000001@chatroom"],
                    senders=[("P5", "wxid_p", "10000000001@chatroom")])
    got = targets.whois("P5", db=path)
    assert got.ok is True
    assert got.chat_id == "wxid_p"


def test_a_display_name_without_dm_history_is_refused(tmp_path):
    """认得出是群里的名字，但没跟他单独说过话 -> 不发（认得出 ≠ 有来往）。"""
    path = _make_db(tmp_path / "group2.db",
                    chats=["10000000001@chatroom"],
                    senders=[("P5", "wxid_p", "10000000001@chatroom")])
    got = targets.whois("P5", db=path)
    assert got.ok is False
    assert "群里的名字" in got.why
    assert "没跟这个人单独说过话" in got.why


def test_two_people_sharing_a_display_name_are_ambiguous(tmp_path):
    path = _make_db(tmp_path / "group3.db",
                    chats=["wxid_p1", "wxid_p2", "10000000001@chatroom"],
                    senders=[("班长", "wxid_p1", "10000000001@chatroom"),
                             ("班长", "wxid_p2", "10000000001@chatroom")])
    got = targets.whois("班长", db=path)
    assert got.ok is False
    assert "不敢猜" in got.why
    assert set(got.candidates) == {"wxid_p1", "wxid_p2"}


def test_contacts_win_over_message_names(tmp_path):
    """通讯录里有这个人时，不拿群里的显示名去改判（显示名可能重名、可能改过）。"""
    path = _make_db(tmp_path / "group4.db",
                    contacts=[("wxid_ban", "班长", "班长")],
                    chats=["wxid_ban", "10000000001@chatroom"],
                    senders=[("班长", "wxid_other", "10000000001@chatroom")])
    got = targets.whois("班长", db=path)
    assert got.ok is True
    assert got.chat_id == "wxid_ban"


def test_sender_names_are_normalized_before_comparing(tmp_path):
    path = _make_db(tmp_path / "group5.db",
                    chats=["wxid_y", "10000000001@chatroom"],
                    senders=[("小 榆", "wxid_y", "10000000001@chatroom")])
    got = targets.whois("P6", db=path)
    assert got.ok is True
    assert got.chat_id == "wxid_y"


def test_senders_of_an_unreadable_db_is_empty(tmp_path):
    assert targets.senders("P5", db=str(tmp_path / "nope.db")) == ()


def test_an_unknown_name_says_both_places_were_checked(db):
    got = targets.whois("王五", db=db)
    assert got.ok is False
    assert "通讯录和聊天记录里都没有" in got.why
