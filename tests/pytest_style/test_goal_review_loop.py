from dataclasses import asdict
from types import SimpleNamespace

import pytest

from core.context import situation, situation_store as store
from core.context.cognitive_commit import Scope
from core.context.goal_wake import pending_wakes
from loop import goal_review as review

NOW = "2026-11-08 10:00:00"
A = Scope("wechat", "a", "same")
DB = "isolated/platforms/wechat/a/account.db"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("DATA_DIR", "LOG_DIR", "PLATFORMS_DIR", "YINYUE_WORLD_MODEL_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("META_DB_PATH", str(tmp_path / "meta.db"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("YINYUE_GOAL_REVIEW_WAKE", "1")
    store.reset_world_models()
    yield
    store.reset_world_models()


def candidate(scope=A):
    model = store.get_world_model(scope.chat_id, scope=scope)
    situation.open_intention(model, "goal", kind="promise", review_at=NOW, desired_outcome="find sources")
    return asdict(pending_wakes(model)[0])


def reviewed(item):
    _, row = review.prepare({"goal_id": "goal", "conclusion": "仍需等待资料"}, [item])
    review.complete_receipts([row], completed=True, dry_run=False, rehearse=False)
    return row


def test_poll_does_not_consume_and_attempts_back_off():
    item = candidate()
    queue = review.ReviewQueue()
    assert queue.poll(DB, now=NOW, allowed=True, clock=0) == [item]
    assert queue.poll(DB, now=NOW, allowed=True, clock=0.4) == [item]
    queue.started([item], clock=1)
    assert queue.poll(DB, now=NOW, allowed=True, clock=2) == []
    assert queue.poll(DB, now=NOW, allowed=True, clock=62) == [item]
    assert review.finish([item], [reviewed(item)], dry_run=False, at=NOW) == 1
    assert queue.poll(DB, now=NOW, allowed=True, clock=68) == []


def test_restart_recovers_unfinished_attempt_but_not_completed_review():
    item = candidate()
    store.persist_world_model("same", scope=A)
    queue = review.ReviewQueue()
    queue.started([item], clock=0)
    store.reset_world_models()
    assert review.ReviewQueue().poll(DB, now=NOW, allowed=True, clock=1) == [item]
    assert review.finish([item], [reviewed(item)], dry_run=False, at=NOW) == 1
    store.reset_world_models()
    assert not review.ReviewQueue().poll(DB, now=NOW, allowed=True, clock=2)
    marker = store.get_world_model("same", scope=A).events[-1]
    assert marker.payload["conclusion"] == "仍需等待资料"


def test_disabled_or_quiet_or_stale_does_not_wake(monkeypatch):
    candidate()
    queue = review.ReviewQueue()
    assert not queue.poll(DB, now=NOW, allowed=False, clock=0)
    monkeypatch.setenv("YINYUE_GOAL_REVIEW_WAKE", "0")
    assert not queue.poll(DB, now=NOW, allowed=True, clock=0)


def test_dispatch_and_prompt_never_cross_accounts_or_chats():
    a = candidate()
    b = candidate(Scope("wechat", "b", "same"))
    assert review.ReviewQueue().poll(DB, now=NOW, allowed=True, clock=0) == [a]
    assert review.for_window([a, b], db=DB, platform="wechat", chat_id="same") == [a]
    assert not review.for_window([a], db=DB, platform="qq", chat_id="same")
    assert not review.for_window([a], db=DB, platform="wechat", chat_id="other")
    assert "find sources" in review.prompt([a])


@pytest.mark.parametrize("completed,dry,rehearse", [(False, False, False), (True, True, False), (True, False, True)])
def test_failed_or_dry_or_rehearsed_turn_keeps_candidate(completed, dry, rehearse):
    item = candidate()
    _, row = review.prepare({"goal_id": "goal", "conclusion": "等资料"}, [item])
    review.complete_receipts([row], completed=completed, dry_run=dry, rehearse=rehearse)
    assert review.finish([item], [row], dry_run=dry, at=NOW) == 0
    assert pending_wakes(store.get_world_model("same", scope=A))


@pytest.mark.parametrize("act", ["error", "timeout"])
def test_error_or_timeout_receipt_blocks_confirmation(act):
    item = candidate()
    assert not review.finish([item], [reviewed(item), {"act": act}], dry_run=False, at=NOW)


def test_empty_normal_return_and_wrong_goal_do_not_consume():
    item = candidate()
    assert not review.finish([item], [], dry_run=False, at=NOW)
    assert review.prepare({"goal_id": "other", "conclusion": "done"}, [item])[1]["result"] == "rejected"
    assert review.prepare({"goal_id": "goal", "conclusion": " "}, [item])[1]["result"] == "rejected"


def test_new_messages_take_priority_without_consuming_foreign_account_review():
    item = candidate()
    assert review.prioritize_messages("other/account.db", DB, [item], changed=True) == ("", [])
    assert review.prioritize_messages(DB, DB, [item], changed=True) == (DB, [item])
    assert review.prioritize_messages("other/account.db", DB, [item], changed=False) == (DB, [item])
    assert pending_wakes(store.get_world_model("same", scope=A))


def test_dispatcher_can_reach_second_account_while_first_is_backing_off():
    a, b = candidate(), candidate(Scope("wechat", "b", "same"))
    second = "isolated/platforms/wechat/b/account.db"
    dispatcher = review.ReviewDispatcher()
    assert dispatcher.poll([DB, second], now=NOW, allowed=True) == (DB, [a])
    dispatcher.started(DB, [a])
    assert dispatcher.poll([DB, second], now=NOW, allowed=True) == (second, [b])


@pytest.mark.parametrize("dry,timeout", [(False, False), (True, False), (False, True)])
def test_real_tick_routes_review_window_and_confirms_only_success(monkeypatch, dry, timeout):
    from loop import run
    from datetime import datetime
    class FixedTime(datetime):
        @classmethod
        def now(cls):
            return cls.fromisoformat(NOW)
    monkeypatch.setattr(run, "datetime", FixedTime)
    item = candidate()
    foreign = candidate(Scope("wechat", "a", "other"))
    snap = {"cells": [{"platform": "wechat", "chat_id": "same", "label": "friend", "after_mine": []}]}
    monkeypatch.setattr(run.snapshot, "build_snapshot", lambda db: snap)
    monkeypatch.setattr(run.snapshot, "render_text", lambda *args, **kwargs: "test world")
    monkeypatch.setattr(run.snapshot, "render_turns", lambda *args, **kwargs: "")
    monkeypatch.setattr(run, "pick_next", lambda *args, **kwargs: ("", {}))
    monkeypatch.setattr(run, "_hb_beat_once", lambda: None)
    monkeypatch.setattr(run, "_write_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(run.ports, "pick_db", lambda: "WRONG DEFAULT DB")
    monkeypatch.setattr(run.direct_send, "owner_ids", lambda: [])
    monkeypatch.setattr(run.direct_send, "check_deliveries", lambda: {})
    seen = []

    async def no_send(*args, **kwargs):
        return {}

    async def brain(db, text, **kwargs):
        seen.append((db, kwargs["chat_id"], kwargs["wake"]["goal_reviews"]))
        if timeout:
            raise run.asyncio.TimeoutError()
        return [reviewed(item)]

    async def wait_for(coro, **kwargs):
        return await coro

    monkeypatch.setattr(run.direct_send, "run", no_send)
    monkeypatch.setattr(run.brain, "run_loop", brain)
    monkeypatch.setattr(run.asyncio, "wait_for", wait_for)
    monkeypatch.setattr(run.ledger, "append_receipt", lambda **kwargs: kwargs)
    coro = run.one_tick(dry_run=dry, llm=object(), wake={"review_db": DB, "goal_reviews": [item, foreign]})
    try:
        with pytest.raises(StopIteration) as done:
            coro.send(None)
    finally:
        coro.close()
    assert done.value.value["windows"] == ["same"]
    assert seen == [(DB, "same", [item])]
    assert bool(pending_wakes(store.get_world_model("same", scope=A))) is (dry or timeout)
