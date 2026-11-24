"""重启时只重排「证据表明一个字都没打」的那几条。

2026-11-22 16:06：写好的一条隐私提醒在标题闸中止（没打字），旧代码记成 unverified 永不重试。
这里锁住重排的白名单、时间窗和状态约束。
"""
from __future__ import annotations

import sqlite3
import time

from db import world_db as W


def _make(tmp_path):
    p = tmp_path / "world.db"
    c = sqlite3.connect(str(p))
    c.execute(
        "create table outbound_queue (id integer primary key, state text, why text,"
        " updated_ts real, next_at real, attempts integer default 1)"
    )
    now = time.time()
    rows = [
        (1, "unverified", "打开的会话不是目标（顶部是「」），已中止，没打字", now - 60),
        (2, "unverified", "微信未连接，请确认微信PC客户端已打开并登录", now - 60),
        (3, "unverified", "输入框里已经有字（可能是上次没发出去的），我没有动它", now - 60),
        (4, "unverified", "别的原因", now - 60),
        (5, "unverified", "没打字", now - 8 * 3600),
        (6, "sent", "没打字", now - 60),
    ]
    c.executemany("insert into outbound_queue(id,state,why,updated_ts) values (?,?,?,?)", rows)
    c.commit()
    c.close()
    return p, now


def test_only_safe_recent_unverified_rows_are_requeued(monkeypatch, tmp_path):
    p, now = _make(tmp_path)
    monkeypatch.setattr(W, "conn", lambda: sqlite3.connect(str(p)))
    ids = W.requeue_not_executed(since_ts=now - 6 * 3600, now=now)
    assert sorted(ids) == [1, 2, 3]

    c = sqlite3.connect(str(p))
    states = dict(c.execute("select id,state from outbound_queue").fetchall())
    c.close()
    assert states[1] == "pending" and states[2] == "pending" and states[3] == "pending"
    assert states[4] == "unverified"   # 原因不在白名单
    assert states[5] == "unverified"   # 太旧
    assert states[6] == "sent"         # 已发出


def test_nothing_to_do_returns_empty(monkeypatch, tmp_path):
    p, now = _make(tmp_path)
    monkeypatch.setattr(W, "conn", lambda: sqlite3.connect(str(p)))
    assert W.requeue_not_executed(since_ts=now + 10, now=now) == []
