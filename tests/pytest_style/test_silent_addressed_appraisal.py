"""决策四：有人对她说、她选择不回——rest 之后也要留一笔评价（用 rest 的 why，不额外调模型）。"""
import asyncio
from types import SimpleNamespace

from core.context import cognitive_commit as cc
from core.context import experience_commit
from loop import brain, ports


def _offer(ref="ev1"):
    return SimpleNamespace(reference=ref)


def _run(monkeypatch, *, kind="named", why="这一拍先不接，等他冷静",
         offers=None, dry_run=False, result="committed"):
    calls = []
    receipts = []
    monkeypatch.setattr(experience_commit, "submit",
                        lambda offer, payload, **kw: calls.append((offer, payload, kw)) or {"result": result})
    monkeypatch.setattr(brain.ledger, "append_receipt", lambda **kw: receipts.append(dict(kw)) or dict(kw))
    ctx = {"scope": cc.Scope("wechat", "acct", "c1"),
           "offers": {"ev1": _offer()} if offers is None else offers,
           "visible": frozenset({"ev1"})}
    token = ports._READ_SCOPE.set(ports.ReadScope("wechat", "acct", "c1", "你好", "u1"))
    try:
        observation, receipt = asyncio.run(brain._exec(
            "rest", {"why": why}, db="unused.db", dry_run=dry_run, turn_id="t1",
            focus="c1", addressed_kind=kind, experience_ctx=ctx))
    finally:
        ports._READ_SCOPE.reset(token)
    return observation, receipt, calls, receipts


def test_addressed_rest_records_why_she_did_not_reply(monkeypatch):
    _, _, calls, receipts = _run(monkeypatch)
    assert len(calls) == 1
    offer, payload, _ = calls[0]
    assert payload["mode"] == "appraisal"
    assert payload["meaning"] == "这一拍先不接，等他冷静"
    assert payload["next_move"] == "wait"
    appraisals = [row for row in receipts if row.get("act") == "evaluate_experience"]
    assert appraisals and appraisals[0]["result"] == "committed"


def test_unaddressed_rest_stays_a_noop(monkeypatch):
    _, _, calls, receipts = _run(monkeypatch, kind="")
    assert calls == []
    assert [row for row in receipts if row.get("act") == "evaluate_experience"] == []


def test_ambiguous_offers_do_not_record(monkeypatch):
    _, _, calls, _ = _run(monkeypatch, offers={"ev1": _offer(), "ev2": _offer("ev2")})
    assert calls == []


def test_rehearsal_rest_does_not_write(monkeypatch):
    _, _, calls, _ = _run(monkeypatch, dry_run=True)
    assert calls == []
