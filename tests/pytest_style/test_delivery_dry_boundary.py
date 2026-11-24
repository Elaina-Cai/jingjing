from loop import desk


def test_dry_delivery_cannot_obtain_sender(monkeypatch):
    monkeypatch.setattr(desk, "_bootstrap", lambda: None)
    monkeypatch.setattr(desk, "_label", lambda chat: chat)
    calls = []
    monkeypatch.setattr(desk, "_sender", lambda: calls.append("sender"))
    result = desk.deliver({"id": 1, "chat_id": "test", "kind": "text", "payload": {"text": "isolated dry"}}, dry=True)
    assert result == (True, "dry_run", "预演模式：没真发")
    assert calls == []
