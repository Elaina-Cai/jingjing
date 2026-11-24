"""同一段经历反复读取，不得反复改变关系或完成承诺。"""
import json

import pytest

from loop import bond, brain, ledger
from core.infra import actions


@pytest.mark.parametrize("message_count", [0, 1, 12])
def test_speaking_context_is_read_only(tmp_path, monkeypatch, message_count):
    state = tmp_path / "bond.json"
    original = json.dumps({"group:test": {"warmth": 0.5}}, ensure_ascii=False).encode()
    state.write_bytes(original)
    monkeypatch.setattr(bond, "STATE", str(state), raising=False)
    monkeypatch.setattr(ledger, "recent_say_targets", lambda **kw: [
        {"target": "group:test", "says": 1, "gap_s": 60, "last_at": 1000}])
    monkeypatch.setattr(brain.ports, "label_for", lambda cid: "测试群")
    monkeypatch.setattr(brain.ports, "incoming_since", lambda *a: message_count)
    monkeypatch.setattr(brain.ports, "peer_events_since", lambda *a, **kw: [
        {"chat_id": "group:test", "text": "A 和 B 在聊别的事"}])
    for _ in range(100):
        text = brain._speaking_block()
        assert "测试群" in text
        assert state.read_bytes() == original
    assert "这个人之间" not in text
    assert "凉" not in text
    if message_count:
        assert "不代表" in text


def test_speaking_context_for_a_turn_hides_other_conversations(monkeypatch):
    monkeypatch.setattr(ledger, "recent_say_targets", lambda **kw: [
        {"target": "group:test", "says": 1, "gap_s": 60, "last_at": 1000},
        {"target": "private:other", "says": 4, "gap_s": 20, "last_at": 1100},
    ])
    monkeypatch.setattr(brain.ports, "label_for",
                        lambda cid: {"group:test": "测试群", "private:other": "别的私聊"}[cid])
    monkeypatch.setattr(brain.ports, "incoming_since", lambda *args: 0)
    monkeypatch.setattr(brain.ports, "peer_events_since", lambda *args, **kwargs: [])

    text = brain._speaking_block("group:test")
    assert "测试群" in text
    assert "别的私聊" not in text
    assert "private:other" not in text


def test_querying_promises_does_not_close_them(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DIR", str(tmp_path))
    promise = ledger.record_promise(who="测试群", what="找两篇资料", chat_id="group:test")
    path = actions.path()
    before = open(path, "rb").read()
    # 即使旧的会话探针认为有人说话，也不应触发完成。
    monkeypatch.setattr(ledger, "_peer_spoke_after", lambda *a: True, raising=False)
    for _ in range(100):
        assert [p["id"] for p in ledger.open_promises()] == [promise["id"]]
        assert "找两篇资料" in ledger.render_promises()
        assert open(path, "rb").read() == before


def test_explicit_closure_still_hides_closed_promise(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "DIR", str(tmp_path))
    promise = ledger.record_promise(who="测试", what="准备资料", chat_id="private:test")
    ledger.close_promise(promise["id"], reason="对应执行记录已完成")
    before = open(actions.path(), "rb").read()
    assert ledger.open_promises() == []
    assert ledger.render_promises() == ""
    assert open(actions.path(), "rb").read() == before
