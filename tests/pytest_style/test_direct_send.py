"""点名即发（P3-5）：只认主人的私聊指令、认不出会话名就不发、发了要能反查。

判据取自方案文档 §17：主人说「在 X 群里说一句 …」时，这条路要真的把它投出去，
不再出现「她回我按不到那个键」——2026-10-21 主人为此试了 16 次最后放弃。
"""

import asyncio
import sqlite3

import pytest

from loop import direct_send, ports


class Row:
    """一条入站消息（形状对齐 loop/snapshot.py 的行对象）。"""

    def __init__(self, msg_id, text, speaker="owner", is_self=0, at="2026-11-04 13:00"):
        self.msg_id = msg_id
        self.text = text
        self.speaker = speaker
        self.is_self = is_self
        self.at = at


def test_mode_is_off_by_default(monkeypatch):
    monkeypatch.delenv("YINYUE_FEATURE_DIRECT_SEND", raising=False)
    assert direct_send.mode() == direct_send.MODE_OFF


def test_owner_ids_reads_the_env_list(monkeypatch):
    monkeypatch.setenv("YINYUE_OWNER_WXIDS", "wxid_a,wxid_b，wxid_c")
    assert direct_send.owner_ids() == {"wxid_a", "wxid_b", "wxid_c"}


def test_find_orders_fails_closed_without_an_owner_list():
    rows = [Row(1, "在银月的测试群里说一句 今天下雨记得带伞")]
    assert direct_send.find_orders(rows, chat_id="wxid_x", owners=set()) == ()


def test_find_orders_ignores_group_chats():
    rows = [Row(1, "在银月的测试群里说一句 今天下雨记得带伞")]
    assert direct_send.find_orders(rows, chat_id="10000000001@chatroom", owners={"owner"}) == ()


def test_find_orders_takes_an_owner_order():
    rows = [Row(7, "在银月的测试群里说一句：今天下雨记得带伞")]
    got = direct_send.find_orders(rows, chat_id="wxid_x", owners={"owner"})
    assert len(got) == 1
    assert got[0].msg_id == "7"
    assert "下雨" in got[0].body


def test_find_orders_skips_self_strangers_and_seen():
    rows = [
        Row(1, "在银月的测试群里说一句 今天下雨记得带伞", speaker="owner", is_self=1),
        Row(2, "在银月的测试群里说一句 今天下雨记得带伞", speaker="stranger"),
        Row(3, "在银月的测试群里说一句 今天下雨记得带伞", speaker="owner"),
    ]
    assert direct_send.find_orders(rows, chat_id="wxid_x", owners={"owner"}, seen=("3",)) == ()


def test_deliver_never_sends_when_the_target_is_unknown(monkeypatch):
    monkeypatch.setattr(ports, "resolve_say_target", lambda name: ("", "认不出这个会话"))

    async def boom(*a, **kw):
        raise AssertionError("认不出会话名还敢发")

    monkeypatch.setattr(ports, "say_many", boom)
    order = direct_send.Order(target="不存在的群", body="今天下雨记得带伞", msg_id="9")
    res = asyncio.run(direct_send.deliver(order))
    assert res["ok"] is False
    assert res["queue_ids"] == []


def test_shadow_records_the_order_but_sends_nothing(monkeypatch):
    state = {"items": {}}
    monkeypatch.setattr(direct_send, "load_state", lambda: state)
    monkeypatch.setattr(direct_send, "save_state", lambda st: None)

    async def boom(*a, **kw):
        raise AssertionError("shadow 不许真发")

    monkeypatch.setattr(ports, "say_many", boom)
    rows = [Row(11, "在银月的测试群里说一句 今天下雨记得带伞")]
    out = asyncio.run(direct_send.run(rows, chat_id="wxid_x", mode_="shadow", owners={"owner"}))
    assert out["orders"] == 1 and out["sent"] == 0
    assert state["items"]["11"]["state"] == "shadow"


def test_check_deliveries_marks_a_sent_order_delivered(monkeypatch):
    # 2026-11-04（§24 地基②）：`state='sent'` 只代表按下回车；「送到了」还要微信账库里有这个 id。
    # 这条用例原来只喂 state/ack/why，现在补上凭据那一层（查无凭据的走 unverified，另有专测）。
    from loop import receipts as _rc

    state = {"items": {"5": {"state": "queued", "queue_ids": [101], "target": "银月"}}}
    monkeypatch.setattr(direct_send, "_query_queue",
                        lambda ids, conn=None: [{"id": 101, "state": "sent", "ack": "confirmed",
                                                 "why": "msg 900"}])
    monkeypatch.setattr(_rc, "verify_row", lambda row: _rc.Receipt(verified=True))
    out = direct_send.check_deliveries(state=state)
    assert out["delivered"] == ["5"]
    assert state["items"]["5"]["state"] == "local_observed"


def test_check_deliveries_marks_a_dead_order_failed(monkeypatch):
    state = {"items": {"6": {"state": "queued", "queue_ids": [102], "target": "银月"}}}
    monkeypatch.setattr(direct_send, "_query_queue",
                        lambda ids, conn=None: [{"id": 102, "state": "failed", "ack": "", "why": "x"}])
    out = direct_send.check_deliveries(state=state)
    assert out["failed"] == ["6"]
    assert state["items"]["6"]["state"] == "failed"


def test_check_deliveries_keeps_a_pending_order_queued(monkeypatch):
    state = {"items": {"7": {"state": "queued", "queue_ids": [103], "target": "银月"}}}
    monkeypatch.setattr(direct_send, "_query_queue",
                        lambda ids, conn=None: [{"id": 103, "state": "pending", "ack": "", "why": ""}])
    out = direct_send.check_deliveries(state=state)
    assert out["pending"] == ["7"]
    assert state["items"]["7"]["state"] == "queued"


def test_sender_ids_resolves_a_display_name_to_the_wxid(tmp_path):
    """E-42：snapshot 把 speaker 换成显示名；Row 的 msg_id 是事件号，只能按正文回查。"""
    db = tmp_path / "account.db"
    con = sqlite3.connect(str(db))
    con.execute("create table messages (chat_id text, content text, sender_id text, created_at text)")
    con.execute("insert into messages values (?, ?, ?, ?)",
                ("wxid_owner", "在银月的测试群里说一句：今天下雨记得带伞", "wxid_owner", "2026-11-04 17:48:41"))
    con.commit()
    con.close()
    row = Row("27202424", "在银月的测试群里说一句：今天下雨记得带伞", speaker="Elaina")
    row.chat_id = "wxid_owner"
    rows = [row]
    owners = {"wxid_owner"}
    # 只比显示名 → 认不出（这就是 2026-11-04 真机那次 orders=0 的原因）
    assert direct_send.find_orders(rows, chat_id="wxid_owner", owners=owners) == ()
    # 按正文回查真实 sender_id → 认得出
    ids = direct_send.sender_ids(str(db), rows)
    assert ids == {"27202424": "wxid_owner"}
    got = direct_send.find_orders(rows, chat_id="wxid_owner", owners=owners, sender_ids_map=ids)
    assert len(got) == 1
    assert got[0].target == "银月的测试群"
    assert got[0].body == "今天下雨记得带伞"
    assert got[0].speaker == "wxid_owner"


def test_sender_ids_skips_self_and_unknown_rows(tmp_path):
    """自己说的话不回查（省一次查询，也不该把「我」当成主人指令）。"""
    db = tmp_path / "account.db"
    con = sqlite3.connect(str(db))
    con.execute("create table messages (chat_id text, content text, sender_id text, created_at text)")
    con.commit()
    con.close()
    mine = Row("1", "在群里说一句 我来发", speaker="我", is_self=1)
    mine.chat_id = "wxid_owner"
    assert direct_send.sender_ids(str(db), [mine]) == {}


def test_sender_ids_returns_empty_when_the_database_is_missing():
    row = Row("m-2", "在群里说一句 你好呀")
    row.chat_id = "wxid_owner"
    rows = [row]
    assert direct_send.sender_ids("", rows) == {}
    assert direct_send.sender_ids(r"no\such\dir\nope.db", rows) == {}
