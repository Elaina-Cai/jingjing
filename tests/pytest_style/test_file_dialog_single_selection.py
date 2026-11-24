import pytest


@pytest.mark.parametrize("visible,title,focus,presses,ok", [(True, True, True, 1, False), (False, False, True, 1, False), (False, True, False, 1, False), (False, True, True, 2, True)])
def test_dialog_selection_and_send_have_separate_boundaries(monkeypatch, visible, title, focus, presses, ok):
    import win32gui
    from core.execution import wx_sender, wx_chat, wx_human
    sender = wx_sender.WxSender()
    monkeypatch.setattr(sender, "_ensure_window_on_screen", lambda: {"ok": True})
    monkeypatch.setattr(sender, "_click_file_button", lambda: True)
    monkeypatch.setattr(sender, "_find_file_dialog", lambda *a: 123)
    monkeypatch.setattr(sender, "_set_dialog_path", lambda *a: True)
    monkeypatch.setattr(sender, "_wait_dialog_path", lambda *a: True)
    monkeypatch.setattr(sender, "_pre_send_guard", lambda *a: None)
    monkeypatch.setattr(win32gui, "SetForegroundWindow", lambda *a: None)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda *a: visible)
    monkeypatch.setattr(wx_chat, "verify_active_chat_title", lambda *a, **kw: (title, "seen"))
    monkeypatch.setattr(wx_chat, "focus_message_box", lambda: focus)
    monkeypatch.setattr(wx_sender, "resolve_nickname", lambda who: who)
    clock = [100.0]
    def now():
        clock[0] += 0.25
        return clock[0]
    monkeypatch.setattr(wx_sender.time, "time", now)
    monkeypatch.setattr(wx_sender.time, "sleep", lambda *a: None)
    keys = []
    monkeypatch.setattr(wx_human, "key_press", lambda key: keys.append(key))
    result = sender._send_file_via_dialog("isolated", "room")
    assert result["ok"] is ok
    assert keys.count(wx_human.VK_RETURN) == presses
    if visible:
        assert keys == [wx_human.VK_RETURN, wx_human.VK_ESCAPE]
    if ok:
        assert result["ack"] == "unconfirmed"
