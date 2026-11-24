"""同一句话不许发两遍（2026-11-04 真机：direct_send 与 say 各发一次，群里出现两条）。"""

import sqlite3
import time

from loop import direct_send


def _conn(rows=()):
    con = sqlite3.connect(":memory:")
    con.execute(
        "create table outbound_queue (id integer primary key, chat_id text, state text, "
        "payload text, created_ts integer, updated_ts integer)"
    )
    now = int(time.time())
    for i, (chat_id, text, state, age_s) in enumerate(rows, 1):
        con.execute(
            "insert into outbound_queue (id, chat_id, state, payload, created_ts, updated_ts) "
            "values (?, ?, ?, ?, ?, ?)",
            (i, chat_id, state, '{"kind": "text", "text": "%s"}' % text, now - age_s, now - age_s),
        )
    con.commit()
    return con


def test_a_line_that_just_went_into_the_queue_counts_as_sent():
    con = _conn([("10000000004@chatroom", "下午三点倒垃圾", "pending", 5)])
    assert direct_send.recently_sent("10000000004@chatroom", "下午三点倒垃圾", conn=con) is True


def test_a_line_sent_to_another_chat_does_not_count():
    con = _conn([("10000000001@chatroom", "下午三点倒垃圾", "sent", 5)])
    assert direct_send.recently_sent("10000000004@chatroom", "下午三点倒垃圾", conn=con) is False


def test_a_different_line_in_the_same_chat_does_not_count():
    con = _conn([("10000000004@chatroom", "明天上午十点开会", "sent", 5)])
    assert direct_send.recently_sent("10000000004@chatroom", "下午三点倒垃圾", conn=con) is False


def test_an_old_line_falls_outside_the_window():
    con = _conn([("10000000004@chatroom", "下午三点倒垃圾", "sent", 600)])
    assert direct_send.recently_sent("10000000004@chatroom", "下午三点倒垃圾", conn=con) is False


def test_a_dead_row_does_not_count():
    for state in ("failed", "expired", "skipped"):
        con = _conn([("10000000004@chatroom", "下午三点倒垃圾", state, 5)])
        assert direct_send.recently_sent("10000000004@chatroom", "下午三点倒垃圾", conn=con) is False


def test_empty_inputs_are_never_a_duplicate():
    con = _conn([("10000000004@chatroom", "下午三点倒垃圾", "sent", 5)])
    assert direct_send.recently_sent("", "下午三点倒垃圾", conn=con) is False
    assert direct_send.recently_sent("10000000004@chatroom", "   ", conn=con) is False
