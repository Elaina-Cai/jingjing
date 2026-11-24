"""观察状态过滤：作废的观察（redacted/superseded）对任何人都不可见。

背景（2026-09-22 清垃圾观察时发现的两个叠加 bug）：
1. ``observation_visible_to`` 的 owner 分支在 status 检查**之前**直接放行，
   于是 redacted/superseded 的条目照样进画像；
2. 清库时写的 ``status='archived'`` 不是合法枚举（active/superseded/expired/redacted），
   会被兜底当成 active —— 所以状态值必须用枚举里那四个。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.contracts.events import ChatType  # noqa: E402
from core.contracts.person_memory import (  # noqa: E402
    ObservationStatus,
    ObservationVisibility,
    PersonObservation,
)
from core.people.visibility import observation_visible_to  # noqa: E402

CHAT = "10000000002@chatroom"


def _obs(status: str) -> PersonObservation:
    return PersonObservation(
        observation_id="obs1",
        person_id="p1",
        platform="wechat",
        account_id="wxid_bot",
        occurred_scope_type=ChatType.GROUP,
        occurred_scope_id=CHAT,
        visibility=ObservationVisibility.GROUP_SHARED,
        status=ObservationStatus(status),
    )


def _visible(obs: PersonObservation, *, owner: bool) -> bool:
    return observation_visible_to(
        obs,
        platform="wechat",
        account_id="wxid_bot",
        chat_type=ChatType.GROUP,
        chat_id=CHAT,
        viewer_person_id="wxid_member",
        viewer_is_owner=owner,
    )


def test_active_is_visible_to_both() -> None:
    assert _visible(_obs("active"), owner=False) is True
    assert _visible(_obs("active"), owner=True) is True


def test_redacted_is_invisible_even_to_owner() -> None:
    assert _visible(_obs("redacted"), owner=False) is False
    assert _visible(_obs("redacted"), owner=True) is False


def test_superseded_and_expired_are_invisible() -> None:
    for status in ("superseded", "expired"):
        assert _visible(_obs(status), owner=True) is False, status
        assert _visible(_obs(status), owner=False) is False, status


def test_status_enum_has_no_archived() -> None:
    """状态值只有四个；写 'archived' 会被兜底成 active（清库时踩过）。"""
    values = {member.value for member in ObservationStatus}
    assert values == {"active", "superseded", "expired", "redacted"}
    assert "archived" not in values


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
