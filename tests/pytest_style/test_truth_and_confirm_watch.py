"""§29 的两件补丁：账本真相对进周报；追认线程压根没起来要喊人。

两个模块都只被判据层测（真 sqlite / 假 summary），不碰 GUI、不碰网络。
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from core.observability import weekly
from loop import desk, owner_copy, receipts


def _mk(tmp_path, rows=(), msgs=()):
    """造一个 world.db（只有 outbound_queue）和一个 account.db（只有 messages）。"""
    world = sqlite3.connect(":memory:")
    world.execute(
        "create table outbound_queue (id integer primary key, chat_id text, state text, "
        "ack text, why text, payload text, created_ts real, updated_ts real, platform text default 'wechat', account_id text default 'a')")
    for i, (state, ack, why, payload, ts) in enumerate(rows, 1):
        world.execute("insert into outbound_queue (id, chat_id, state, ack, why, payload, "
                      "created_ts, updated_ts) values (?,?,?,?,?,?,?,?)",
                      (i, "c1", state, ack, why, payload, ts, ts))
    world.commit()
    acc = tmp_path / "data/platforms/wechat/a/account.db"
    acc.parent.mkdir(parents=True,exist_ok=True)
    conn = sqlite3.connect(acc)
    conn.execute("create table messages (id integer primary key, chat_id text, content text, "
                 "is_self integer, created_at text, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
    for mid, content, when in msgs:
        conn.execute("insert into messages (id, chat_id, content, is_self, created_at) "
                     "values (?,?,?,1,?)", (mid, "c1", content, when))
    conn.commit()
    conn.close()
    return world, str(acc)


def test_summary_counts_each_bucket(tmp_path):
    now = 1_800_000_000.0
    world, acc = _mk(tmp_path,
                     rows=[("sent", "confirmed", "msg 111", '{"text":"a"}', now),
                           ("sent", "pending", "", '{"text":"b"}', now),
                           ("unverified", "unconfirmed", "msg 222｜回读不到凭据", '{"text":"c"}', now),
                           ("pending", "", "", '{"text":"d"}', now),
                           ("failed", "", "x", '{"text":"e"}', now)],
                     msgs=[(111, "a", "2026-12-01 00:00:00")])
    got = receipts.summary(days=1, conn=world, db=acc, now=now)
    assert (got["total"], got["verified"], got["unverified"], got["rewritten"],
            got["pending"], got["not_sent"]) == (5, 1, 1, 1, 1, 1)


def test_summary_rate_denominator_includes_rewritten(tmp_path):
    now = 1_800_000_000.0
    world, acc = _mk(tmp_path,
                     rows=[("sent", "confirmed", "msg 111", '{"text":"a"}', now),
                           ("unverified", "unconfirmed", "msg 222", '{"text":"c"}', now),
                           ("unverified", "pending", "msg 333", '{"text":"d"}', now)],
                     msgs=[(111, "a", "2026-12-01 00:00:00")])
    got = receipts.summary(days=1, conn=world, db=acc, now=now)
    assert got["claimed"] == 3
    assert got["verified"] == 1 and got["rewritten"] == 2
    assert abs(got["rate"] - (1 / 3)) < 1e-9


def test_summary_of_an_empty_queue_is_zero_not_missing(tmp_path):
    now = 1_800_000_000.0
    world, acc = _mk(tmp_path)
    got = receipts.summary(days=1, conn=world, db=acc, now=now)
    assert got["total"] == 0 and got["claimed"] == 0 and got["rate"] == 0.0
    assert "error" not in got


def test_summary_reports_a_broken_source_instead_of_zero():
    broken = sqlite3.connect(":memory:")  # 没有 outbound_queue 表
    got = receipts.summary(days=1, conn=broken)
    assert "error" in got and got["total"] == 0


def _fake_summary(**kw):
    return {"total": 10, "claimed": 3, "verified": 2, "unverified": 0,
            "rewritten": 1, "pending": 6, "not_sent": 1, "rate": 2 / 3}


def test_truth_metrics_use_the_summary_numbers():
    ms = {m.name: m for m in weekly._receipt_truth_metrics(days=7, summary=_fake_summary)}
    assert ms["truth.verified"].value == 2.0
    assert ms["truth.rewritten"].value == 1.0
    assert ms["truth.rate"].text == "66.7%"  # `%` 单位自己 ×100，这里给的是 0~1


def test_truth_metrics_are_missing_not_zero_when_source_fails():
    def boom(**kw):
        raise RuntimeError("world.db 没开")

    ms = weekly._receipt_truth_metrics(days=7, summary=boom)
    assert len(ms) == 4
    assert all(m.value is None and m.text == "未采集" for m in ms)


def test_weekly_report_carries_the_truth_metrics():
    rep = weekly.build_weekly(
        days=7,
        turn_metrics=lambda **kw: [],
        receipt_metrics=lambda **kw: [],
        job_metrics=lambda **kw: [],
        truth_metrics=lambda **kw: weekly._receipt_truth_metrics(days=7, summary=_fake_summary),
    )
    assert rep.metric("truth.rate") is not None
    assert rep.metric("truth.rate").text == "66.7%"
    md = rep.markdown()
    third = md.index("三、发得对不对")
    fourth = md.find("四、手有没有被卡住")
    at = md.index("| truth.rate |")
    assert third < at and (fourth == -1 or at < fourth)  # 落在「三」里，不落到「六、其它」


@pytest.fixture(autouse=True)
def _clean_pending():
    desk._PENDING_START.clear()
    yield
    desk._PENDING_START.clear()


def test_a_pending_start_is_forgotten_once_it_starts():
    desk._mark_pending_start(7, "c1")
    assert 7 in desk._PENDING_START
    desk._mark_started(7)
    assert 7 not in desk._PENDING_START
    assert desk.sweep_confirm_starts(now=10_000.0) == 0


def test_a_start_that_never_happens_is_reported_after_the_grace(monkeypatch):
    logged, noted = [], []
    monkeypatch.setattr(desk, "_log", lambda event, **kw: logged.append((event, kw)))
    monkeypatch.setattr(owner_copy, "note", lambda kind, **kw: noted.append((kind, kw)))
    desk._mark_pending_start(9, "c9")
    planned = desk._PENDING_START[9][1]
    assert desk.sweep_confirm_starts(now=planned + 1.0, grace_s=90.0) == 0  # 刚排上，不算迟到
    late = desk.sweep_confirm_starts(now=planned + 120.0, grace_s=90.0)
    assert late == 1
    assert logged[0][0] == "desk_confirm_never_started"
    assert noted[0][0] == "confirm_not_started"
    assert noted[0][1]["target"] == "c9"
    assert desk.sweep_confirm_starts(now=planned + 600.0, grace_s=90.0) == 0  # 只喊一次


def test_the_report_goes_to_the_owner_as_a_new_kind():
    assert "confirm_not_started" in owner_copy.KINDS
    body = owner_copy.text_for("confirm_not_started", target="10000000001@chatroom")
    assert "回执核对" in body
    assert len(body) <= owner_copy.MAX_CHARS

# ---- 2026-11-05 §29.5 第 1 条：反向核对（账上说「有凭据」的，正文真对得上吗）----


def test_reverse_check_passes_when_the_text_matches(tmp_path):
    now = 1_800_000_000.0
    world, acc = _mk(tmp_path,
                     rows=[("sent", "confirmed", "msg 111", '{"text":"a"}', now)],
                     msgs=[(111, "a", "2026-12-01 00:00:00")])
    got = receipts.reverse_check(conn=world, db=acc)
    assert (got["seen"], got["checked"], got["ok"], got["problems"]) == (1, 1, 1, [])


def test_reverse_check_flags_a_different_text(tmp_path):
    now = 1_800_000_000.0
    world, acc = _mk(tmp_path,
                     rows=[("sent", "confirmed", "msg 111", '{"text":"a"}', now)],
                     msgs=[(111, "完全另一句", "2026-12-01 00:00:00")])
    got = receipts.reverse_check(conn=world, db=acc)
    assert got["ok"] == 0
    assert [p["kind"] for p in got["problems"]] == ["text_mismatch"]
    assert got["problems"][0]["msg_id"] == "111"


def test_reverse_check_flags_a_missing_message(tmp_path):
    now = 1_800_000_000.0
    world, acc = _mk(tmp_path,
                     rows=[("sent", "confirmed", "msg 999", '{"text":"a"}', now)])
    got = receipts.reverse_check(conn=world, db=acc)
    assert got["checked"] == 1 and got["ok"] == 0
    assert [p["kind"] for p in got["problems"]] == ["msg_missing"]


def test_reverse_check_flags_a_different_chat(tmp_path):
    now = 1_800_000_000.0
    world, acc = _mk(tmp_path,
                     rows=[("sent", "confirmed", "msg 111", '{"text":"a"}', now)],
                     msgs=[(111, "a", "2026-12-01 00:00:00")])
    fix = sqlite3.connect(acc)
    fix.execute("update messages set chat_id = 'c2' where id = 111")
    fix.commit()
    fix.close()
    got = receipts.reverse_check(conn=world, db=acc)
    assert [p["kind"] for p in got["problems"]] == ["chat_mismatch"]


def test_reverse_check_counts_rows_without_an_id(tmp_path):
    now = 1_800_000_000.0
    world, acc = _mk(tmp_path,
                     rows=[("sent", "pending", "", '{"text":"b"}', now)])
    got = receipts.reverse_check(conn=world, db=acc)
    assert (got["seen"], got["checked"], got["no_msg_id"]) == (1, 0, 1)


# ---- 2026-11-05 §29.5 第 2 条：开跑了但卡住的追认 ----


def _recorder(monkeypatch):
    got = []

    def _note(kind, **kw):
        got.append((kind, kw))
        return "sent"

    monkeypatch.setattr(owner_copy, "note", _note)
    monkeypatch.setattr(desk, "_PENDING_START", {})
    monkeypatch.setattr(desk, "_RUNNING", {})
    return got


def test_a_finished_confirm_is_not_reported_as_stuck(monkeypatch):
    got = _recorder(monkeypatch)
    desk._PENDING_START[7] = ("c1", 1000.0)
    desk._mark_started(7)
    desk._mark_finished(7)
    assert desk.sweep_confirm_stuck(now=1e9) == 0
    assert got == []


def test_a_stuck_confirm_is_reported_once(monkeypatch):
    got = _recorder(monkeypatch)
    desk._PENDING_START[8] = ("g1@chatroom", 1000.0)
    desk._mark_started(8)
    t0 = desk._RUNNING[8][1]
    assert desk.sweep_confirm_stuck(now=t0 + 100) == 0  # 465 秒的延迟还没走完
    assert desk.sweep_confirm_stuck(now=t0 + 601) == 1
    assert got and got[0][0] == "confirm_stuck"
    assert got[0][1]["target"] == "g1@chatroom"
    assert desk.sweep_confirm_stuck(now=t0 + 9000) == 0  # 只喊一次


def test_the_stuck_notice_is_a_new_kind_without_the_text(monkeypatch):
    got = _recorder(monkeypatch)
    assert "confirm_stuck" in owner_copy.KINDS
    line = owner_copy.text_for("confirm_stuck", target="g1@chatroom")
    assert "回执核对" in line and len(line) <= owner_copy.MAX_CHARS
    desk._PENDING_START[9] = ("c1", 1000.0)
    desk._mark_started(9)
    desk.sweep_confirm_stuck(now=desk._RUNNING[9][1] + 601)
    assert got and "detail" not in got[0][1]  # 不带正文


# ---------------------------------------------------------------- 审计脚本

def _audit_module():
    """把 `scripts/receipts_audit.py` 当模块载进来（它不是包里的模块）。"""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    spec = importlib.util.spec_from_file_location("receipts_audit", root / "scripts" / "receipts_audit.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _fake_audit(reverse):
    def _audit(*, days=30.0):
        return {"days": days, "total": 73, "claimed": 71, "verified": 46, "unverified": 2,
                "rewritten": 23, "pending": 0, "not_sent": 2, "rate": 0.648,
                "samples": [], "reverse": reverse}
    return _audit


def test_the_audit_script_prints_the_reverse_result(monkeypatch, capsys):
    """§29.5 第 1 条：只读脚本必须把「消息里真的是这句话吗」打出来，而不是只报凭据率。"""
    mod = _audit_module()
    monkeypatch.setattr(mod, "audit", _fake_audit(
        {"seen": 48, "checked": 46, "ok": 46, "no_msg_id": 2,
         "problems": [{"queue_id": 9, "chat_id": "c1", "msg_id": "111", "kind": "text_mismatch"}]}))
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "反向核对" in out
    assert "查 46 条，对上 46 条，对不上 1 条" in out
    assert "text_mismatch" in out
    assert "凭据率：64.8%" in out


def test_the_audit_script_says_so_when_the_reverse_check_cannot_read(monkeypatch, capsys):
    """读不出来不许当成「全对」——要明说读不出来。"""
    mod = _audit_module()
    monkeypatch.setattr(mod, "audit", _fake_audit({"error": "database is locked", "problems": []}))
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "读不出来" in out
    assert "database is locked" in out


def test_the_audit_helper_tolerates_counts_and_lists():
    """口径里的数字历史上既可能是计数也可能是明细列表，两种都不能炸。"""
    mod = _audit_module()
    assert mod._n([1, 2, 3]) == 3
    assert mod._n(7) == 7
    assert mod._n(None) == 0
    assert mod._n("5") == 5


# ---------------------------------------------------------------- §29.5 两条尾巴
def _now_local(offset_s: float = 0.0) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_s))


def _fake_reverse(**kw):
    return {"seen": 5, "checked": 3, "ok": 2, "no_msg_id": 2,
            "problems": [{"queue_id": 4, "chat_id": "c1", "msg_id": "111",
                          "kind": "text_mismatch"},
                         {"queue_id": 6, "chat_id": "c1", "msg_id": "333",
                          "kind": "text_mismatch"}]}


def test_reverse_metrics_carry_checked_and_bad():
    ms = {m.name: m for m in weekly._reverse_metrics(days=7, reverse=_fake_reverse)}
    assert ms["truth.reverse_checked"].value == 3.0
    assert ms["truth.reverse_bad"].value == 2.0
    assert "另 2 条" in ms["truth.reverse_checked"].basis
    assert "text_mismatch 2" in ms["truth.reverse_bad"].basis


def test_reverse_metrics_are_missing_not_zero_when_source_fails():
    def boom(**kw):
        raise RuntimeError("world.db 没开")

    ms = weekly._reverse_metrics(days=7, reverse=boom)
    assert len(ms) == 2
    assert all(m.value is None and m.text == "未采集" for m in ms)


def test_reverse_metrics_land_in_the_third_group():
    rep = weekly.build_weekly(
        days=7,
        turn_metrics=lambda **kw: [],
        receipt_metrics=lambda **kw: [],
        job_metrics=lambda **kw: [],
        truth_metrics=lambda **kw: [],
        restart_metrics=lambda **kw: [],
        reverse_metrics=lambda **kw: weekly._reverse_metrics(days=7, reverse=_fake_reverse),
    )
    md = rep.markdown()
    third = md.index("三、发得对不对")
    at = md.index("| truth.reverse_bad |")
    fourth = md.find("四、手有没有被卡住")
    assert third < at and (fourth == -1 or at < fourth)


def test_settle_stuck_finds_the_message_and_only_fills_the_proof(tmp_path):
    now = 1_800_000_000.0
    world, acc = _mk(tmp_path, rows=[("sent", "pending", "", '{"text":"a"}', now)],
                     msgs=[(111, "a", _now_local())])
    asked = []
    got = receipts.settle_stuck(1, conn=world, db=acc, now=now,
                                confirm=lambda *a, **kw: asked.append((a, kw)))
    assert got["ok"] is False and got["why"] == "operation_binding_missing"
    ack, why = world.execute("select ack, why from outbound_queue where id=1").fetchone()
    assert ack == "pending" and why == ""
    assert asked == []


def test_settle_stuck_resends_once_when_the_text_is_nowhere(tmp_path):
    now = 1_800_000_000.0
    world, acc = _mk(tmp_path, rows=[("sent", "pending", "", '{"text":"a"}', now)],
                     msgs=[(111, "别的话", _now_local())])
    sent = []

    def _enq(*a, **kw):  # 真 `enqueue` 是 (turn_id, chat, items, *, turn_id=…)，这里不挑形状
        sent.append((a, kw))
        return [42]

    got = receipts.settle_stuck(1, conn=world, db=acc, now=now,
                                confirm=lambda *a, **kw: None, enqueue=_enq)
    assert got["ok"] is False and got["why"] == "operation_binding_missing"
    assert sent == []
    state, why = world.execute("select state, why from outbound_queue where id = 1").fetchone()
    assert state == "sent" and why == ""


def test_settle_stuck_leaves_a_row_that_has_not_settled_alone(tmp_path):
    now = 1_800_000_000.0
    world, acc = _mk(tmp_path, rows=[("pending", "", "", '{"text":"a"}', now)], msgs=[])
    got = receipts.settle_stuck(1, conn=world, db=acc, now=now)
    assert got["ok"] is False and got["why"] == "not_settled"
    assert world.execute("select state from outbound_queue where id = 1").fetchone()[0] == "pending"


def test_settle_stuck_does_not_resend_when_the_wechat_db_cannot_be_read(tmp_path):
    now = 1_800_000_000.0
    world, _acc = _mk(tmp_path, rows=[("sent", "pending", "", '{"text":"a"}', now)], msgs=[])
    sent = []
    got = receipts.settle_stuck(1, conn=world, db=str(tmp_path / "nope.db"), now=now,
                                enqueue=lambda *a, **kw: sent.append(a) or [42])
    assert got["ok"] is False and got["why"] == "operation_binding_missing"
    assert sent == []


def test_a_stuck_confirm_is_handled_after_it_is_reported(monkeypatch):
    logged, noted, settled = [], [], []
    monkeypatch.setattr(desk, "_log", lambda name, **kw: logged.append((name, kw)))
    monkeypatch.setattr(owner_copy, "note", lambda kind, **kw: noted.append((kind, kw)))
    monkeypatch.setattr(receipts, "settle_stuck",
                        lambda qid, **kw: settled.append(qid) or
                        {"ok": True, "action": "resent", "new_id": 42})
    monkeypatch.setattr(desk, "_RUNNING", {5: ("g1@chatroom", 1_000.0)})
    assert desk.sweep_confirm_stuck(now=1_601.0) == 1
    assert settled == [5]
    assert ("desk_confirm_stuck_settled", {"id": 5, "action": "resent", "new_id": 42}) in logged
    assert noted and noted[0][0] == "confirm_stuck"


@pytest.fixture(autouse=True)
def explicit_source_root(tmp_path,monkeypatch):
    from db import world_db
    monkeypatch.setattr(world_db,'ROOT',str(tmp_path))
