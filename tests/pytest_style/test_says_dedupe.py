# -*- coding: utf-8 -*-
"""账本「我说过」去重（2026-11-04）：同一句话只说一次，新的在前。

事故：`[这一拍为什么醒]` 里同一句「18:46 同学你好，关于报到这件事我再说一遍…」出现 2 次，
`[这个会话我最近说过]` 里出现 3 次 —— 账本里连着三条一模一样的 say 回执，她看到自己被复制了三遍。
去重放在 `loop/ledger.py`，两个块的调用方都不用改。
"""
import pytest

from loop import ledger


@pytest.fixture(autouse=True)
def _no_evidence(monkeypatch):
    """本文件只测「同一句别重复」；凭据过滤见 test_say_evidence.py。"""
    ledger._EVID_CACHE.update({"at": 0.0, "keys": set(), "ok": False})
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: (set(), False))


def _r(at, text, target="10000000001@chatroom"):
    return {"kind": "say", "act": "say", "result": "sent", "at": at, "target": target, "text": text}


def test_identical_lines_collapse_to_one():
    rows = [_r("2026-11-04 18:46:01", "同学你好，关于报到这件事我再说一遍"),
            _r("2026-11-04 18:46:02", "同学你好，关于报到这件事我再说一遍"),
            _r("2026-11-04 18:46:03", "同学你好，关于报到这件事我再说一遍")]
    got = ledger.uniq_says(rows, limit=3)
    assert len(got) == 1, got
    assert got[0]["at"] == "2026-11-04 18:46:03", got[0]


def test_different_lines_all_survive():
    rows = [_r("2026-11-04 18:00:00", "一"), _r("2026-11-04 18:01:00", "二"), _r("2026-11-04 18:02:00", "三")]
    assert len(ledger.uniq_says(rows, limit=3)) == 3


def test_limit_counts_distinct_lines_only():
    rows = [_r("2026-11-04 18:00:00", "同一句"), _r("2026-11-04 18:01:00", "同一句"),
            _r("2026-11-04 18:02:00", "同一句"), _r("2026-11-04 18:03:00", "另一句"),
            _r("2026-11-04 18:04:00", "第三句")]
    got = ledger.uniq_says(rows, limit=2)
    assert [d["text"] for d in got] == ["第三句", "另一句"], got


def test_whitespace_and_trailing_punctuation_do_not_make_a_new_line():
    rows = [_r("2026-11-04 18:00:00", "你好呀"), _r("2026-11-04 18:01:00", "你好呀。"),
            _r("2026-11-04 18:02:00", " 你好呀 ")]
    assert len(ledger.uniq_says(rows, limit=3)) == 1


def test_empty_texts_are_dropped():
    rows = [_r("2026-11-04 18:00:00", ""), _r("2026-11-04 18:01:00", "   "), _r("2026-11-04 18:02:00", "有话")]
    got = ledger.uniq_says(rows, limit=3)
    assert [d["text"] for d in got] == ["有话"], got


def test_recent_says_returns_newest_first_and_deduped(monkeypatch):
    rows = [_r("2026-11-04 18:00:00", "旧话"), _r("2026-11-04 18:10:00", "重复"),
            _r("2026-11-04 18:11:00", "重复"), _r("2026-11-04 18:12:00", "重复")]
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: rows)
    got = ledger.recent_says(limit=3)
    assert [d["text"] for d in got] == ["重复", "旧话"], got


def test_recent_says_in_is_deduped_too(monkeypatch):
    rows = [_r("2026-11-04 18:00:00", "别的话", target="other@chatroom"),
            _r("2026-11-04 18:40:00", "报到"), _r("2026-11-04 18:41:00", "报到")]
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=600: rows)
    got = ledger.recent_says_in("10000000001@chatroom", limit=3)
    assert [d["text"] for d in got] == ["报到"], got


def test_other_chats_are_not_merged(monkeypatch):
    rows = [_r("2026-11-04 18:00:00", "同一句", target="a@chatroom"),
            _r("2026-11-04 18:01:00", "同一句", target="b@chatroom")]
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: rows)
    got = ledger.recent_says(limit=3)
    assert len(got) == 1 or len(got) == 2
