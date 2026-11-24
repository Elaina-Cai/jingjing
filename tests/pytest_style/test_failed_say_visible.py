"""想发但没发出去的句子，下一拍必须看得见。

2026-11-22 16:06：她写好了「这张是身份证背面……赶紧撤回」，投递端在标题闸中止。
下一拍「最近开口」只列已确认的句子，失败那条完全不可见——她可能把没说出去的话当成说过了。
"""
from __future__ import annotations

import asyncio
import time

from loop import brain, ledger


def _stamp(ago_s: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - ago_s))


def test_recent_failed_says_keeps_only_recent_failures(monkeypatch):
    rows = [
        {"act": "say", "result": "failed", "target": "c1", "at": _stamp(60),
         "blocked_reason": "标题读不到"},
        {"act": "say", "result": "sent", "target": "c1", "at": _stamp(30)},
        {"act": "say", "result": "failed", "target": "c2", "at": _stamp(7200),
         "blocked_reason": "太久了"},
    ]
    monkeypatch.setattr(ledger, "read_receipts", lambda limit=300: rows)
    monkeypatch.setattr(ledger, "_current_receipt_scope", lambda: ())
    monkeypatch.setattr(ledger, "_receipt_in_scope", lambda row, scope: True)
    got = ledger.recent_failed_says(window_min=60)
    assert [row["target"] for row in got] == ["c1"]
    assert "标题读不到" in got[0]["why"]


def test_speaking_block_tells_her_the_send_failed(monkeypatch):
    monkeypatch.setattr(ledger, "recent_say_targets", lambda window_min=60: [])
    monkeypatch.setattr(ledger, "recent_failed_says",
                        lambda window_min=60: [{"target": "c1", "at": 0.0,
                                                "why": "打开的会话不是目标", "text": ""}])
    monkeypatch.setattr(brain.ports, "label_for", lambda cid: "Elaina")
    out = brain._speaking_block("c1")
    assert "没发出去" in out
    assert "打开的会话不是目标" in out
    assert "别当成已经说过" in out


def test_skipped_say_keeps_the_content_gate_reason(monkeypatch):
    calls = []

    async def say_many(parts, **kwargs):
        calls.append(list(parts))
        return brain.ports.Result(True, data={"ack": "skipped", "why": "称呼更正没通过"})

    monkeypatch.setattr(brain.ports, "say_many", say_many)
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: "c1")
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: ("c1", ""))
    monkeypatch.setattr(brain.ports, "label_for", lambda chat_id: "Elaina")
    monkeypatch.setattr(brain.ledger, "blocked_targets", lambda: {})
    monkeypatch.setattr(brain.ledger, "quiet_ms", lambda *args, **kwargs: None)
    monkeypatch.setattr(brain.ledger, "said_this_turn", lambda *args, **kwargs: False)
    from loop import direct_send
    monkeypatch.setattr(direct_send, "recently_sent", lambda *args, **kwargs: False)

    _, receipt = asyncio.run(brain._exec(
        "say", {"target": "c1", "text": "称呼更正一下", "why": "更正称呼"},
        db="unused", dry_run=False, turn_id="turn-skipped",
    ))
    assert calls == [["称呼更正一下"]]
    assert receipt["result"] == "skipped"
    assert "称呼更正没通过" in receipt["blocked_reason"]
    failed = ledger.recent_failed_says(window_min=60)
    assert any("称呼更正没通过" in row["why"] for row in failed)
    block = brain._speaking_block("c1")
    assert "称呼更正没通过" in block
    assert "别当成已经说过" in block
