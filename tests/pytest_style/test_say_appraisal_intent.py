"""say 顺带写评价：除了 meaning/feeling/next_move，也要能写「意图」（tendency）。

2026-11-24：`cognitive_events` 里 19 条 appraise 的 tendency 全是空——不是模型不写，
是 `say` 的 schema 和 `_commit_say_appraisal` 都没接这一项。这里钉住三件事：
1. say 的工具表里有 tendency；
2. 填了就随评价一起提交；
3. 不填/非字符串不会把评价写坏。
"""
from types import SimpleNamespace

from core.context import cognitive_commit as cc
from core.context import experience_commit
from loop import brain


def _offer(ref="ev1"):
    return SimpleNamespace(reference=ref)


def _run_say(monkeypatch, args, *, result="sent"):
    calls = []
    monkeypatch.setattr(experience_commit, "submit",
                        lambda offer, payload, **kw: calls.append((offer, payload, kw)) or {"result": "committed"})
    monkeypatch.setattr(brain.ledger, "append_receipt", lambda **kw: kw)
    ctx = {"scope": cc.Scope("wechat", "acct", "c1"), "offers": {"ev1": _offer()},
           "visible": frozenset({"ev1"})}
    brain._commit_say_appraisal(args, {"result": result}, ctx, turn_id="t", dry_run=False)
    return calls


def test_say_schema_carries_tendency():
    say = next(tool["function"] for tool in brain.SCHEMAS if tool["function"]["name"] == "say")
    assert "tendency" in say["parameters"]["properties"]
    assert "tendency" in say["description"]


def test_say_appraisal_payload_keeps_the_intent(monkeypatch):
    calls = _run_say(monkeypatch, {"meaning": "他在等我回话", "feeling": "有点紧张",
                                   "tendency": "先把话说清楚，再问他急不急",
                                   "next_move": "clarify"})
    assert len(calls) == 1
    payload = calls[0][1]
    assert payload["tendency"] == "先把话说清楚，再问他急不急"
    assert payload["next_move"] == "clarify"
    assert payload["feeling_text"] == "有点紧张"


def test_missing_or_non_string_intent_does_not_break_the_appraisal(monkeypatch):
    for extra in ({}, {"tendency": 123}, {"tendency": "   "}):
        calls = _run_say(monkeypatch, {"meaning": "他在等我回话", **extra})
        assert len(calls) == 1
        assert "tendency" not in calls[0][1]

def test_say_can_record_what_is_still_unresolved(monkeypatch):
    calls = _run_say(monkeypatch, {"meaning": "他还在气头上", "unresolved": "先问清他到底在意哪一句",
                                   "next_move": "clarify"})
    assert len(calls) == 1
    assert calls[0][1]["unresolved_condition"] == "先问清他到底在意哪一句"


def test_say_schema_carries_unresolved():
    say = next(tool["function"] for tool in brain.SCHEMAS if tool["function"]["name"] == "say")
    assert "unresolved" in say["parameters"]["properties"]
    assert "unresolved" in say["description"]
