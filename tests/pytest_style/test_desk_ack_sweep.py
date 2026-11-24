"""sent 不等于到了：追认线程是进程内的 daemon，desk 一重启 ack 就永远停在 pending。
这里只做行为断言：给一个假的世界（内存 sqlite），看它到底改不改、怎么改。
"""
import sqlite3
import time
from types import SimpleNamespace


def _world(*, stale: bool = True):
    """最小可用的假库：只建 sweep 会碰的两张表。"""
    c = sqlite3.connect(":memory:")
    c.execute("create table outbound_queue (id integer primary key, state text, ack text, "
              "not_after real, why text, attempts integer default 1)")
    c.execute("create table outbound_attempt (id integer primary key, queue_id integer, "
              "ack text, why text, attempt_no integer default 1)")
    age = 3600.0 if stale else 3600.0
    c.execute("insert into outbound_queue(id,state,ack,not_after,why) values (1, 'sent', 'pending', ?, '')",
              (time.time() - age,))
    c.execute("insert into outbound_attempt(id,queue_id,ack,why) values (1, 1, 'pending', '')")
    c.commit()
    from db.world_db import mark_confirmation_unknown
    return SimpleNamespace(conn=lambda: c, mark_confirmation_unknown=mark_confirmation_unknown), c


def test_stale_pending_row_becomes_unconfirmed(monkeypatch):
    from loop import desk
    fake, c = _world()
    monkeypatch.setattr(desk, "W", fake)
    assert desk.sweep_acks(max_age_s=60) == 1
    row = c.execute("select ack, why from outbound_queue where id=1").fetchone()
    assert row[0] == "unconfirmed"
    assert "追认" in row[1]
    assert c.execute("select ack from outbound_attempt where queue_id=1").fetchone()[0] == "unconfirmed"


def test_it_does_not_touch_a_row_inside_the_window(monkeypatch):
    from loop import desk
    fake, c = _world()
    c.execute("update outbound_queue set not_after=? where id=1", (time.time() + 600,))
    c.commit()
    monkeypatch.setattr(desk, "W", fake)
    assert desk.sweep_acks(max_age_s=60) == 0
    assert c.execute("select ack from outbound_queue where id=1").fetchone()[0] == "pending"


def test_second_run_sweeps_nothing(monkeypatch):
    """幂等：第一次改成 unconfirmed，第二次不该再扫到它。"""
    from loop import desk
    fake, c = _world()
    monkeypatch.setattr(desk, "W", fake)
    assert desk.sweep_acks(max_age_s=60) == 1
    assert desk.sweep_acks(max_age_s=60) == 0
