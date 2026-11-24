"""「sent 但没有凭据」怎么办：回写 unverified，追认失败重发一跳。

2026-11-04 主人拍板两件（m03414 逐字：「历史 23 条 sent 但没凭据的行要不要回写成
unverified————要 / 追认失败之后要不要重发（现在只抄送告知）————要」）。
这里只测行为：造真 sqlite 的 `outbound_queue` 与 `messages`，跑真函数。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from db import world_db
from loop import receipts


def _world(tmp_path):
    path = tmp_path / "world.db"
    c = sqlite3.connect(str(path))
    c.execute(
        "create table outbound_queue (id integer primary key, chat_id text, state text,"
        " ack text, why text, payload text, updated_ts real, platform text, turn_id text, account_id text default 'a')")
    c.commit()
    return str(path), c


def _acct(tmp_path):
    path = tmp_path / "data/platforms/wechat/a/account.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute("create table messages (id text primary key, chat_id text, content text,"
              " is_self integer, created_at text, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
    c.execute("insert into messages values ('900', 'c1', '我去问一下场地', 1, '2026-11-04 10:00:00')")
    c.commit()
    c.close()
    return str(path)


def _row(c, qid, *, state="sent", ack="confirmed", why="", text="说一句话"):
    c.execute("insert into outbound_queue (id, chat_id, state, ack, why, payload, updated_ts,"
              " platform, turn_id) values (?,?,?,?,?,?,?,?,?)",
              (qid, "c1", state, ack, why, json.dumps({"kind": "text", "text": text}, ensure_ascii=False),
               1793800000.0, "wechat", "t1"))
    c.commit()


def _states(c):
    return dict(c.execute("select id, state from outbound_queue order by id").fetchall())


@pytest.fixture()
def world(tmp_path):
    path, c = _world(tmp_path)
    yield c
    c.close()


@pytest.fixture()
def acct(tmp_path):
    return _acct(tmp_path)


def test_a_dry_run_lists_but_writes_nothing(world, acct):
    _row(world, 1, why="msg 900", text="我去问一下场地")          # 有凭据
    _row(world, 2, ack="pending", why="")  # 没凭据
    out = receipts.mark_unverified(conn=world, db=acct, dry_run=True)
    assert out["seen"] == 2
    assert out["marked"] == [2]
    assert out["kept"] == [1]
    assert out["dry_run"] is True
    assert _states(world) == {1: "sent", 2: "sent"}


def test_only_the_unprovable_rows_are_marked(world, acct):
    _row(world, 1, why="msg 900", text="我去问一下场地")
    _row(world, 2, ack="pending", why="")
    _row(world, 3, why="msg 999")           # 声称发过，微信里没有
    out = receipts.mark_unverified(conn=world, db=acct, dry_run=False)
    assert out["marked"] == [2, 3] and out["kept"] == [1]
    assert _states(world) == {1: "sent", 2: "unverified", 3: "unverified"}


def test_the_original_why_is_kept(world, acct):
    _row(world, 3, why="msg 999")
    receipts.mark_unverified(conn=world, db=acct, dry_run=False, note="2026-11-04")
    why = world.execute("select why from outbound_queue where id = 3").fetchone()[0]
    assert why.startswith("msg 999｜回读不到凭据")
    assert "2026-11-04" in why and "999" in why


def test_a_second_run_does_not_touch_marked_rows(world, acct):
    _row(world, 2, ack="pending", why="")
    first = receipts.mark_unverified(conn=world, db=acct, dry_run=False)
    second = receipts.mark_unverified(conn=world, db=acct, dry_run=False)
    assert first["seen"] == 1 and first["marked"] == [2]
    assert second["seen"] == 0 and second["marked"] == []
    assert _states(world)[2] == "unverified"


def test_resend_puts_one_marked_jump_into_the_queue(world, acct, monkeypatch):
    _row(world, 5, ack="pending", why="", text="场地定了吗")
    sent = []

    def _enqueue(group_id, chat_id, items, **kw):
        sent.append({"gid": group_id, "chat": chat_id, "items": list(items), "kw": dict(kw)})
        return [77]

    monkeypatch.setattr(world_db, "enqueue", _enqueue)
    out = receipts.resend(5, conn=world, now=1000.0)
    assert out == {"ok": False, "why": "delivery_uncertain", "old": 5}
    assert sent == []


def test_the_old_row_is_marked_as_resent(world, acct, monkeypatch):
    _row(world, 5, why="")
    monkeypatch.setattr(world_db, "enqueue", lambda *a, **k: [77])
    receipts.resend(5, conn=world, now=1000.0)
    state, why = world.execute("select state, why from outbound_queue where id = 5").fetchone()
    assert state == "sent"
    assert why == ""


def test_a_resent_row_is_never_resent_again(world, acct, monkeypatch):
    _row(world, 6, why="")
    world.execute("update outbound_queue set payload = ? where id = 6",
                  (json.dumps({"kind": "text", "text": "再来一次", "retry_of": 5}, ensure_ascii=False),))
    world.commit()
    called = []
    monkeypatch.setattr(world_db, "enqueue", lambda *a, **k: called.append(1) or [1])
    out = receipts.resend(6, conn=world, now=1000.0)
    assert out == {"ok": False, "why": "already_retried", "old": 6, "retry_of": 5}
    assert called == []


def test_nothing_is_resent_while_it_is_still_pending(world, acct, monkeypatch):
    _row(world, 7, state="pending", ack="", why="")
    monkeypatch.setattr(world_db, "enqueue", lambda *a, **k: [1])
    out = receipts.resend(7, conn=world, now=1000.0)
    assert out["ok"] is False and out["why"] == "not_settled" and out["state"] == "pending"


def test_an_empty_text_is_not_resent(world, acct, monkeypatch):
    _row(world, 8, why="", text="   ")
    monkeypatch.setattr(world_db, "enqueue", lambda *a, **k: [1])
    out = receipts.resend(8, conn=world, now=1000.0)
    assert out["ok"] is False and out["why"] == "no_text"


def test_a_missing_row_is_refused(world, acct):
    out = receipts.resend(999, conn=world, now=1000.0)
    assert out["ok"] is False and out["why"] == "not_found" and out["old"] == 999


def test_a_broken_payload_is_not_resent(world, acct, monkeypatch):
    _row(world, 9, why="")
    world.execute("update outbound_queue set payload = '{oops' where id = 9")
    world.commit()
    called = []
    monkeypatch.setattr(world_db, "enqueue", lambda *a, **k: called.append(1) or [1])
    out = receipts.resend(9, conn=world, now=1000.0)
    assert out["ok"] is False and out["why"] == "payload_unreadable"
    assert called == []


def test_a_failed_enqueue_leaves_the_old_row_alone(world, acct, monkeypatch):
    _row(world, 10, why="")

    def _boom(*a, **k):
        raise RuntimeError("队列写不进去")

    monkeypatch.setattr(world_db, "enqueue", _boom)
    out = receipts.resend(10, conn=world, now=1000.0)
    assert out["ok"] is False and out["why"] == "delivery_uncertain"
    assert _states(world)[10] == "sent"

# ---- 2026-11-05：落 failed 的补发一跳（requeue_failed）----


def _sent(monkeypatch, *, new_id=77):
    got = []

    def _enq(gid, chat, items, **kw):
        got.append({"gid": gid, "chat": chat, "items": items, "kw": kw})
        return [new_id]

    monkeypatch.setattr(world_db, "enqueue", _enq)
    return got


def test_a_failed_row_is_requeued_once(world, acct, monkeypatch):
    _row(world, 5, state="failed", ack="", why="无法激活微信窗口")
    got = _sent(monkeypatch)
    out = receipts.requeue_failed(5, conn=world, now=1000.0)
    assert out == {"ok": False, "why": "retry_policy_exhausted", "old": 5}
    assert got == []
    assert _states(world)[5] == "failed"


def test_only_failed_rows_are_requeued(world, acct, monkeypatch):
    _row(world, 7, state="sent", why="msg 900")
    got = _sent(monkeypatch)
    out = receipts.requeue_failed(7, conn=world, now=1000.0)
    assert out["ok"] is False and out["why"] == "not_failed"
    assert got == []


def test_a_requeued_row_is_never_requeued_again(world, acct, monkeypatch):
    _row(world, 8, state="failed", why="x")
    world.execute("update outbound_queue set payload = ? where id = 8",
                  (json.dumps({"kind": "text", "text": "再来一次", "retry_of": 5},
                              ensure_ascii=False),))
    world.commit()
    got = _sent(monkeypatch)
    out = receipts.requeue_failed(8, conn=world, now=1000.0)
    assert out["why"] == "already_retried" and got == []


def test_an_empty_text_is_not_requeued(world, acct, monkeypatch):
    _row(world, 9, state="failed", why="x", text="   ")
    got = _sent(monkeypatch)
    assert receipts.requeue_failed(9, conn=world)["why"] == "no_text"
    assert got == []


def test_a_missing_row_is_not_requeued(world, acct, monkeypatch):
    got = _sent(monkeypatch)
    assert receipts.requeue_failed(404, conn=world)["why"] == "not_found"
    assert got == []


def test_unreadable_local_store_does_not_rewrite_history(world, tmp_path):
    _row(world, 20, why="msg 900")
    before = world.execute("select state,ack,why from outbound_queue where id=20").fetchone()
    result = receipts.mark_unverified(conn=world, db=str(tmp_path / "missing.db"), dry_run=False)
    assert result["marked"] == []
    assert result["unresolved"] == [{"queue_id": 20, "why": "local_store_unavailable"}]
    assert world.execute("select state,ack,why from outbound_queue where id=20").fetchone() == before


def test_late_confirmation_is_not_downgraded_by_stale_check(world, acct, monkeypatch):
    _row(world, 21, ack="pending", why="")
    def verify(row, **kw):
        world.execute("update outbound_queue set ack='confirmed',why='msg 900' where id=21")
        world.commit()
        return receipts.Receipt(queue_id=21, verified=False, why="old snapshot")
    monkeypatch.setattr(receipts, "verify_row", verify)
    result = receipts.mark_unverified(conn=world, db=acct, dry_run=False)
    assert result["marked"] == []
    assert result["changed_during_check"] == [21]
    assert world.execute("select state,ack,why from outbound_queue where id=21").fetchone() == ("sent", "confirmed", "msg 900")


@pytest.fixture(autouse=True)
def explicit_source_root(tmp_path, monkeypatch):
    from db import world_db
    monkeypatch.setattr(world_db, "ROOT", str(tmp_path))
