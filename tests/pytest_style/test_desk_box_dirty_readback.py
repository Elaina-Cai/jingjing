"""框里还有字，但账号库里已经有这条：按成功记，别把已送达记成失败。

2026-11-22 实测：10:16 那条 quiet_alert 在 messages 表里落库了，键盘侧却报
「发送后输入框仍有文本」，outbound_queue 里那条一直挂着 unverified。
"""
from __future__ import annotations

import pytest

from loop import desk


class _DirtySender:
    def send_text(self, who, text, **kw):
        return {"ok": False, "error": "发送后输入框仍有文本，请检查微信焦点/输入法"}


def _item():
    return {"id": 77, "attempts": 1, "owner": "tester", "kind": "text",
            "chat_id": "room@chatroom", "platform": "wechat", "account_id": "acct",
            "payload": {"text": "hello"}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(desk, "_bootstrap", lambda: None)
    monkeypatch.setattr(desk, "_dry", lambda chat: False)
    monkeypatch.setattr(desk, "_label", lambda chat: "room")
    monkeypatch.setattr(desk, "_sender", lambda: _DirtySender())
    logs = []
    monkeypatch.setattr(desk, "_log", lambda name, **kw: logs.append((name, kw)))
    monkeypatch.setattr(desk._residue, "reset", lambda *a, **kw: None)
    monkeypatch.setattr(desk.W, "save_outbound_observation", lambda *a, **kw: None)
    monkeypatch.setattr(desk.W, "record_outbound", lambda *a, **kw: 123)
    monkeypatch.setattr(desk.W, "bind_observation_receipt", lambda *a, **kw: True)
    monkeypatch.setattr(desk, "_confirm_queue_later", lambda *a, **kw: None)
    import core.execution.local_text_receipt as ltr

    monkeypatch.setattr(ltr, "account_path", lambda *a, **kw: tmp_path / "wx.db")
    monkeypatch.setattr(ltr, "capture", lambda *a, **kw: (5, "tok"))
    calls: dict = {}

    def fake_observe(path, chat, text, after, *, source_token=None):
        calls.update({"chat": chat, "text": text, "after": after, "token": source_token})
        return "9"

    monkeypatch.setattr(ltr, "observe", fake_observe)
    import core.execution.wx_sender as wxs

    monkeypatch.setattr(wxs, "account_send_options", lambda *a, **kw: {})
    import core.features as features

    monkeypatch.setattr(features, "dry_run_send", lambda *a, **kw: False)
    return logs, calls


def test_dirty_box_with_local_record_counts_as_sent(env):
    logs, calls = env
    ok, ack, why = desk.deliver(_item())
    assert ok is True and why == ""
    assert calls["after"] == 5 and calls["text"] == "hello" and calls["chat"] == "room@chatroom"
    assert any(name == "desk_box_dirty_but_delivered" for name, _ in logs)


def test_dirty_box_without_local_record_stays_failed(env, monkeypatch):
    logs, _ = env
    import core.execution.local_text_receipt as ltr

    monkeypatch.setattr(ltr, "observe", lambda *a, **kw: "")
    ok, ack, why = desk.deliver(_item())
    assert ok is False and "输入框仍有文本" in why

class _PreSendSender:
    def send_text(self, who, text, **kw):
        return {"ok": False, "error": "发送前检测到聊天窗口已变化，已中止发送", "not_executed": True}


def test_pre_send_failure_is_marked_not_executed(env, monkeypatch):
    """回车之前就失败，队列才有资格重排；不带这个证据的消息会被当「可能已发出」。"""
    logs, _ = env
    monkeypatch.setattr(desk, "_sender", lambda: _PreSendSender())
    out = desk.deliver(_item())
    assert out.ok is False and out.not_executed is True


def test_post_enter_failure_is_not_marked_not_executed(env, monkeypatch):
    """回车之后失败一律不重排（可能已经发出）。"""
    logs, _ = env
    import core.execution.local_text_receipt as ltr

    monkeypatch.setattr(ltr, "observe", lambda *a, **kw: "")
    out = desk.deliver(_item())
    assert out.ok is False and out.not_executed is False
