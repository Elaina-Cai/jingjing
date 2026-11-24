from __future__ import annotations

from dataclasses import asdict

import pytest

from core.context import situation, situation_store as store
from core.context.cognitive_commit import Scope
from loop import goal_review
from core.context.goal_wake import (
    acknowledge_review,
    due_wakes,
    note_goal_signal,
    pending_wakes,
)

A = Scope("wechat", "a", "same")
NOW = "2026-11-08 10:00:00"
YESTERDAY = "2026-11-07 10:00:00"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    store.reset_world_models()
    yield
    store.reset_world_models()


def goal(**kwargs):
    model = store.get_world_model("same", scope=A)
    situation.open_intention(model, "goal", kind="promise", **kwargs)
    return model


def test_condition_signal_wakes_without_due_time_and_read_is_pure():
    model = goal(blocked_on="flight_status")
    assert pending_wakes(model) == ()
    assert note_goal_signal(
        model, "goal", source="condition", ref="flight:landed", at=NOW
    )
    first = pending_wakes(model)
    assert len(first) == 1
    assert first[0].reason == "condition"
    assert first[0].event_id
    before = list(model.events)
    for _ in range(5):
        assert pending_wakes(model) == first
    assert model.events == before
    assert acknowledge_review(first[0], at=NOW)
    assert pending_wakes(model) == ()


def test_new_evidence_matching_goal_ref_wakes():
    model = goal(evidence_refs=("ledger:42",))
    model.add(
        "fact_asserted",
        {"key": "payment", "value": "paid", "evidence_refs": ["ledger:42"]},
        at=NOW,
    )
    wakes = [item for item in pending_wakes(model) if item.reason == "evidence"]
    assert len(wakes) == 1
    assert wakes[0].event_id
    assert acknowledge_review(wakes[0], at=NOW)
    assert [item for item in pending_wakes(model) if item.reason == "evidence"] == []


def test_condition_key_event_wakes_blocked_goal():
    model = goal(blocked_on="flight_status")
    model.add("position_observed", {"key": "flight_status", "value": "landed"}, at=NOW)
    wakes = [item for item in pending_wakes(model) if item.reason == "condition"]
    assert len(wakes) == 1
    assert wakes[0].event_id


def test_unrelated_evidence_does_not_wake():
    model = goal(evidence_refs=("ledger:42",), blocked_on="flight_status")
    model.add(
        "fact_asserted",
        {"key": "weather", "value": "rain", "evidence_refs": ["ledger:99"]},
        at=NOW,
    )
    model.add("position_observed", {"key": "other", "value": "x"}, at=NOW)
    assert pending_wakes(model) == ()


def test_revoked_goal_source_is_not_a_review_candidate(monkeypatch):
    from core.memory import belief_commit

    model = goal(
        evidence_refs=("revoked-request",),
        review_at=NOW,
        desired_outcome="已经撤回的请求",
    )
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope, store=None: "dead" if ref == "revoked-request" else "unknown",
    )

    assert pending_wakes(model) == ()


def test_unknown_goal_source_wakes_with_a_visible_label(monkeypatch):
    from core.memory import belief_commit

    model = goal(
        evidence_refs=("missing-request",),
        review_at=NOW,
        desired_outcome="需要核对的请求",
    )
    monkeypatch.setattr(
        belief_commit,
        "evidence_state",
        lambda ref, scope, store=None: "unknown",
    )

    wakes = pending_wakes(model)

    assert len(wakes) == 1
    assert wakes[0].evidence_state == "unknown"
    assert "依据未核实" in goal_review.prompt([asdict(wakes[0])])


def test_old_signal_is_ignored_after_reopen_and_old_token_cannot_ack():
    model = goal()
    assert note_goal_signal(model, "goal", source="evidence", ref="old", at=YESTERDAY)
    old = pending_wakes(model)[0]
    situation.close_intention(model, "goal", at=NOW, state="abandoned")
    situation.open_intention(model, "goal", kind="promise", at=NOW)
    assert pending_wakes(model) == ()
    assert note_goal_signal(model, "goal", source="evidence", ref="new", at=NOW)
    new = pending_wakes(model)
    assert len(new) == 1 and new[0].token != old.token
    assert not acknowledge_review(old, at=NOW)
    assert acknowledge_review(new[0], at=NOW)


def test_signal_requires_open_goal_and_stable_ref():
    model = goal()
    assert not note_goal_signal(model, "missing", source="condition", ref="x")
    assert not note_goal_signal(model, "goal", source="condition", ref="")
    assert note_goal_signal(model, "goal", source="receipt", ref="job:1:done", at=NOW)
    assert not note_goal_signal(model, "goal", source="receipt", ref="job:1:done", at=NOW)
    wakes = pending_wakes(model)
    assert len(wakes) == 1 and wakes[0].reason == "receipt"


def test_future_signal_is_not_due_yet():
    model = goal()
    assert note_goal_signal(
        model, "goal", source="condition", ref="later", at="2026-11-09 10:00:00"
    )
    assert due_wakes({"same": model}, now=NOW) == ()
    assert pending_wakes(model)


def test_prompt_names_non_time_signal_reason():
    model = goal(blocked_on="flight_status")
    assert note_goal_signal(model, "goal", source="condition", ref="flight:landed", at=NOW)
    item = asdict(pending_wakes(model)[0])
    text = goal_review.prompt([item])
    assert "等待条件可能满足" in text


def test_many_old_promises_are_not_starved():
    model = store.get_world_model("same", scope=A)
    for index in range(40):
        situation.open_intention(
            model,
            f"old-{index}",
            kind="promise",
            due_at="2026-11-05 09:00:00",
        )
    keys = {item.goal_id for item in pending_wakes(model)}
    assert len(keys) == 40
    assert "old-0" in keys


def test_review_dispatcher_rotates_between_databases():
    class _Queue:
        def poll(self, db, *, now, allowed):
            return [{"goal_id": db, "token": db}]

    dispatcher = goal_review.ReviewDispatcher()
    dispatcher._queues["db1"] = _Queue()
    dispatcher._queues["db2"] = _Queue()

    assert dispatcher.poll(["db1", "db2"], now=NOW, allowed=True)[0] == "db1"
    assert dispatcher.poll(["db1", "db2"], now=NOW, allowed=True)[0] == "db2"
    assert dispatcher.poll(["db1", "db2"], now=NOW, allowed=True)[0] == "db1"


def test_three_day_old_promise_still_wakes():
    model = store.get_world_model("same", scope=A)
    situation.open_intention(
        model,
        "old-promise",
        kind="promise",
        due_at="2026-11-05 09:00:00",
    )
    wakes = pending_wakes(model)
    assert len(wakes) == 1
    assert wakes[0].goal_id == "old-promise"
    assert wakes[0].reason == "due"


def test_signal_survives_persist_and_reload():
    model = goal(blocked_on="flight_status")
    assert note_goal_signal(model, "goal", source="condition", ref="flight:landed", at=NOW)
    store.persist_world_model("same", scope=A)
    store.reset_world_models()
    reloaded = store.get_world_model("same", scope=A)
    wakes = [item for item in pending_wakes(reloaded) if item.reason == "condition"]
    assert len(wakes) == 1
    assert wakes[0].event_id
