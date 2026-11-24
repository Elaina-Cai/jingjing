"""WP-08 8.3：TimingPolicy 与旧窗口读数等价。

Run directly:
    python tests/test_timing_equivalence.py
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import timing  # noqa: E402
from legacy_brain.group_state import (  # noqa: E402
    coalesce_max_wait,
    coalesce_seconds,
    media_followup_seconds,
    pat_grace_max_wait,
    pat_grace_seconds,
    rhythm_seconds,
    unit_max_wait_seconds,
    unit_silence_seconds,
)

_ENV_KEYS = (
    "YINYUE_UNIT_SILENCE_SECONDS",
    "YINYUE_PAT_GRACE_SECONDS",
    "YINYUE_UNIT_MAX_WAIT_SECONDS",
    "YINYUE_UNIT_MEDIA_FOLLOWUP_SECONDS",
    "YINYUE_GROUP_COALESCE_SECONDS",
    "YINYUE_GROUP_COALESCE_MAX_WAIT",
    "YINYUE_PAT_GRACE_MAX_WAIT",
    "YINYUE_GROUP_STALE_MESSAGE_SECONDS",
)


@contextmanager
def _env(values: dict[str, str] | None = None):
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        for key, value in (values or {}).items():
            os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_defaults_and_old_readers_agree() -> bool:
    with _env():
        p = timing.get()
        assert p == timing.TimingPolicy(
            unit_silence=6.0, unit_max_wait=30.0, media_followup=15.0,
            deferred_wait=15.0, observation_window=3.0,
            rhythm=6.0,
        ), p
        assert unit_silence_seconds() == p.unit_silence
        assert unit_max_wait_seconds() == p.unit_max_wait
        assert media_followup_seconds() == p.media_followup
        assert rhythm_seconds() == p.rhythm
        assert pat_grace_seconds() == p.unit_silence
        assert p.coalesce == 4.0 and p.coalesce_max_wait == 12.0
        assert p.pat_grace_max_wait == 12.0 and p.stale_message == 1800.0
        assert p.merge_gap == 10.0 and p.merge_span == 30.0
        assert p.merge_max_messages == 5
        assert p.room_recent == 300.0 and p.self_ago == 90.0
        assert p.qa_gap == 60.0 and p.duplicate_window == 120.0
        assert coalesce_seconds() == p.coalesce
        assert coalesce_max_wait() == p.coalesce_max_wait
        assert pat_grace_max_wait() == p.pat_grace_max_wait
    return True


def test_env_overrides_and_alias() -> bool:
    with _env({
        "YINYUE_UNIT_SILENCE_SECONDS": "8",
        "YINYUE_UNIT_MAX_WAIT_SECONDS": "20",
        "YINYUE_UNIT_MEDIA_FOLLOWUP_SECONDS": "10",
    }):
        p = timing.get()
        assert p.unit_silence == 8.0, p
        assert p.unit_max_wait == 20.0, p
        assert p.media_followup == 10.0, p
        assert p.rhythm == 8.0, p
        assert unit_silence_seconds() == 8.0
        assert unit_max_wait_seconds() == 20.0
        assert media_followup_seconds() == 10.0
        assert rhythm_seconds() == 8.0

    with _env({"YINYUE_PAT_GRACE_SECONDS": "7"}):
        p = timing.get()
        assert p.unit_silence == 7.0 and p.rhythm == 7.0, p
        assert unit_silence_seconds() == 7.0
        assert pat_grace_seconds() == 7.0

    with _env({"YINYUE_UNIT_SILENCE_SECONDS": "9",
               "YINYUE_PAT_GRACE_SECONDS": "3"}):
        assert timing.get().unit_silence == 9.0, "新名优先"
    return True


def test_clamps_match_old_readers() -> bool:
    with _env({"YINYUE_UNIT_SILENCE_SECONDS": "0.01"}):
        assert timing.get().unit_silence == 0.05
    with _env({"YINYUE_UNIT_SILENCE_SECONDS": "99"}):
        assert timing.get().unit_silence == 15.0
    with _env({"YINYUE_UNIT_MAX_WAIT_SECONDS": "0"}):
        assert timing.get().unit_max_wait == 6.0, "上限不早于 N"
    with _env({"YINYUE_UNIT_MAX_WAIT_SECONDS": "9999"}):
        assert timing.get().unit_max_wait == 600.0
    with _env({"YINYUE_UNIT_MEDIA_FOLLOWUP_SECONDS": "0"}):
        assert timing.get().media_followup == 1.0
    with _env({"YINYUE_UNIT_MEDIA_FOLLOWUP_SECONDS": "99"}):
        assert timing.get().media_followup == 60.0
    return True


def test_rhythm_is_same_source_and_constants() -> bool:
    with _env({"YINYUE_UNIT_SILENCE_SECONDS": "11"}):
        p = timing.get()
        assert p.rhythm == p.unit_silence == 11.0, p
        assert p.deferred_wait == 15.0
        assert p.observation_window == 3.0
        assert rhythm_seconds() == unit_silence_seconds() == 11.0
    return True


def test_new_windows_env_and_clamps() -> bool:
    with _env({"YINYUE_GROUP_COALESCE_SECONDS": "0.5",
               "YINYUE_GROUP_COALESCE_MAX_WAIT": "8",
               "YINYUE_PAT_GRACE_MAX_WAIT": "9",
               "YINYUE_GROUP_STALE_MESSAGE_SECONDS": "120"}):
        p = timing.get()
        assert p.coalesce == 0.5 and p.coalesce_max_wait == 8.0, p
        assert p.pat_grace_max_wait == 9.0 and p.stale_message == 120.0, p
        assert coalesce_seconds() == 0.5
        assert coalesce_max_wait() == 8.0
        assert pat_grace_max_wait() == 9.0
    with _env({"YINYUE_GROUP_COALESCE_SECONDS": "99"}):
        assert timing.get().coalesce == 15.0
    with _env({"YINYUE_GROUP_COALESCE_MAX_WAIT": "1"}):
        assert timing.get().coalesce_max_wait == 4.0, "上限不早于合并窗口"
    with _env({"YINYUE_PAT_GRACE_MAX_WAIT": "1"}):
        assert timing.get().pat_grace_max_wait == 6.0, "上限不早于 N"
    with _env({"YINYUE_GROUP_STALE_MESSAGE_SECONDS": "0"}):
        assert timing.get().stale_message == 0.0, "0 = 关闸"
    with _env({"YINYUE_GROUP_STALE_MESSAGE_SECONDS": "abc"}):
        assert timing.get().stale_message == 1800.0, "读不动回落默认"
    return True


def test_single_owner_static_guard() -> bool:
    import pathlib

    timing_src = pathlib.Path("core/timing.py").read_text(encoding="utf-8")
    readers = (
        "legacy_brain/group_state.py",
        "core/context/room_state.py",
        "core/context/threads.py",
    )
    for env in ("YINYUE_GROUP_COALESCE_SECONDS", "YINYUE_GROUP_COALESCE_MAX_WAIT",
                "YINYUE_PAT_GRACE_MAX_WAIT", "YINYUE_GROUP_STALE_MESSAGE_SECONDS"):
        assert env in timing_src, env
        probe = f'os.environ.get("{env}")'
        for name in readers:
            src = pathlib.Path(name).read_text(encoding="utf-8")
            assert probe not in src, (name, env)
    room = pathlib.Path("core/context/room_state.py").read_text(encoding="utf-8")
    assert "RECENT_WINDOW = " not in room
    assert "SELF_AGO_WINDOW = " not in room
    assert "QA_GAP_LIMIT_SECONDS = " not in room
    threads = pathlib.Path("core/context/threads.py").read_text(encoding="utf-8")
    assert "_DUPLICATE_WINDOW_SECONDS = " not in threads
    return True


def main() -> bool:
    tests = [
        test_defaults_and_old_readers_agree,
        test_env_overrides_and_alias,
        test_clamps_match_old_readers,
        test_rhythm_is_same_source_and_constants,
        test_new_windows_env_and_clamps,
        test_single_owner_static_guard,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\ntiming equivalence: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
