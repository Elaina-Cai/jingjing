"""全自动外发前的最后一道闸（用户 m02886 选的「全 C」）。

这些用例只认行为：同一句话在同一个会话里不许发第二遍；回话不受 300 秒限制；
自发开口受限制；`dry_run` 不占窗口；被拦下来是 `ack="skipped"`（一条可记账的裁决），
不是失败。
"""

from __future__ import annotations

import asyncio

import pytest

from loop import outbound_gate as gate
from loop import ports


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch):
    gate.reset()
    yield
    gate.reset()


def test_the_same_line_twice_is_blocked():
    assert gate.check("c1", "我先去问一下场地", now=1000.0) == (True, "ok")
    gate.remember("c1", "我先去问一下场地", now=1000.0)
    assert gate.check("c1", "我先去问一下场地", now=1001.0) == (False, "duplicate")


def test_a_different_full_stop_does_not_make_a_new_line():
    gate.remember("c1", "问了，等他回", now=1000.0)
    assert gate.check("c1", "问了，等他回。", now=1001.0)[0] is False


def test_another_chat_is_not_affected():
    gate.remember("c1", "收到", now=1000.0)
    assert gate.check("c2", "收到", now=1001.0) == (True, "ok")


def test_after_the_window_it_is_allowed_again():
    gate.remember("c1", "收到", now=1000.0)
    assert gate.check("c1", "收到", now=1000.0 + gate.DEDUPE_WINDOW_S) == (True, "ok")


def test_empty_text_is_never_sendable():
    assert gate.check("c1", "   ", now=1000.0) == (False, "empty_text")


def test_remember_without_a_chat_is_ignored():
    gate.remember("", "收到", now=1000.0)
    assert gate.check("", "收到", now=1000.1) == (True, "ok")


def test_spontaneous_speaking_is_throttled_per_chat():
    gate.remember("c1", "第一句", reason="spont", now=1000.0)
    assert gate.check("c1", "第二句", reason="spont", now=1100.0) == (False, "too_soon")
    assert gate.check("c1", "第二句", reason="spont", now=1000.0 + gate.PROACTIVE_GAP_S) == (True, "ok")


def test_a_reply_is_not_throttled_by_the_proactive_gap():
    gate.remember("c1", "第一句", reason="spont", now=1000.0)
    assert gate.check("c1", "回答你的问题", reason="reply", now=1001.0) == (True, "ok")


def test_reset_clears_everything():
    gate.remember("c1", "收到", reason="spont", now=1000.0)
    gate.reset()
    assert gate.check("c1", "收到", reason="spont", now=1001.0) == (True, "ok")


def _stub_send(monkeypatch):
    sent = []

    def _enqueue(gid, chat, items, **kw):
        sent.append((chat, [str(i.get("text") or "") for i in items]))
        return list(range(1, len(items) + 1))

    import db.world_db as wdb

    monkeypatch.setattr(wdb, "enqueue", _enqueue)
    monkeypatch.setattr(ports, "resolve_chat_id", lambda c: c)
    from loop import speak_policy

    monkeypatch.setattr(speak_policy, "allowed", lambda *a, **k: (True, "ok"))
    return sent


def test_say_sends_once_then_reports_a_skip(monkeypatch):
    sent = _stub_send(monkeypatch)
    one = asyncio.run(ports.say("我去问一下场地", dry_run=False, chat_id="c1"))
    two = asyncio.run(ports.say("我去问一下场地", dry_run=False, chat_id="c1"))
    assert one.ok is True and one.data["ack"] == "queued"
    assert two.ok is True and two.data["ack"] == "skipped" and two.data["why"] == "duplicate"
    assert sent == [("c1", ["我去问一下场地"])]


def test_a_dry_run_does_not_use_up_the_window(monkeypatch):
    sent = _stub_send(monkeypatch)
    asyncio.run(ports.say("我去问一下场地", dry_run=True, chat_id="c1"))
    real = asyncio.run(ports.say("我去问一下场地", dry_run=False, chat_id="c1"))
    assert real.data["ack"] == "queued"
    assert len(sent) == 1


def test_say_many_is_skipped_as_a_whole(monkeypatch):
    sent = _stub_send(monkeypatch)
    first = asyncio.run(ports.say_many(["问了", "等他回"], dry_run=False, chat_id="c1"))
    second = asyncio.run(ports.say_many(["问了", "等他回"], dry_run=False, chat_id="c1"))
    assert first.data["ack"] == "queued"
    assert second.data["ack"] == "skipped" and second.data["bubbles"] == 2
    assert len(sent) == 1
