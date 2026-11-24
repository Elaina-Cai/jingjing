"""C7「这一拍我已经说过」的取数口径：不截断、不限条数、本窗（名字或 chat_id）都排除。"""

from loop import ledger


def _fake(rows):
    return lambda limit=200: rows


def test_no_truncation(monkeypatch):
    long_text = "这句真的很长" * 40  # 240 字
    monkeypatch.setattr(ledger, "read_receipts", _fake([
        {"at": "2026-10-31 13:40:31", "act": "say", "result": "sent",
         "target": "wxid_a", "text": long_text},
    ]))
    rows = ledger.said_since("2026-10-31 13:40:00")
    assert len(rows) == 1
    assert rows[0]["text"] == long_text  # 一个字都不许少


def test_no_default_limit(monkeypatch):
    monkeypatch.setattr(ledger, "read_receipts", _fake([
        {"at": "2026-10-31 13:40:%02d" % i, "act": "say", "result": "sent",
         "target": "wxid_%d" % i, "text": "第 %d 句" % i} for i in range(1, 9)
    ]))
    rows = ledger.said_since("2026-10-31 13:40:00")
    assert len(rows) == 8  # 旧的 limit=4 会砍到 4


def test_limit_still_works_when_asked(monkeypatch):
    monkeypatch.setattr(ledger, "read_receipts", _fake([
        {"at": "2026-10-31 13:40:%02d" % i, "act": "say", "result": "sent",
         "target": "wxid_%d" % i, "text": "x"} for i in range(1, 9)
    ]))
    assert len(ledger.said_since("2026-10-31 13:40:00", limit=2)) == 2


def test_exclude_by_name_and_chat(monkeypatch):
    monkeypatch.setattr(ledger, "read_receipts", _fake([
        {"at": "2026-10-31 13:40:31", "act": "say", "result": "sent",
         "target": "Elaina", "text": "本窗（名字）"},
        {"at": "2026-10-31 13:40:32", "act": "say", "result": "sent",
         "target": "wxid_seed01", "text": "本窗（chat_id）"},
        {"at": "2026-10-31 13:40:33", "act": "say", "result": "sent",
         "target": "wxid_other", "text": "别的窗"},
    ]))
    rows = ledger.said_since("2026-10-31 13:40:00", exclude="Elaina",
                             exclude_chat="wxid_seed01")
    assert [r["text"] for r in rows] == ["别的窗"]


def test_only_sent_says(monkeypatch):
    monkeypatch.setattr(ledger, "read_receipts", _fake([
        {"at": "2026-10-31 13:40:31", "act": "say", "result": "error", "target": "a", "text": "失败的"},
        {"at": "2026-10-31 13:40:32", "act": "blocked", "result": "blocked", "target": "b", "text": "被挡的"},
        {"at": "2026-10-31 13:40:33", "act": "say", "result": "sent", "target": "c", "text": "真发出去了"},
        {"at": "2026-10-31 13:39:00", "act": "say", "result": "sent", "target": "d", "text": "上一拍"},
    ]))
    rows = ledger.said_since("2026-10-31 13:40:00")
    assert [r["text"] for r in rows] == ["真发出去了"]


def test_brain_passes_chat_id(monkeypatch):
    import inspect
    from loop import brain
    src = inspect.getsource(brain.run_loop)
    assert "exclude_chat=chat_id" in src
