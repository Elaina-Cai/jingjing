"""台账里她自己的话/欠的账不再被截、不再被限条数（2026-10-31 主人要求"全部上限都取消"）。"""

import pytest

from loop import ledger


@pytest.fixture(autouse=True)
def _no_evidence(monkeypatch):
    """本文件只测「文本别被截」；凭据过滤见 test_say_evidence.py。"""
    ledger._EVID_CACHE.update({"at": 0.0, "keys": set(), "ok": False})
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: (set(), False))


def test_recent_says_keeps_whole_text(monkeypatch):
    long = "很长的句子" * 30  # 150 字
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=120: [
        {"at": "2026-10-31 13:40:00", "act": "say", "result": "sent", "target": "a", "text": long}])
    rows = ledger.recent_says()
    assert rows and rows[0]["text"] == long


def _send_scope(chat="wxid_a"):
    return {"platform": "wechat", "account_id": "acct", "chat_id": chat}


def test_recent_say_targets_keeps_whole_text(monkeypatch):
    import time as _t
    long = "这一句也很长" * 20
    at = _t.strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: [
        {"at": at, "act": "say", "result": "sent", "target": "wxid_a", "text": long,
         "ack": "confirmed", "scope": _send_scope()}])
    rows = ledger.recent_say_targets(window_min=60)
    assert rows and rows[0]["last_text"] == long


def test_recent_say_targets_no_default_limit(monkeypatch):
    import time as _t
    at = _t.strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: [
        {"at": at, "act": "say", "result": "sent", "target": "wxid_%d" % i, "text": "x",
         "ack": "confirmed", "scope": _send_scope("wxid_%d" % i)}
        for i in range(5)])
    assert len(ledger.recent_say_targets(window_min=60)) == 5   # 旧的 limit=2 只给两个
    assert len(ledger.recent_say_targets(window_min=60, limit=1)) == 1


def _say_row(at, *, target="wxid_a", text="那两句", ack="", scope=None, **extra):
    row = {"at": at, "act": "say", "result": "sent", "target": target, "text": text,
           "ack": ack, "scope": scope if scope is not None else _send_scope(target)}
    row.update(extra)
    return row


def test_recent_say_targets_skips_queued_until_world_db_confirms(monkeypatch):
    import time as _t
    from db import world_db

    at = _t.strftime("%Y-%m-%d %H:%M:%S")
    row = _say_row(at, ack="queued", queue_ids=[71], turn_id="turn-1",
                   timing={"bubbles": 1})
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: [row])
    monkeypatch.setattr(world_db, "confirmed_queue_settled_ts", lambda *args, **kwargs: None)
    assert ledger.recent_say_targets(window_min=60) == []


def test_recent_say_targets_counts_queue_confirmation_once_at_settled_time(monkeypatch):
    import time as _t
    from db import world_db

    now = _t.time()
    at = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(now - 40))
    settled = now - 5
    row = _say_row(at, ack="queued", queue_ids=[71, 72], turn_id="turn-1",
                   timing={"bubbles": 2})
    calls = []

    def settled_ts(qids, **kwargs):
        calls.append((qids, kwargs))
        return settled

    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: [row])
    monkeypatch.setattr(world_db, "confirmed_queue_settled_ts", settled_ts)
    rows = ledger.recent_say_targets(window_min=60)
    assert len(rows) == 1 and rows[0]["says"] == 1
    assert rows[0]["says_10m"] == 1
    assert rows[0]["last_at"] == settled
    assert 0 <= rows[0]["gap_s"] <= 10
    assert calls == [([71, 72], {"platform": "wechat", "account_id": "acct",
                                 "chat_id": "wxid_a", "turn_id": "turn-1"})]


def test_recent_say_targets_counts_direct_confirmed_say(monkeypatch):
    import time as _t

    now = _t.time()
    at = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(now - 10))
    monkeypatch.setattr(ledger, "read_receipts",
                        lambda limit=300: [_say_row(at, ack="confirmed", turn_id="turn-2")])
    rows = ledger.recent_say_targets(window_min=60)
    expected = _t.mktime(_t.strptime(at, "%Y-%m-%d %H:%M:%S"))
    assert len(rows) == 1 and rows[0]["says"] == 1
    assert rows[0]["last_at"] == expected


def test_recent_says_in_binds_delivery_state_to_each_text(monkeypatch):
    import time as _t
    from db import world_db

    now = _t.time()
    old_at = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(now - 40))
    new_at = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(now - 10))
    rows = [
        _say_row(old_at, text="旧稿已经确认发出。", ack="queued", queue_ids=[71],
                 turn_id="turn-old", timing={"bubbles": 1}),
        _say_row(new_at, text="新稿还在队列里。", ack="queued", queue_ids=[72],
                 turn_id="turn-new", timing={"bubbles": 1}),
    ]
    settled = _t.mktime(_t.strptime(old_at, "%Y-%m-%d %H:%M:%S"))
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=600: rows)
    monkeypatch.setattr(ledger, "_current_receipt_scope",
                        lambda: ("wechat", "acct", "wxid_a"))
    monkeypatch.setattr(
        world_db, "confirmed_queue_settled_ts",
        lambda qids, **kwargs: settled if qids == [71] else None)

    got = ledger.recent_says_in("wxid_a", limit=3)
    states = {row["text"]: row["delivery"] for row in got}
    assert states == {
        "旧稿已经确认发出。": "本地确认发出，送达还未确认",
        "新稿还在队列里。": "已排进投递队列，送达还未确认",
    }


def test_recent_says_in_keeps_image_and_sticker_actions(monkeypatch):
    import time as _t

    now = _t.time()
    show_at = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(now - 20))
    sticker_at = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(now - 10))
    rows = [
        _say_row(show_at, text="给你的图", ack="confirmed", scope=_send_scope()),
        _say_row(sticker_at, text="点头", ack="queued", queue_ids=[72],
                 turn_id="turn-sticker", timing={"bubbles": 1}),
    ]
    rows[0]["act"] = "show"
    rows[1]["act"] = "sticker"
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=600: rows)
    monkeypatch.setattr(ledger, "_current_receipt_scope",
                        lambda: ("wechat", "acct", "wxid_a"))

    got = ledger.recent_says_in("wxid_a", limit=3)
    assert {row["act"]: row["delivery"] for row in got} == {
        "show": "本地确认发出，送达还未确认",
        "sticker": "已排进投递队列，送达还未确认",
    }


def test_recent_says_in_does_not_merge_same_text_across_actions(monkeypatch):
    import time as _t

    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        _say_row(now, text="嗯", ack="confirmed", scope=_send_scope()),
        _say_row(now, text="嗯", ack="queued", queue_ids=[72],
                 turn_id="turn-sticker", timing={"bubbles": 1}),
    ]
    rows[0]["act"] = "show"
    rows[1]["act"] = "sticker"
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=600: rows)
    monkeypatch.setattr(ledger, "_current_receipt_scope",
                        lambda: ("wechat", "acct", "wxid_a"))

    got = ledger.recent_says_in("wxid_a", limit=3)
    assert {row["act"] for row in got} == {"show", "sticker"}


@pytest.mark.parametrize("missing", ["platform", "account_id", "chat_id"])
def test_recent_say_targets_rejects_incomplete_scope(monkeypatch, missing):
    import time as _t

    at = _t.strftime("%Y-%m-%d %H:%M:%S")
    scope = _send_scope()
    scope.pop(missing)
    row = _say_row(at, ack="confirmed", turn_id="turn-3", scope=scope)
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: [row])
    assert ledger.recent_say_targets(window_min=60) == []


def test_recent_say_targets_rejects_target_outside_scope(monkeypatch):
    import time as _t

    at = _t.strftime("%Y-%m-%d %H:%M:%S")
    row = _say_row(at, target="wxid_b", ack="confirmed", turn_id="turn-mismatch",
                   scope=_send_scope("wxid_a"))
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: [row])
    assert ledger.recent_say_targets(window_min=60) == []


def test_recent_say_targets_rejects_queue_without_turn_id(monkeypatch):
    import time as _t
    from db import world_db

    at = _t.strftime("%Y-%m-%d %H:%M:%S")
    row = _say_row(at, ack="queued", queue_ids=[71], turn_id="",
                   timing={"bubbles": 1})
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: [row])
    monkeypatch.setattr(world_db, "confirmed_queue_settled_ts", lambda *args, **kwargs: _t.time())
    assert ledger.recent_say_targets(window_min=60) == []


@pytest.mark.parametrize("queue_ids", ["71", 71, []])
def test_recent_say_targets_rejects_invalid_queue_ids(monkeypatch, queue_ids):
    import time as _t

    at = _t.strftime("%Y-%m-%d %H:%M:%S")
    row = _say_row(at, ack="queued", queue_ids=queue_ids, turn_id="turn-4",
                   timing={"bubbles": 1})
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: [row])
    assert ledger.recent_say_targets(window_min=60) == []


@pytest.mark.parametrize("timing", [{"bubbles": 1}, {}])
def test_recent_say_targets_rejects_bubble_count_mismatch(monkeypatch, timing):
    import time as _t
    from db import world_db

    at = _t.strftime("%Y-%m-%d %H:%M:%S")
    row = _say_row(at, ack="queued", queue_ids=[71, 72], turn_id="turn-5",
                   timing=timing)
    calls = []
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: [row])
    monkeypatch.setattr(
        world_db, "confirmed_queue_settled_ts",
        lambda *args, **kwargs: calls.append(1) or _t.time())
    assert ledger.recent_say_targets(window_min=60) == []
    assert calls == []


def test_close_promise_keeps_whole_reason(monkeypatch):
    cap = {}
    monkeypatch.setattr(ledger, "_append_row", lambda row: (cap.update(row), {})[1])
    why = "理由" * 80
    ledger.close_promise("1", reason=why)
    assert cap["why"] == why


def _promise_rows(n):
    return [{"kind": "promise", "id": str(i), "at": "2026-10-31 13:0%d:00" % i,
             "chat_id": "", "target": "", "who": "Elaina",
             "what": "第%d件事 %s" % (i, "很长" * 30)} for i in range(1, n + 1)]


def test_open_promises_no_default_limit(monkeypatch):
    monkeypatch.setattr(ledger, "_promise_rows", lambda days: _promise_rows(6))
    assert len(ledger.open_promises()) == 6          # 旧的 limit=4 会砍到 4
    assert len(ledger.open_promises(limit=2)) == 2   # 显式仍然有效


def test_render_promises_no_budget(monkeypatch):
    monkeypatch.setattr(ledger, "_promise_rows", lambda days: _promise_rows(6))
    txt = ledger.render_promises()
    assert len(txt) > 260                            # 旧的 budget=260 会截
    assert "很长" * 30 in txt                        # 一句都没被切
    assert "第1件事" in txt and "第6件事" in txt


def test_record_promise_no_120_cut():
    import inspect
    assert "[:120]" not in inspect.getsource(ledger.record_promise)


def test_brain_receipts_not_truncated():
    import inspect
    from loop import brain
    src = inspect.getsource(brain)
    assert "[:60]" not in src
