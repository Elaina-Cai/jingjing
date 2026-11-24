"""普通唤醒使用当前分窗快照，上一拍时间戳的单位是毫秒。"""

from datetime import datetime

from core.infra import duration
from loop import run


def test_wake_facts_accepts_milliseconds():
    now = duration.now_ms()

    def at(ms):
        return datetime.fromtimestamp(ms // 1000).strftime("%Y-%m-%d %H:%M:%S")

    cell = {"label": "本窗",
            "peer_last": {"at": at(now - 500_000), "speaker": "小池", "text": "本窗的新话"},
            "my_last": {"at": at(now - 700_000), "text": "上一拍之前的回复"}}
    got = run._wake_facts(now - 600_000, cell)
    assert 599_000 <= got["since_last_tick_ms"] <= 601_000
    assert [event["text"] for event in got["peers"]] == ["本窗的新话"]
    assert got["mine"] == []


def test_wake_facts_zero_is_safe():
    got = run._wake_facts(0)
    assert got == {"since_last_tick_ms": None, "peers": [], "mine": []}


def test_trace_keeps_the_exact_current_request_anchor():
    import hashlib

    from loop import trace

    request_text = "@鲸鲸 先听我说，不要建议。"
    got = trace._wake_facts({"addressed": {
        "chat_id": "room@chatroom", "kind": "named", "who": "阿岚",
        "must_answer": True, "request_row_id": "42",
        "request_text": request_text,
    }})
    assert got["addressed"] == {
        "chat_id": "room@chatroom", "kind": "named", "who": "阿岚",
        "request_row_id": "42", "must_answer": True,
        "request_chars": len(request_text),
        "request_sha256": hashlib.sha256(request_text.encode("utf-8")).hexdigest(),
    }
