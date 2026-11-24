from __future__ import annotations

import inspect

from core.execution import wx_chat, wx_sender


def test_fingerprint_shortcut_wins_without_ocr(monkeypatch):
    calls = {"ocr": 0}

    monkeypatch.setattr(wx_chat, "try_reuse_chat", lambda *a, **k: {"ok": True})

    def _ocr(*a, **k):
        calls["ocr"] += 1
        return True, ""

    monkeypatch.setattr(wx_chat, "verify_active_chat_title", _ocr)
    reused, src = wx_sender._probe_reuse("Elaina", "wxid_seed01")
    assert reused is True
    assert src == "fingerprint"
    assert calls["ocr"] == 0, "指纹命中时不该再花一次 OCR"


def test_falls_back_to_ocr_when_fingerprint_misses(monkeypatch):
    calls = {"ocr": 0}
    monkeypatch.setattr(wx_chat, "try_reuse_chat", lambda *a, **k: None)

    def _ocr(*a, **k):
        calls["ocr"] += 1
        return True, ""

    monkeypatch.setattr(wx_chat, "verify_active_chat_title", _ocr)
    reused, src = wx_sender._probe_reuse("Elaina", "wxid_seed01")
    assert reused is True and src == "ocr" and calls["ocr"] == 1


def test_ocr_mismatch_is_not_reuse(monkeypatch):
    monkeypatch.setattr(wx_chat, "try_reuse_chat", lambda *a, **k: None)
    monkeypatch.setattr(wx_chat, "verify_active_chat_title", lambda *a, **k: (False, "不是她"))
    reused, src = wx_sender._probe_reuse("Elaina", "wxid_seed01")
    assert reused is False and src == "ocr_miss"


def test_reuse_never_trusts_unverified_memory(monkeypatch):
    seen = {}

    def _reuse(display, raw_who="", *, allow_unverified=False):
        seen["allow_unverified"] = allow_unverified
        return None

    monkeypatch.setattr(wx_chat, "try_reuse_chat", _reuse)
    monkeypatch.setattr(wx_chat, "verify_active_chat_title", lambda *a, **k: (False, ""))
    wx_sender._probe_reuse("Elaina", "wxid_seed01")
    assert seen["allow_unverified"] is False, "不许信没被标题验证过的记忆"


def test_send_path_still_hard_verifies_before_typing():
    src = inspect.getsource(wx_sender.WxSender.send_text)
    assert "verify_active_chat_title" in src or "_probe_reuse" in src
    assert "wx_send_wrong_chat_abort" in src, "验完标题不对必须中止，不许打字"
