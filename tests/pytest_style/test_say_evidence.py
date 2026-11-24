"""账本「我说过」必须有真凭据（E-50 的后半）。

2026-11-04：账本 18:46:52 有 4 条 `say`/`sent` 回执，`messages` 里同期 0 条、
`outbound_queue` 也没有对应行（`ack` 是空串）。她据此以为自己「连发了三遍」，
下一拍就去「撤回」一件没发生的事。
"""

import pytest

from loop import ledger


def _receipt(**kw):
    d = {"at": "2026-11-04 18:46:52", "act": "say", "target": "10000000001@chatroom",
         "result": "sent", "ack": "", "text": "同学你好，关于报到这件事我再说一遍"}
    d.update(kw)
    return d


@pytest.fixture(autouse=True)
def _fresh_cache():
    ledger._EVID_CACHE.update({"at": 0.0, "keys": set(), "ok": False})
    yield
    ledger._EVID_CACHE.update({"at": 0.0, "keys": set(), "ok": False})


def test_a_receipt_without_any_evidence_is_dropped(monkeypatch):
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: (set(), True))
    assert ledger.verified_says([_receipt()]) == []


def test_a_queued_receipt_counts_even_without_a_row_in_the_table(monkeypatch):
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: (set(), True))
    kept = ledger.verified_says([_receipt(ack="queued")])
    assert len(kept) == 1


def test_receipts_with_queue_ids_count(monkeypatch):
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: (set(), True))
    assert len(ledger.verified_says([_receipt(queue_ids=[264])])) == 1


def test_a_receipt_that_matches_the_outbox_counts(monkeypatch):
    key = ("10000000001@chatroom", ledger._say_key("同学你好，关于报到这件事我再说一遍。"))
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: ({key}, True))
    assert len(ledger.verified_says([_receipt()])) == 1


def test_other_chats_do_not_lend_their_evidence(monkeypatch):
    key = ("10000000004@chatroom", ledger._say_key("同学你好，关于报到这件事我再说一遍"))
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: ({key}, True))
    assert ledger.verified_says([_receipt()]) == []


def test_when_the_ledger_of_evidence_is_unreadable_we_do_not_erase_history(monkeypatch):
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: (set(), False))
    assert len(ledger.verified_says([_receipt(), _receipt(text="另一句")])) == 2


def test_recent_says_only_returns_verified_lines(monkeypatch):
    good = _receipt(ack="queued", text="真发出去的那句")
    bad = _receipt(text="其实没发出去的那句")
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: [bad, good])
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: (set(), True))
    got = ledger.recent_says(limit=3)
    assert [d["text"] for d in got] == ["真发出去的那句"]


# ---- 2026-11-04：测试自己写的回执不算「我说过」（源头过滤，与凭据无关）----
def test_a_test_harness_receipt_is_never_my_words(monkeypatch):
    # 哪怕凭据齐（ack=queued），via="speech" 的打字回执也不算她说过。
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: (set(), True))
    assert ledger.verified_says([_receipt(ack="queued", via="speech")]) == []


def test_a_wiring_target_is_never_my_words(monkeypatch):
    # tests/test_claim_ledger_wiring.py 的 DEFAULT_CHAT 就是这个前缀。
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: (set(), True))
    r = _receipt(ack="queued", target="wiring_10000000001@chatroom")
    assert ledger.verified_says([r]) == []


def test_the_c1_probe_chat_is_never_my_words(monkeypatch):
    key = ("c1", ledger._say_key("hello"))
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: ({key}, True))
    assert ledger.verified_says([_receipt(target="c1", text="hello")]) == []


def test_test_receipts_are_dropped_even_when_evidence_is_unreadable(monkeypatch):
    # 降级放行（读不到凭据）时也不许把测试的话放回来。
    monkeypatch.setattr(ledger, "_real_say_keys", lambda days=7: (set(), False))
    real = _receipt(ack="queued", text="真发出去的那句")
    fake = _receipt(target="wiring_10000000001@chatroom")
    kept = ledger.verified_says([real, fake])
    assert [d["text"] for d in kept] == ["真发出去的那句"]
