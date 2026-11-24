from types import SimpleNamespace as NS

import pytest

from core.execution.wx_sender import account_send_options


def test_account_key_reads_only_requested_account():
    calls = []
    def get(account):
        calls.append(account)
        return NS(account_id=account, platform="wechat", config_json='{"send_key":"ctrl_enter"}')
    assert account_send_options("a", meta_repo=NS(account_registry=NS(get=get))) == {"send_key": "ctrl_enter"}
    assert calls == ["a"]


@pytest.mark.parametrize("platform,config", [("qq", "{}"), ("wechat", "{broken"), ("wechat", "[]"), ("wechat", '{"send_key":"guess"}')])
def test_bad_account_configuration_does_not_guess(platform, config):
    meta = NS(account_registry=NS(get=lambda account: NS(account_id=account, platform=platform, config_json=config)))
    with pytest.raises(ValueError):
        account_send_options("a", meta_repo=meta)


def test_desk_uses_call_option_and_does_not_change_shared_sender(monkeypatch):
    from core.execution import wx_sender
    from loop import desk
    calls = []
    class Sender:
        def send_text(self, *args, **kw):
            calls.append(kw)
            return {"ok": False, "error": "isolated stop"}
    monkeypatch.setattr(desk, "_bootstrap", lambda: None)
    monkeypatch.setattr(desk, "_label", lambda chat: chat)
    monkeypatch.setattr(desk, "_dry", lambda chat: False)
    monkeypatch.setattr(desk, "_sender", Sender)
    monkeypatch.setattr(desk, "_log", lambda *a, **kw: None)
    seen = []
    def options(account, **kw):
        seen.append((account, kw))
        return {"send_key": "ctrl_enter" if account == "a" else "enter"}
    monkeypatch.setattr(wx_sender, "account_send_options", options)
    for account in ("a", "b"):
        desk.deliver({"account_id": account, "platform": "wechat", "chat_id": "room", "payload": {"text": "hello"}})
    assert calls == [{"send_key": "ctrl_enter"}, {"send_key": "enter"}]
    assert seen == [("a", {"platform": "wechat"}), ("b", {"platform": "wechat"})]
