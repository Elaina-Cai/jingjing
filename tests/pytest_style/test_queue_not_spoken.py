"""排队不等于说过：只排进投递队列的回应，不能当成"我已经说了，对方没接"。

反例来自真实事故形状：回复 ack=queued（还在投递队列里），主循环却按 result="sent"
判 _spoke，于是社交值按"说过了"下降、spont_streak 加一。她随后按"对方没接我"
收话、退避。这条用例锁住"只有本地确认发出的才算说过"。
"""

import time

from loop import ledger


def _scope(chat="wxid_a"):
    return {"platform": "wechat", "account_id": "acct", "chat_id": chat}


def _row(at, *, act="say", result="sent", target="wxid_a", text="在", ack="", **extra):
    row = {"at": at, "act": act, "result": result, "target": target,
           "text": text, "ack": ack, "scope": _scope(target)}
    row.update(extra)
    return row


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def test_confirmed_send_counts_as_spoken():
    state = ledger.send_state_of([_row(_now(), ack="confirmed")])
    assert state == {"confirmed": 1, "queued": 0}


def test_queued_send_is_not_spoken(monkeypatch):
    from db import world_db

    monkeypatch.setattr(world_db, "confirmed_queue_settled_ts", lambda *a, **k: None)
    state = ledger.send_state_of([
        _row(_now(), ack="queued", queue_ids=[71], turn_id="t1", timing={"bubbles": 1})])
    assert state == {"confirmed": 0, "queued": 1}


def test_queued_send_counts_once_the_world_db_confirms(monkeypatch):
    from db import world_db

    monkeypatch.setattr(world_db, "confirmed_queue_settled_ts", lambda *a, **k: time.time())
    state = ledger.send_state_of([
        _row(_now(), act="sticker", ack="queued", queue_ids=[71], turn_id="t1",
             timing={"bubbles": 1})])
    assert state == {"confirmed": 1, "queued": 0}


def test_other_acts_and_failures_are_not_speech():
    state = ledger.send_state_of([
        _row(_now(), act="read_memory", result="ok", ack=""),
        _row(_now(), result="blocked", ack=""),
        _row(_now(), act="say", result="rehearsal", ack="dry_run"),
        "not-a-row",
        None,
    ])
    assert state == {"confirmed": 0, "queued": 0}


def test_loop_counts_only_confirmed_sends_as_spoken():
    """源码锁：主循环的 _spoke 必须走 send_state_of，白醒也不许把排队算进去。"""
    source = (ledger.__file__.replace("ledger.py", "run.py"))
    import io

    with io.open(source, encoding="utf-8") as fh:
        text = fh.read()
    assert "_send_state = ledger.send_state_of(out.get(\"receipts\") or [])" in text
    assert "_spoke = bool(_send_state[\"confirmed\"])" in text
    assert ('if spont and not _spoke and not _heard and not _send_state["queued"]:'
            in text)
    assert 'any(x.get("act") == "say" and x.get("result") == "sent"' not in text

def test_picture_and_sticker_keep_their_own_delivery_state(monkeypatch):
    """同一会话里一张已确认发出的图、一张仍在队列的表情：各自带各自的状态，不许合并。

    真实形状：对方问「你刚才到底发了什么，出去了吗」——她要能分清哪条真出去了、
    哪条还在队列里，而且这两条都不能说明对方已经看到。
    """
    from db import world_db

    chat = "wxid_a"
    now = _now()
    monkeypatch.setattr(ledger, "_current_receipt_scope", lambda: ("wechat", "acct", chat))
    monkeypatch.setattr(world_db, "confirmed_queue_settled_ts", lambda *a, **k: None)
    rows = [
        _row(now, act="show", target=chat, text="给你的图", ack="confirmed", turn_id="t1"),
        _row(now, act="sticker", target=chat, text="嗯", ack="queued", queue_ids=[9],
             turn_id="t1", timing={"bubbles": 1}),
        _row(now, act="say", target=chat, text="嗯", ack="confirmed", turn_id="t1"),
    ]
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=600: rows)
    out = ledger.recent_says_in(chat, limit=5)
    by_act = {row["act"]: row for row in out}
    assert by_act["show"]["delivery"] == "本地确认发出，送达还未确认"
    assert by_act["sticker"]["delivery"] == "已排进投递队列，送达还未确认"
    assert by_act["say"]["delivery"] == "本地确认发出，送达还未确认"
    assert len([r for r in out if r["text"] == "嗯"]) == 2      # 同文的文字与表情不合并
