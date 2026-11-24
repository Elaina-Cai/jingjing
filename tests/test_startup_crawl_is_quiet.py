"""Re-opening the app must not make her answer old messages in the group.

Why this file exists
--------------------
The owner described the symptom exactly: *"我一打开程序，自动爬取历史消息 … 然后
就疯狂抢鼠标"*, and the group saw a generic assistant-style line addressed to her
inside the room.

The live database shows what happened.  On 2026-09-15 a startup wrote 161 rows
whose ``created_at`` ran from **2026-05-30 to 2026-08-29** -- three months of
history -- each stamped ``synced_at = now()``, i.e. "just arrived".  The
visibility boundary (``hide_before_ts``) existed, but it was only applied when
``bootstrap_mode`` was true: a table whose cursor was reset to -1 (a table seen
for the first time, or the ``sync_table_cursor_reset`` path) gets a full
re-scan, and outside bootstrap every replayed row was published as new.

Two independent guarantees are pinned down here:

1. In ``db.sync_service``, the boundary is ``legacy_sync_ts`` and it is applied
   to rows on *their own timestamp*, whatever scan found them.
2. In ``legacy_brain.group_state``, a message that is already old when it arrives can
   never trigger a turn -- so even a row that slips past the sync layer cannot
   become a live reply.

Run directly: python tests/test_startup_crawl_is_quiet.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.routing.turn_decision import (  # noqa: E402
    ROUTE_SEGMENT_AMBIENT,
    route_segment,
)


# ------------------------------------------------------- the trigger-level gate

def _msg(**kw):
    from legacy_brain.group_state import BufferedMessage

    defaults = dict(
        sender_id="wxid_seed01",
        sender_name="蔡神",
        content="你在干啥",
        created_at=datetime.now(),
    )
    defaults.update(kw)
    return BufferedMessage(**defaults)


def test_old_message_does_not_trigger() -> None:
    """A message from months ago is history, and history does not speak."""
    from legacy_brain.group_state import GroupState

    state = GroupState(chat_id="g1")
    old = _msg(created_at=datetime.now() - timedelta(days=90))
    assert state.classify_trigger(old).action == "silent"


def test_fresh_message_still_triggers() -> None:
    """The gate must not silence live conversation."""
    from legacy_brain.group_state import GroupState

    state = GroupState(chat_id="g1")
    fresh = _msg(content="今天天气真好", created_at=datetime.now())
    decision = state.classify_trigger(fresh)
    assert decision.action == "reply_now", decision
    assert route_segment(decision) == ROUTE_SEGMENT_AMBIENT, decision


def test_owner_history_does_not_trigger_either() -> None:
    """Being the owner is no reason to answer three-month-old chat."""
    from legacy_brain.group_state import GroupState

    state = GroupState(chat_id="g1")
    old_owner = _msg(
        content="今天天气真好",
        is_owner=True,
        created_at=datetime.now() - timedelta(days=30),
    )
    assert state.classify_trigger(old_owner).action == "silent"


def test_stale_gate_runs_before_mention() -> None:
    """An old @ still must not fire: it is not a live summons."""
    from legacy_brain.group_state import GroupState

    state = GroupState(chat_id="g1")
    old_mention = _msg(
        mention_self=True, created_at=datetime.now() - timedelta(days=7)
    )
    assert state.classify_trigger(old_mention).action == "silent"


def test_slow_delivery_is_not_treated_as_history() -> None:
    """A few minutes late is normal (paused process, slow media). Keep it live."""
    from legacy_brain.group_state import GroupState

    state = GroupState(chat_id="g1")
    recent = _msg(content="今天天气真好", created_at=datetime.now() - timedelta(minutes=5))
    decision = state.classify_trigger(recent)
    assert decision.action == "reply_now", decision
    assert route_segment(decision) == ROUTE_SEGMENT_AMBIENT, decision


def test_unreadable_timestamp_fails_open() -> None:
    """A broken timestamp must not silently mute a live message."""
    from legacy_brain.group_state import GroupState

    state = GroupState(chat_id="g1")
    broken = _msg(content="今天天气真好", created_at=None)
    decision = state.classify_trigger(broken)
    assert decision.action == "reply_now", decision
    assert route_segment(decision) == ROUTE_SEGMENT_AMBIENT, decision


def test_gate_can_be_disabled_by_env() -> None:
    """``YINYUE_GROUP_STALE_MESSAGE_SECONDS=0`` turns the gate off."""
    from legacy_brain.group_state import GroupState

    state = GroupState(chat_id="g1")
    old = _msg(content="今天天气真好", created_at=datetime.now() - timedelta(days=365))
    os.environ["YINYUE_GROUP_STALE_MESSAGE_SECONDS"] = "0"
    try:
        decision = state.classify_trigger(old)
        assert decision.action == "reply_now", decision
        assert route_segment(decision) == ROUTE_SEGMENT_AMBIENT, decision
    finally:
        os.environ.pop("YINYUE_GROUP_STALE_MESSAGE_SECONDS", None)


def test_gate_threshold_is_env_tunable() -> None:
    from legacy_brain.group_state import GroupState

    state = GroupState(chat_id="g1")
    msg = _msg(content="今天天气真好", created_at=datetime.now() - timedelta(minutes=10))
    os.environ["YINYUE_GROUP_STALE_MESSAGE_SECONDS"] = "60"
    try:
        assert state.classify_trigger(msg).action == "silent"
    finally:
        os.environ.pop("YINYUE_GROUP_STALE_MESSAGE_SECONDS", None)


# -------------------------------------------------- the sync-level boundary

def test_sync_boundary_is_not_gated_on_bootstrap() -> None:
    """Visibility must never again be written as ``if bootstrap_mode``.

    Read the source rather than the runtime: the point is structural.  The old
    shape was a plan key zeroed outside bootstrap plus a per-row test on that
    key, which meant a full table re-scan (cursor reset to -1) published
    months-old rows as new.
    """
    from pathlib import Path

    text = (
        Path(__file__).resolve().parent.parent / "db" / "sync_service.py"
    ).read_text(encoding="utf-8")
    assert "hide_before_ts\": int(legacy_sync_ts) if bootstrap_mode else 0" not in text, (
        "the visibility boundary is conditional on bootstrap_mode again"
    )
    assert "plan.get(\"hide_before_ts\", 0)" not in text, (
        "the per-row visibility test is back on the plan key"
    )
    # Every row must be judged on its own timestamp against a boundary.
    assert "if boundary and wx_msg.create_time <= boundary:" in text, (
        "the per-row visibility test is no longer timestamp-based"
    )
    # ...and the boundary must be chosen per chat outside bootstrap, so one
    # chat's traffic cannot silence another.
    assert "_seen_boundary_for(talker)" in text, (
        "a plain re-scan must use a per-chat boundary"
    )


def test_rolled_back_boundary_is_not_above_every_row() -> None:
    """The rolled-back path must not set a boundary newer than the whole DB.

    That branch only runs when the source's newest row is already behind
    ``last_sync_ts``; using ``last_sync_ts`` as the visibility line would put
    it above every row and hide all of them, silencing her permanently.
    """
    from pathlib import Path

    text = (
        Path(__file__).resolve().parent.parent / "db" / "sync_service.py"
    ).read_text(encoding="utf-8")
    assert "legacy_sync_ts = last_sync_ts\n" not in text, (
        "the rolled-back branch is setting the boundary to last_sync_ts again"
    )
    assert "legacy_sync_ts = min(last_sync_ts, db_max_ts)" in text, (
        "the rolled-back boundary should be the older of the two"
    )


def test_sync_boundary_has_a_non_bootstrap_source() -> None:
    """Outside bootstrap the boundary is still derived, from the newest row."""
    from pathlib import Path

    text = (
        Path(__file__).resolve().parent.parent / "db" / "sync_service.py"
    ).read_text(encoding="utf-8")
    assert "else:" in text and "sync_boundary_probe_failed" in text, (
        "no non-bootstrap boundary probe; a cursor reset would replay history"
    )


def main() -> bool:
    tests = [
        test_old_message_does_not_trigger,
        test_fresh_message_still_triggers,
        test_owner_history_does_not_trigger_either,
        test_stale_gate_runs_before_mention,
        test_slow_delivery_is_not_treated_as_history,
        test_unreadable_timestamp_fails_open,
        test_gate_can_be_disabled_by_env,
        test_gate_threshold_is_env_tunable,
        test_sync_boundary_is_not_gated_on_bootstrap,
        test_rolled_back_boundary_is_not_above_every_row,
        test_sync_boundary_has_a_non_bootstrap_source,
    ]
    failed = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print("\nall passed" if not failed else f"\nfailed: {', '.join(failed)}")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
