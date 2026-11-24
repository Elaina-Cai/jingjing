"""say 顺带写这一拍的评价，不额外调模型。"""
from __future__ import annotations

import asyncio

from core.context import cognitive_commit as cc
from core.context import experience_commit
from loop import brain, direct_send

SCOPE = cc.Scope("wechat", "acct", "c1")


def _offer(reference: str) -> experience_commit.ExperienceOffer:
    return experience_commit.ExperienceOffer(
        scope=SCOPE, reference=reference, fingerprint="fp-" + reference,
        content="在等我回话", heads=(), goals=frozenset(), methods=frozenset())


def _wire_say(monkeypatch, *, ack: str = "confirmed", why: str = ""):
    async def say_many(parts, **kwargs):
        return brain.ports.Result(True, data={"ack": ack, "why": why})

    monkeypatch.setattr(brain.ports, "say_many", say_many)
    monkeypatch.setattr(brain.ports, "resolve_say_target", lambda target: ("c1", ""))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: "c1")
    monkeypatch.setattr(brain.ledger, "blocked_targets", lambda: {})
    monkeypatch.setattr(brain.ledger, "quiet_ms", lambda *args, **kwargs: None)
    monkeypatch.setattr(brain.ledger, "said_this_turn", lambda *args, **kwargs: False)
    monkeypatch.setattr(direct_send, "recently_sent", lambda *args, **kwargs: False)
    rows = []

    def append_receipt(**kwargs):
        rows.append(dict(kwargs))
        return dict(kwargs)

    monkeypatch.setattr(brain.ledger, "append_receipt", append_receipt)
    return rows


def _say(args: dict, ctx: dict | None):
    return asyncio.run(brain._exec(
        "say", {"target": "c1", "text": "我在，你说。", **args},
        db="unused", dry_run=False, turn_id="turn-say", experience_ctx=ctx))


def _eval_rows(rows):
    return [row for row in rows if row.get("act") == "evaluate_experience"]


def test_one_offer_makes_meaning_required_in_say():
    tools = brain._turn_schemas({"evt-1": _offer("evt-1")})
    say = next(tool["function"] for tool in tools if tool["function"]["name"] == "say")
    assert "meaning" in say["parameters"]["required"]
    assert "meaning 必填" in say["description"]
    # 共享的 SCHEMAS 不能被这一拍改掉
    base = next(tool["function"] for tool in brain.SCHEMAS if tool["function"]["name"] == "say")
    assert "meaning" not in base["parameters"]["required"]


def test_no_offer_or_two_offers_keep_meaning_optional():
    empty = brain._turn_schemas({})
    say = next(tool["function"] for tool in empty if tool["function"]["name"] == "say")
    assert "meaning" not in say["parameters"]["required"]
    two = brain._turn_schemas({"evt-1": _offer("evt-1"), "evt-2": _offer("evt-2")})
    say2 = next(tool["function"] for tool in two if tool["function"]["name"] == "say")
    assert "meaning" not in say2["parameters"]["required"]


def test_say_success_commits_one_appraisal_with_feeling_and_next_move(monkeypatch):
    calls = []
    monkeypatch.setattr(
        experience_commit, "submit",
        lambda offer, payload, **kwargs: calls.append((offer, payload, kwargs)) or {"result": "committed"})
    rows = _wire_say(monkeypatch)

    _, receipt = _say(
        {"meaning": "他在等我的答复", "feeling": "有点紧张", "next_move": "reply"},
        {"offers": {"evt-1": _offer("evt-1")}, "scope": SCOPE,
         "visible": frozenset({"evt-1"})})

    assert receipt["result"] == "sent"
    assert len(calls) == 1
    offer, payload, kwargs = calls[0]
    assert offer.reference == "evt-1"
    assert payload == {"mode": "appraisal", "evidence_ref": "evt-1",
                       "meaning": "他在等我的答复", "feeling_text": "有点紧张",
                       "next_move": "reply"}
    assert kwargs == {"scope": SCOPE, "visible": frozenset({"evt-1"}), "dry_run": False}
    assert [(row["result"], row["why"]) for row in _eval_rows(rows)] == [("committed", "他在等我的答复")]


def test_say_without_meaning_does_not_commit(monkeypatch):
    calls = []
    monkeypatch.setattr(experience_commit, "submit",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or {"result": "committed"})
    rows = _wire_say(monkeypatch)

    _, receipt = _say({"feeling": "有点紧张"},
                      {"offers": {"evt-1": _offer("evt-1")}, "scope": SCOPE,
                       "visible": frozenset({"evt-1"})})

    assert receipt["result"] == "sent"
    assert calls == []
    assert _eval_rows(rows) == []


def test_a_non_string_meaning_is_ignored(monkeypatch):
    calls = []
    monkeypatch.setattr(experience_commit, "submit",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or {"result": "committed"})
    rows = _wire_say(monkeypatch)

    _, receipt = _say({"meaning": {"text": "他其实想被听见"}},
                      {"offers": {"evt-1": _offer("evt-1")}, "scope": SCOPE,
                       "visible": frozenset({"evt-1"})})

    assert receipt["result"] == "sent"
    assert calls == []
    assert _eval_rows(rows) == []


def test_two_offers_skip_and_leave_a_receipt(monkeypatch):
    calls = []
    monkeypatch.setattr(experience_commit, "submit",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or {"result": "committed"})
    rows = _wire_say(monkeypatch)

    _, receipt = _say(
        {"meaning": "还没定下来"},
        {"offers": {"evt-1": _offer("evt-1"), "evt-2": _offer("evt-2")},
         "scope": SCOPE, "visible": frozenset({"evt-1", "evt-2"})})

    assert receipt["result"] == "sent"
    assert calls == []
    skipped = _eval_rows(rows)
    assert len(skipped) == 1
    assert skipped[0]["result"] == "skipped"
    assert "2" in skipped[0]["blocked_reason"]


def test_content_gate_skipped_say_does_not_commit(monkeypatch):
    calls = []
    monkeypatch.setattr(experience_commit, "submit",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or {"result": "committed"})
    rows = _wire_say(monkeypatch, ack="skipped", why="称呼更正没通过")

    _, receipt = _say({"meaning": "差点说错"},
                      {"offers": {"evt-1": _offer("evt-1")}, "scope": SCOPE,
                       "visible": frozenset({"evt-1"})})

    assert receipt["result"] == "skipped"
    assert calls == []
    assert _eval_rows(rows) == []
