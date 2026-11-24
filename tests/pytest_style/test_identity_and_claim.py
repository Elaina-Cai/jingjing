# -*- coding: utf-8 -*-
"""身份与认领：真 sender_id 随行走、回读追认不许两条行抢同一个 msg_id（2026-11-04）。

背景（E-42 / E-44 / 261-262 现场）：
- `loop/snapshot.py` 会把行上的 `speaker` 换成**给人看的显示名**（Elaina），拿它比 wxid 永远不中；
- 追认线程按「同会话同文本取最新一条」回读，两条队列行会拿到同一个 msg_id。

这两个 bug 都是「用看起来像身份的东西当身份」。这里只测行为，不测源码文本。
"""
import os
import sqlite3

from db import world_db
from loop import desk, direct_send, ports


class Kick:
    """最小行对象：只带 direct_send.find_orders 会读的字段。"""

    def __init__(self, msg_id, text, speaker="Elaina", sender_id="", is_self=0):
        self.msg_id = msg_id
        self.text = text
        self.speaker = speaker
        self.sender_id = sender_id
        self.is_self = is_self
        self.at = "2026-11-04 18:00:00"


def test_rows_carry_the_real_sender_id():
    raw = [{
        "id": 7, "chat_id": "wxid_owner", "chat_type": "private", "speaker": "Elaina",
        "created_at": "2026-11-04 18:00:00", "content": "在银月的测试群里说一句：明天开会",
        "is_self": 0, "kind": "text", "quote_user": "", "media_path": "",
        "sender_id": "wxid_owner",
    }]
    rows = ports._rows(raw, {})
    assert rows[0].sender_id == "wxid_owner"
    assert rows[0].speaker == "Elaina"


def test_rows_without_sender_id_degrade_to_empty():
    raw = [{
        "id": 8, "chat_id": "wxid_owner", "chat_type": "private", "speaker": "Elaina",
        "created_at": "2026-11-04 18:00:00", "content": "普通一句",
        "is_self": 0, "kind": "text", "quote_user": "", "media_path": "",
    }]
    assert ports._rows(raw, {})[0].sender_id == ""


def test_find_orders_matches_by_the_row_sender_id():
    rows = [Kick(1, "在银月的测试群里说一句：明天上午十点开会", sender_id="wxid_owner")]
    got = direct_send.find_orders(rows, chat_id="wxid_owner", owners={"wxid_owner"})
    assert len(got) == 1
    assert got[0].body == "明天上午十点开会"


def test_find_orders_still_works_without_sender_id_but_with_the_lookup_map():
    rows = [Kick(2, "在银月的测试群里说一句：下午三点倒垃圾")]
    got = direct_send.find_orders(
        rows, chat_id="wxid_owner", owners={"wxid_owner"},
        sender_ids_map={"2": "wxid_owner"})
    assert len(got) == 1


def test_find_orders_refuses_a_display_name_without_any_id():
    rows = [Kick(3, "在银月的测试群里说一句：别忘了")]
    assert direct_send.find_orders(rows, chat_id="wxid_owner", owners={"wxid_owner"}) == ()


def _claim_conn(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("create table outbound_queue (id integer primary key, why text)")
    con.executemany("insert into outbound_queue (id, why) values (?, ?)", rows)
    con.commit()
    return con


def test_claimed_msg_ids_reads_the_msg_prefix(monkeypatch):
    con = _claim_conn([(261, "msg 5246297189817911481"), (260, "msg 6460343961411027685"),
                       (262, ""), (263, "回读追认 5 次都没找到这条")])
    monkeypatch.setattr(desk.W, "conn", lambda: con)
    assert desk._claimed_msg_ids() == {"5246297189817911481", "6460343961411027685"}


def test_claimed_msg_ids_survives_a_broken_query(monkeypatch):
    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(desk.W, "conn", boom)
    assert desk._claimed_msg_ids() == set()


def _fake_account_db(root, ids_with_time):
    d = os.path.join(root, "data", "platforms", "wechat", "wxid_abc")
    os.makedirs(d, exist_ok=True)
    db = os.path.join(d, "account.db")
    con = sqlite3.connect(db)
    con.execute("create table messages (id integer primary key, is_self integer,"
                " content text, created_at text)")
    for i, _ in ids_with_time:
        con.execute("insert into messages (id, is_self, content, created_at)"
                    " values (?, 1, ?, datetime('now','localtime'))", (i, "你好"))
    con.commit()
    con.close()
    return db


def test_self_proof_without_operation_binding_never_claims_same_text(tmp_path, monkeypatch):
    from core.execution.gui import wechat_send
    _fake_account_db(str(tmp_path), [(101, 0), (102, 0)])
    monkeypatch.chdir(str(tmp_path))
    newest = wechat_send._gui_self_proof("你好", wait_s=0)
    assert newest == ""
    older = wechat_send._gui_self_proof("你好", wait_s=0, exclude={newest})
    assert older == ""
    assert wechat_send._gui_self_proof("你好", wait_s=0, exclude={"101", "102"}) == ""
