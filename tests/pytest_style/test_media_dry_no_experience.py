import pytest


@pytest.mark.parametrize("kind", ["file", "image"])
def test_public_media_dry_never_records_or_starts_confirmation(monkeypatch, kind):
    from core import features
    from core.execution import wx_sender
    from db import world_db
    monkeypatch.setattr(features, "dry_run_send", lambda *a: True)
    calls = []
    monkeypatch.setattr(world_db, "record_outbound", lambda *a, **kw: calls.append("record"))
    monkeypatch.setattr(wx_sender, "_confirm_file_later", lambda *a: calls.append("confirm"))
    monkeypatch.setattr(wx_sender.WxSender, "connect", lambda *a: calls.append("connect"))
    sender = wx_sender.WxSender()
    result = getattr(sender, "send_" + kind)("isolated", "missing-file")
    assert result == {"ok": True, "dry_run": True}
    assert calls == []


@pytest.mark.parametrize("kind", ["file", "image"])
def test_desk_preserves_adapter_dry_evidence(monkeypatch, tmp_path, kind):
    from loop import desk
    from types import SimpleNamespace as NS
    path = tmp_path / "asset"
    path.write_text("isolated", encoding="utf-8")
    monkeypatch.setattr(desk, "_bootstrap", lambda: None)
    monkeypatch.setattr(desk, "_label", lambda chat: chat)
    monkeypatch.setattr(desk, "_dry", lambda chat: False)
    calls = []
    def send(*a):
        calls.append(a)
        return {"ok": True, "dry_run": True}
    monkeypatch.setattr(desk, "_sender", lambda: NS(send_image=send, send_file=send))
    result = desk.deliver({"chat_id": "isolated", "kind": kind, "payload": {"path": str(path)}})
    assert result == (True, "dry_run", "预演模式：没真发")
    assert len(calls) == 1


def test_live_file_success_still_records_existing_receipt(monkeypatch):
    from core.execution import wx_sender
    from db import world_db
    calls = []
    monkeypatch.setattr(wx_sender, "_orig_send_file", lambda *a: {"ok": True, "ack": "unconfirmed"})
    monkeypatch.setattr(world_db, "record_outbound", lambda *a, **kw: calls.append("record"))
    monkeypatch.setattr(wx_sender, "_confirm_file_later", lambda *a: calls.append("confirm"))
    result = wx_sender.WxSender().send_file("isolated", "asset")
    assert result["ok"]
    assert calls == ["record"]


@pytest.mark.parametrize("kind", ["text", "sticker"])
def test_other_adapter_dry_does_not_write_receipts_or_cooldown(monkeypatch, kind):
    from loop import desk, stickers
    from core.execution import wx_sticker_sender
    from db import world_db
    from types import SimpleNamespace as NS
    calls = []
    monkeypatch.setattr(desk, "_bootstrap", lambda: None)
    monkeypatch.setattr(desk, "_label", lambda chat: chat)
    monkeypatch.setattr(desk, "_dry", lambda chat: False)
    monkeypatch.setattr(desk, "_sender", lambda: NS(send_text=lambda *a: {"ok": True, "dry_run": True}))
    monkeypatch.setattr(wx_sticker_sender, "send_sticker", lambda *a, **kw: {"ok": True, "dry_run": True})
    monkeypatch.setattr(stickers, "account_of", lambda key: "bot")
    monkeypatch.setattr(stickers, "mark_sent", lambda *a: calls.append("cooldown"))
    monkeypatch.setattr(world_db, "record_outbound", lambda *a, **kw: calls.append("record"))
    monkeypatch.setattr(desk, "_confirm_queue_later", lambda *a: calls.append("confirm"))
    result = desk.deliver({"kind": kind, "chat_id": "isolated", "payload": {"text": "hello", "key": "test"}})
    assert result == (True, "dry_run", "预演模式：没真发")
    assert calls == []
