"""主人抄送：只报事实、只认一个主人、不许刷屏、自己绝不再走一遍内容闸。"""

from __future__ import annotations

import pytest

from loop import owner_copy as oc


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """每个用例都从"只有一个主人、节流表是空的"开始。

    注意打桩打的是 `direct_send.owner_ids` 而不是 `oc.owner_chat` —— 后者会把
    「认不出/认出多个就不发」这条判据整个短路掉，那两条用例就再也测不到真函数
    （2026-11-04 第一次写完就是这么假绿的）。
    """
    from loop import direct_send as ds

    oc.reset()
    monkeypatch.setattr(ds, "owner_ids", lambda: {"wxid_owner"})
    yield
    oc.reset()


def _sent(monkeypatch):
    box: list = []

    def _enqueue(group_id, chat_id, items, **kw):
        box.append((group_id, chat_id, items, kw))
        return [101]

    monkeypatch.setattr("db.world_db.enqueue", _enqueue)
    return box


def test_an_unknown_kind_is_never_sent(monkeypatch):
    box = _sent(monkeypatch)
    got = oc.note("whatever", target="c1")
    assert got == {"sent": False, "why": "unknown_kind"}
    assert box == []


def test_without_a_known_owner_nothing_is_sent(monkeypatch):
    box = _sent(monkeypatch)
    monkeypatch.setattr(oc, "owner_chat", lambda: "")
    got = oc.note("content_blocked", target="c1", detail="content:money")
    assert got["sent"] is False and got["why"] == "no_owner"
    assert box == []


def test_an_ambiguous_owner_list_is_not_guessed(monkeypatch):
    from loop import direct_send as ds

    monkeypatch.setattr(ds, "owner_ids", lambda: {"wxid_a", "wxid_b"})
    assert oc.owner_chat() == ""


def test_a_single_owner_is_used(monkeypatch):
    from loop import direct_send as ds

    monkeypatch.setattr(ds, "owner_ids", lambda: {"wxid_seed01"})
    assert oc.owner_chat() == "wxid_seed01"


def test_the_blocked_text_itself_is_never_quoted(monkeypatch):
    _sent(monkeypatch)
    body = oc.text_for("content_blocked", target="10000000001@chatroom", detail="content:money")
    assert "钱" in body or "money" in body
    assert "转账" not in body and "¥" not in body
    assert "10000000001@chatroom" in body


def test_two_reports_within_a_minute_are_throttled(monkeypatch):
    box = _sent(monkeypatch)
    first = oc.note("content_blocked", target="c1", detail="content:money", now=1000.0)
    second = oc.note("confirm_timeout", target="c2", now=1000.0 + oc.GAP_S / 2)
    third = oc.note("confirm_timeout", target="c2", now=1000.0 + oc.GAP_S)
    assert first["sent"] is True
    assert second == {"sent": False, "why": "too_soon"}
    assert third["sent"] is True
    assert len(box) == 2


def test_the_same_kind_for_the_same_chat_waits_ten_minutes(monkeypatch):
    box = _sent(monkeypatch)
    assert oc.note("job_stuck", target="c1", detail="订水", now=2000.0)["sent"] is True
    assert oc.note("job_stuck", target="c1", detail="订水", now=2000.0 + oc.SAME_KIND_GAP_S / 2)["why"] == "same_kind_recent"
    assert oc.note("job_stuck", target="c1", detail="订水", now=2000.0 + oc.SAME_KIND_GAP_S)["sent"] is True
    assert len(box) == 2


def test_a_dry_run_does_not_consume_the_window(monkeypatch):
    box = _sent(monkeypatch)
    dry = oc.note("content_blocked", target="c1", detail="content:money", dry_run=True)
    real = oc.note("content_blocked", target="c1", detail="content:money", now=1000.0)
    assert dry["why"] == "dry_run" and dry["chat_id"] == "wxid_owner"
    assert real["sent"] is True
    assert len(box) == 1


def test_it_lands_in_the_owner_private_queue(monkeypatch):
    box = _sent(monkeypatch)
    got = oc.note("order_unverified", target="10000000004@chatroom", now=3000.0)
    (gid, chat, items, kw), = box
    assert chat == "wxid_owner"
    assert gid.startswith("owner:order_unverified:")
    assert kw.get("turn_id") == "owner_copy"
    assert len(items) == 1 and items[0]["kind"] == "text"
    assert "10000000004@chatroom" in items[0]["text"]
    assert got["queue_ids"] == [101]


def test_a_broken_queue_reports_instead_of_raising(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("db.world_db.enqueue", _boom)
    got = oc.note("confirm_timeout", target="c1", now=4000.0)
    assert got["sent"] is False and got["why"] == "enqueue_failed"


def test_no_reentry_while_it_is_reporting(monkeypatch):
    box = _sent(monkeypatch)
    oc._IN_NOTE = True
    try:
        got = oc.note("content_blocked", target="c1", detail="content:money", now=5000.0)
    finally:
        oc._IN_NOTE = False
    assert got == {"sent": False, "why": "busy"}
    assert box == []


def test_every_line_stays_one_short_sentence(monkeypatch):
    _sent(monkeypatch)
    long_detail = "订水这件事" * 40
    for kind in oc.KINDS:
        body = oc.text_for(kind, target="10000000004@chatroom", detail=long_detail)
        assert 0 < len(body) <= oc.MAX_CHARS
        assert body.count("\n") == 0


# ---- 下面三条是"接线"测试：四个出事点真的会去拍 owner_copy 的门 ----

def test_a_blocked_say_tells_the_owner(monkeypatch):
    """`ports.say` 被内容闸拦下 → 抄送，且正文一个字都不带。"""
    import asyncio

    from loop import outbound_gate, ports

    calls: list = []
    monkeypatch.setattr(outbound_gate, "check", lambda chat, text, reason="reply", now=None: (False, "content:money"))
    monkeypatch.setattr(oc, "note", lambda *a, **kw: calls.append((a, kw)) or {"sent": True})
    monkeypatch.setattr("loop.speak_policy.allowed", lambda *a, **kw: (True, "ok"))
    # 真 `resolve_chat_id("c1")` 会把它映射成真库里某个 wxid（测试环境的尾巴），
    # 这里只关心"被拦之后抄送报的是哪个会话"，所以钉成恒等。
    monkeypatch.setattr(ports, "resolve_chat_id", lambda c: str(c or ""))
    res = asyncio.run(ports.say("我这就给你转 200 块钱", dry_run=False, chat_id="c1"))
    assert res.data["ack"] == "skipped" and res.data["why"] == "content:money"
    assert calls and calls[0][0] == ("content_blocked",)
    assert calls[0][1]["target"] == "c1"
    assert calls[0][1]["detail"] == "content:money"
    assert "转 200" not in str(calls[0])


def test_a_job_that_runs_out_of_budget_tells_the_owner(monkeypatch):
    from loop import job_bridge

    class _Job:
        job_id = "j1"
        goal = "往「银月的测试群」说一句：今天下雨记得带伞"
        step_n = 1
        steps = [1, 2]

    class _Store:
        def open_jobs(self, chat_id):
            return [_Job()]

    class _Res:
        stop = "budget"
        progressed = False
        job = _Job()

    calls: list = []
    monkeypatch.setattr(oc, "note", lambda *a, **kw: calls.append((a, kw)) or {"sent": True})
    monkeypatch.setattr("core.jobs.runner.advance", lambda job, **kw: _Res())
    got = job_bridge.advance_once("wxid_owner", store=_Store())
    assert got["stop"] == "budget"
    assert calls and calls[0][0] == ("job_stuck",)
    assert "银月的测试群" in calls[0][1]["detail"]


def test_an_unverified_order_tells_the_owner(monkeypatch):
    from loop import direct_send

    calls: list = []
    monkeypatch.setattr(oc, "note", lambda *a, **kw: calls.append((a, kw)) or {"sent": True})
    monkeypatch.setattr(direct_send, "_query_queue",
                        lambda ids, conn=None: [{"id": 1, "state": "sent", "ack": "pending", "why": ""}])
    monkeypatch.setattr(direct_send, "_verified", lambda it, marks: (False, ["队列行到 sent 了，但没回读到 msg_id（ack=pending）"]))
    state = {"items": {"m1": {"state": "queued", "queue_ids": [1],
                              "target": "银月的测试群", "chat": "10000000004@chatroom"}}}
    got = direct_send.check_deliveries(state=state)
    assert got["unverified"] == ["m1"]
    assert state["items"]["m1"]["state"] == "unverified"
    assert calls and calls[0][0] == ("order_unverified",)
    assert calls[0][1]["target"] == "10000000004@chatroom"

# --------------------------------------------------------------------------- 发不出去
# 2026-11-05：一夜里 265/266 两条「我卡住了」的话，desk 重试到上限落 failed，
# 主人一个字都没收到。这三条守的是「落 failed 就得有人说一声」。


def _desk_run(monkeypatch, *, ok: bool, why: str, state: str):
    """把 desk.execute_one 的外围全部打桩，只留「落 failed 要不要抄送」这段真逻辑。"""
    from loop import desk

    calls: list = []
    monkeypatch.setattr(oc, "note", lambda *a, **kw: calls.append((a, kw)) or {"sent": True})
    monkeypatch.setattr(desk, "_log", lambda *a, **kw: None)
    monkeypatch.setattr(desk.W, "claim_next",
                        lambda **kw: {"id": 265, "kind": "text", "chat_id": "10000000006@chatroom",
                                      "payload": {"kind": "text", "text": "我这边好像卡住了"},
                                      "attempts": 50})
    monkeypatch.setattr(desk.W, "heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(desk.W, "settle", lambda *a, **kw: state)
    monkeypatch.setattr(desk, "deliver", lambda it, dry=False: (ok, "" if not ok else "queued", why))
    # ★ 必须打桩：不打桩的话 desk 会去读真 data/world.db、把真队列行 265 补发出去。
    from loop import receipts as _rc
    requeues: list = []
    monkeypatch.setattr(_rc, "requeue_failed",
                        lambda qid, **kw: requeues.append(qid) or {"ok": True, "new_id": 77})
    return desk.execute_one(), calls, requeues


def test_a_message_that_never_gets_out_tells_the_owner(monkeypatch):
    got, calls, _rq = _desk_run(monkeypatch, ok=False, why="无法激活微信窗口", state="failed")
    assert got["state"] == "failed"
    assert calls and calls[0][0] == ("send_failed",)
    assert calls[0][1]["target"] == "10000000006@chatroom"
    assert "无法激活微信窗口" in calls[0][1]["detail"]


def test_a_message_that_got_out_is_not_reported(monkeypatch):
    got, calls, _rq = _desk_run(monkeypatch, ok=True, why="", state="sent")
    assert got["state"] == "sent"
    assert calls == []


def test_the_send_failed_line_names_the_reason_and_stays_short(monkeypatch):
    body = oc.text_for("send_failed", target="10000000006@chatroom", detail="无法激活微信窗口")
    assert "没发出去" in body and "无法激活微信窗口" in body
    assert "10000000006@chatroom" in body
    assert len(body) <= oc.MAX_CHARS and "\n" not in body
    assert "send_failed" in oc.KINDS



def test_a_failed_send_is_not_requeued(monkeypatch):
    """2026-11-09 拍板：不保留「落 failed 补发一跳」。

    failed 不是「没发出去」的证明（可能已发出只是没回执），补发会重复打扰；
    也不能绕过原队列的次数/终止策略。终局失败只通知主人，不再建新任务。
    见 docs/交接.md 的 requeue_failed 收口条目。
    """
    got, calls, requeues = _desk_run(monkeypatch, ok=False, why="无法激活微信窗口", state="failed")
    assert got["state"] == "failed"
    assert requeues == []             # 明确不补发
    assert calls and calls[0][0] == ("send_failed",)


def test_a_message_that_got_out_is_not_requeued(monkeypatch):
    got, calls, requeues = _desk_run(monkeypatch, ok=True, why="", state="sent")
    assert got["state"] == "sent"
    assert requeues == []            # 发成功的绝不补发


def test_the_quiet_alert_line_names_the_hours_and_stays_short():
    assert "quiet_alert" in oc.KINDS
    text = oc.text_for("quiet_alert", target="", detail="18.1")
    assert "18.1" in text
    assert "没有自己开口" in text
    assert len(text) <= oc.MAX_CHARS
    assert "\n" not in text


def test_a_failed_message_to_the_owner_is_not_reported_to_the_owner(monkeypatch):
    """发给主人的那条消息自己失败了，不许再抄回给主人（否则会缓慢自激）。"""
    box = _sent(monkeypatch)
    got = oc.note("send_failed", target="wxid_owner", detail="无法激活微信窗口")
    assert got["sent"] is False
    assert got["why"] == "owner_self"
    assert box == []          # 一条都不该进队列


def test_a_failed_message_to_a_group_is_still_reported(monkeypatch):
    box = _sent(monkeypatch)
    got = oc.note("send_failed", target="10000000001@chatroom", detail="无法激活微信窗口")
    assert got["sent"] is True
    assert len(box) == 1



def test_enqueue_carries_account_id_for_confirmation(monkeypatch):
    """2026-11-18：不带 account_id 时确认线程直接 return，抄送永远 unconfirmed。"""
    box = _sent(monkeypatch)
    monkeypatch.setattr("loop.speak_policy._account_of", lambda platform, chat: "wxid_bot")
    oc.note("order_unverified", target="10000000004@chatroom", now=5000.0)
    (gid, chat, items, kw), = box
    assert kw.get("account_id") == "wxid_bot"


def test_quiet_alert_is_not_queued_when_wechat_is_down(monkeypatch):
    """客户端没开时，quiet_alert 不许塞进同一条发不出去的队列。"""
    box = _sent(monkeypatch)
    monkeypatch.setattr("core.execution.wx_health.client_down", lambda: True)
    got = oc.note("quiet_alert", target="c1", detail="7.0", now=6000.0)
    assert got["sent"] is False and got["why"] == "wechat_client_down"
    assert box == []
