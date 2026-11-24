"""WorldState (B2) checks: one window read feeds both the prompt and the gate.

Run directly: python tests/test_world_state.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.chat_window import WindowMessage  # noqa: E402
from core.context.world_state import build_turn_world, sha12  # noqa: E402

BASE = datetime(2026, 9, 23, 13, 31, 0)


def _rows() -> list[WindowMessage]:
    """The real 13:32 other-pair QA case: Elaina asks, P7 answers."""
    return [
        WindowMessage(BASE + timedelta(seconds=29), "Elaina", "选班干部吗"),
        WindowMessage(BASE + timedelta(seconds=57), "P7", "青协招新"),
    ]


class _Patch:
    """Patch chat_window.window_enabled_for for one test."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.saved = None

    def __enter__(self):
        import core.context.chat_window as cw

        self.saved = cw.window_enabled_for
        cw.window_enabled_for = lambda chat_id: self.enabled
        return cw

    def __exit__(self, *exc):
        import core.context.chat_window as cw

        cw.window_enabled_for = self.saved


def test_one_load_feeds_everything() -> bool:
    print("\n[单次读取：窗口块与局况同源]")
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return _rows()

    with _Patch(True):
        world = build_turn_world(
            "c1", now=BASE + timedelta(seconds=60),
            want_room_block=True, loader=loader,
        )
    checks = [
        ("窗口只读了一次", calls["n"] == 1),
        ("source=window", world.source == "window"),
        ("rows=2", world.rows == 2),
        ("rows_hash 有值", bool(world.rows_hash)),
        ("窗口块含两条原文",
         "选班干部吗" in world.window_text and "青协招新" in world.window_text),
        ("window_hash 自洽", world.window_hash == sha12(world.window_text)),
        ("局况从同一份 rows 派生",
         world.situation is not None
         and world.situation.turns
         and world.situation.turns[0].asker == "Elaina"),
    ]
    return _report(checks)


def test_gate_and_block_share_one_situation() -> bool:
    print("\n[闸门与渲染读同一个 situation 对象]")
    with _Patch(True):
        world = build_turn_world(
            "c1", now=BASE + timedelta(seconds=60),
            want_room_block=True, loader=_rows,
        )
    # R0：看戏门退役——world 只负责装配，不再有「该不该接」的读数
    checks = [
        ("看戏门调用面已删除", not hasattr(world, "should_skip")),
        ("room_block 就是 situation.render()",
         world.room_block == world.situation.render()),
        ("room_hash 自洽", world.room_hash == sha12(world.room_block)),
    ]
    return _report(checks)


def test_window_off_falls_back() -> bool:
    print("\n[窗口关：返回 none，交给调用方回落 buffer]")
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return _rows()

    with _Patch(False):
        world = build_turn_world("c1", now=BASE, loader=loader)
    checks = [
        ("不读窗口", calls["n"] == 0),
        ("source=none", world.source == "none"),
        ("situation=None", world.situation is None),
        ("window_text 为空", world.window_text == ""),
        ("局面为空（无 situation 可判）", world.situation is None),
    ]
    return _report(checks)


def test_as_dict_json_safe() -> bool:
    print("\n[as_dict 是 JSON 安全的小载荷]")
    with _Patch(True):
        world = build_turn_world(
            "c1", now=BASE + timedelta(seconds=60),
            want_room_block=True, loader=_rows,
        )
    payload = world.as_dict()
    dumped = json.dumps(payload, ensure_ascii=False)
    checks = [
        ("可 json.dumps", bool(dumped)),
        ("rows_hash 在", payload["rows_hash"] == world.rows_hash),
        ("不含大文本", "选班干部吗" not in dumped),
        ("situation 是布尔", payload["situation"] is True),
    ]
    return _report(checks)



def _report(checks: list[tuple[str, bool]]) -> bool:
    ok = True
    for name, good in checks:
        ok &= bool(good)
        print(f"  {'✓' if good else '✗'} {name}")
    return bool(ok)


def main() -> bool:
    print("=" * 60)
    print("world state checks")
    print("=" * 60)
    tests = [
        test_one_load_feeds_everything,
        test_gate_and_block_share_one_situation,
        test_window_off_falls_back,
        test_as_dict_json_safe,
    ]
    results = []
    for test in tests:
        try:
            results.append((test.__name__, bool(test())))
        except Exception:  # pragma: no cover - diagnostics only
            import traceback

            traceback.print_exc()
            results.append((test.__name__, False))
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    print(f"{passed}/{len(results)} passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
