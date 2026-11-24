import pytest

from core.execution import wx_chat, wx_sender


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("key", ["enter", "ctrl_enter"])
@pytest.mark.parametrize("remaining", [True, False, None])
def test_real_sender_press_once_even_if_post_send_probe_is_uncertain(monkeypatch, key, remaining, override):
    from core import features
    monkeypatch.setattr(features, "dry_run_send", lambda *a: False)
    monkeypatch.setattr(wx_sender, "_wechat_window", lambda: object())
    monkeypatch.setattr(wx_sender, "resolve_nickname", lambda who: who)
    monkeypatch.setattr(wx_sender, "ensure_foreground", lambda: 1)
    monkeypatch.setattr(wx_sender, "_probe_reuse", lambda *a: (True, "test"))
    monkeypatch.setattr(wx_sender.time, "sleep", lambda *a: None)
    monkeypatch.setattr(wx_chat, "box_looks_empty", lambda: True)
    monkeypatch.setattr(wx_chat, "message_box_snapshot", lambda: None)
    changes = iter([True, True])
    monkeypatch.setattr(wx_chat, "message_box_changed", lambda *a: next(changes))
    monkeypatch.setattr(wx_chat, "message_box_has_text", lambda: remaining)
    monkeypatch.setattr(wx_chat, "invalidate_view", lambda: None)
    monkeypatch.setattr(wx_sender, "type_text", lambda *a: True)
    monkeypatch.setattr(wx_sender, "ack_baseline", lambda *a: None)
    monkeypatch.setattr(wx_sender, "spawn_ack_backfill", lambda *a: None)
    monkeypatch.setattr(wx_sender.WxSender, "_pre_send_guard", lambda *a: None)
    monkeypatch.setattr(wx_sender.WxSender, "_uipi_hint", lambda *a: "")
    calls = []
    monkeypatch.setattr(wx_sender, "press_enter", lambda: calls.append("enter"))
    monkeypatch.setattr(wx_sender, "press_ctrl_enter", lambda: calls.append("ctrl_enter"))
    sender = wx_sender.WxSender(min_interval=0, send_key=("ctrl_enter" if key == "enter" else "enter") if override else key)
    sender._connected = True
    result = sender.send_text("isolated", "hello", **({"send_key": key} if override else {}))
    assert calls == [key]
    assert result["ok"] is (remaining is not True)


def test_invalid_key_rejected_before_ui():
    with pytest.raises(ValueError, match="send_key"):
        wx_sender.WxSender(send_key="guess")


def test_real_sender_dry_does_not_press_any_key(monkeypatch):
    from core import features
    monkeypatch.setattr(features, "dry_run_send", lambda *a: True)
    calls = []
    monkeypatch.setattr(wx_sender, "press_enter", lambda: calls.append("enter"))
    monkeypatch.setattr(wx_sender, "press_ctrl_enter", lambda: calls.append("ctrl_enter"))
    assert wx_sender.WxSender().send_text("isolated", "hello")["dry_run"]
    assert calls == []
