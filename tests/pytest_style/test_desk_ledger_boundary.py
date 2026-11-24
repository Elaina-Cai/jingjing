"""Send succeeded but local ledger failed must not settle as sent."""
from __future__ import annotations

import pytest

from loop import desk


class _Sender:
    def send_text(self, who, text, **kw):
        return {"ok": True, "ack": "unconfirmed"}


def _item():
    return {"id": 41, "attempts": 1, "owner": "tester", "kind": "text",
            "chat_id": "room@chatroom", "platform": "wechat", "account_id": "acct",
            "payload": {"text": "hello"}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(desk, "_bootstrap", lambda: None)
    monkeypatch.setattr(desk, "_dry", lambda chat: False)
    monkeypatch.setattr(desk, "_label", lambda chat: "room")
    monkeypatch.setattr(desk, "_sender", lambda: _Sender())
    monkeypatch.setattr(desk, "_log", lambda *a, **kw: None)
    monkeypatch.setattr(desk._residue, "reset", lambda *a, **kw: None)
    monkeypatch.setattr(desk.W, "save_outbound_observation", lambda *a, **kw: None)
    import core.execution.local_text_receipt as ltr
    monkeypatch.setattr(ltr, "account_path", lambda *a, **kw: tmp_path / "wx.json")
    monkeypatch.setattr(ltr, "capture", lambda *a, **kw: (None, ""))
    import core.execution.wx_sender as wxs
    monkeypatch.setattr(wxs, "account_send_options", lambda *a, **kw: {})
    import core.features as features
    monkeypatch.setattr(features, "dry_run_send", lambda *a, **kw: False)
    return monkeypatch


def test_ledger_failure_is_flagged_without_faking_send(env):
    env.setattr(desk.W, "record_outbound", lambda *a, **kw: None)
    item = _item()
    assert desk.deliver(item) == (True, "unconfirmed", "")
    assert item["_post_send_ledger_missing"] is True


def test_execute_one_settles_ledger_failure_as_unverified(env, monkeypatch):
    item = _item()
    seen = {}

    class FakeThread:
        def __init__(self, *a, **kw):
            pass
        def start(self):
            pass

    monkeypatch.setattr(desk.threading, "Thread", FakeThread)
    monkeypatch.setattr(desk.W, "claim_next", lambda owner: dict(item))
    monkeypatch.setattr(desk, "deliver", lambda it, dry=False: (
        it.__setitem__("_post_send_ledger_missing", True) or True, "unconfirmed", ""))
    monkeypatch.setattr(desk._residue, "take_terminal", lambda qid: None)
    def settle(*a, **kw):
        seen.update(kw)
        seen["ok"] = a[1] if len(a) > 1 else kw.get("ok")
        return "unverified"
    monkeypatch.setattr(desk.W, "settle", settle)

    result = desk.execute_one(dry=False, owner="tester")
    assert result["state"] == "unverified"
    assert result["ok"] is False
    assert result["ack"] == "unconfirmed"
    assert "落账失败" in result["why"] and "不自动重发" in result["why"]
    assert seen["ok"] is False
    assert seen["not_executed"] is False


def test_ledger_success_keeps_send_result(env):
    env.setattr(desk.W, "record_outbound", lambda *a, **kw: 7)
    item = _item()
    assert desk.deliver(item) == (True, "unconfirmed", "")
    assert item.get("_post_send_ledger_missing") is not True
