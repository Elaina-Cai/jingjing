from dataclasses import replace

import pytest

from core.context import situation, situation_store as store
from core.context.cognitive_commit import Scope
from core.context.goal_wake import acknowledge_review, due_wakes, next_wake, pending_wakes

A = Scope("wechat", "a", "same")
B = Scope("wechat", "b", "same")
NOW = "2026-11-08 10:00:00"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    store.reset_world_models()
    yield
    store.reset_world_models()


def goal(scope=A, **kwargs):
    model = store.get_world_model("same", scope=scope)
    situation.open_intention(model, "goal", kind="promise", review_at=kwargs.pop("review_at", NOW), **kwargs)
    return model


def test_reading_and_polling_do_not_consume_or_complete():
    model = goal()
    before = list(model.events)
    first = next_wake({"same": model})
    for _ in range(10):
        assert next_wake({"same": model}) == first
        assert due_wakes({"same": model}, now=NOW) == (first,)
    assert model.events == before
    assert situation.open_intentions(model)[0].state == "active"


def test_review_acknowledgement_survives_restart_and_is_idempotent():
    model = goal()
    candidate = pending_wakes(model)[0]
    assert acknowledge_review(candidate, at=NOW)
    assert not acknowledge_review(candidate, at=NOW)
    store.reset_world_models()
    model = store.get_world_model("same", scope=A)
    assert pending_wakes(model) == ()
    assert situation.open_intentions(model)[0].state == "active"
    assert len([event for event in model.events if event.kind == "goal_reviewed"]) == 1


@pytest.mark.parametrize("raises", [False, True])
def test_failed_save_keeps_review_pending(monkeypatch, raises):
    model = goal()
    candidate = pending_wakes(model)[0]
    with monkeypatch.context() as patch:
        def save(*args, **kwargs):
            if raises:
                raise OSError("test disk failure")
            return False
        patch.setattr(store, "persist_world_model", save)
        assert not acknowledge_review(candidate, at=NOW)
    assert pending_wakes(model) == (candidate,)
    assert not any(event.kind == "goal_reviewed" for event in model.events)
    assert acknowledge_review(candidate, at=NOW)


def test_old_token_cannot_acknowledge_a_changed_review():
    model = goal()
    old = pending_wakes(model)[0]
    goal(review_at="2026-11-09 10:00:00")
    assert not acknowledge_review(old, at=NOW)
    current = pending_wakes(model)[0]
    assert current.token != old.token
    assert not acknowledge_review(current, at=NOW)


def test_identical_reopened_goal_gets_a_new_review_token():
    model = goal()
    old = pending_wakes(model)[0]
    assert acknowledge_review(old, at=NOW)
    goal()
    new = pending_wakes(model)[0]
    assert new.token != old.token
    assert store.persist_world_model("same", scope=A)
    store.reset_world_models()
    assert pending_wakes(store.get_world_model("same", scope=A)) == (new,)
    assert not acknowledge_review(old, at=NOW)


def test_cognitive_close_rejects_acknowledgement_even_with_stale_cache():
    from core.context import cognitive_commit as cc
    model = goal()
    candidate = pending_wakes(model)[0]
    conn = cc.connect()
    try:
        assert cc.commit_updates(conn, context=cc.CommitContext(A, visible_event_ids=frozenset({"fact"})), proposals=[cc.Proposal("p", "goal", "close", {"state": "abandoned"}, ("fact",))], idempotency_key="close").ok
    finally:
        conn.close()
    assert not acknowledge_review(candidate, at=NOW)
    assert not any(event.kind == "goal_reviewed" for event in model.events)


def test_same_named_goals_in_other_accounts_are_independent():
    first, other = goal(A), goal(B)
    a, b = pending_wakes(first)[0], pending_wakes(other)[0]
    assert a.token != b.token
    assert acknowledge_review(a, at=NOW)
    assert pending_wakes(other) == (b,)
    assert not acknowledge_review(replace(a, scope=B.key), at=NOW)


@pytest.mark.parametrize("state", ["proposed", "paused", "completed", "abandoned"])
def test_inactive_goal_does_not_wake(state):
    assert not pending_wakes(goal(state=state))


def test_closed_goal_cannot_consume_stale_wake():
    model = goal()
    candidate = pending_wakes(model)[0]
    model.add("intention_closed", {"key": "goal", "state": "abandoned"})
    assert not pending_wakes(model)
    assert not acknowledge_review(candidate, at=NOW)


def test_explicit_review_takes_priority_over_due_time():
    model = goal(review_at="2026-11-09 10:00:00", due_at="2026-11-07 10:00:00")
    assert not due_wakes({"same": model}, now=NOW)
    assert next_wake({"same": model}).reason == "review"


def test_due_date_without_review_is_only_a_review_candidate():
    model = goal(review_at="", due_at="2026-11-07 10:00:00")
    candidate = pending_wakes(model)[0]
    assert candidate.reason == "due"
    assert acknowledge_review(candidate, at=NOW)
    assert situation.open_intentions(model)


@pytest.mark.parametrize("value", ["invalid", "2026-11-08T10:00:00+08:00"])
def test_invalid_or_mixed_timezone_time_is_not_guessed(value):
    assert pending_wakes(goal(review_at=value)) == ()
    with pytest.raises(ValueError, match="invalid_wake_now"):
        due_wakes({}, now=value)


def test_unknown_conversation_does_not_produce_a_wake():
    model = situation.WorldModel()
    situation.open_intention(model, "goal", review_at=NOW)
    assert next_wake({"same": model}) is None
