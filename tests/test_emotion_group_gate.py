# -*- coding: utf-8 -*-
"""群聊情绪信号门（2026-10-16）：别人互聊的 ambient 回合不计正面积分。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import emotion as em  # noqa: E402


class _Data:
    def __init__(self, **kw):
        self.is_group = True
        self.explicit_address = False
        self.summons = ()
        self.about_me = ""
        self.window_rows = ()
        self.admitted_rows = ()
        self.self_names = ("鲸鲸",)
        self.now = None
        for k, v in kw.items():
            setattr(self, k, v)


def test_group_ambient_no_signal():
    sig = em.derive_behavior_signals(_Data())
    assert sig.owner_initiated is False
    assert sig.replied_to_her is False and sig.waited_minutes == 0.0


def test_group_addressed_and_about_me_signal():
    assert em.derive_behavior_signals(_Data(explicit_address=True)).owner_initiated is True
    assert em.derive_behavior_signals(_Data(summons=("pat",))).owner_initiated is True
    assert em.derive_behavior_signals(_Data(about_me="me")).owner_initiated is True


def test_group_ambient_update_writes_nothing():
    from core.context.situation import WorldModel

    model = WorldModel()
    out = em.update_emotion(model, data=_Data(), section=None, at="2026-10-16 01:00")
    assert out is None and not model.events


def test_private_path_unchanged():
    class _P(_Data):
        def __init__(self):
            super().__init__()
            self.is_group = False

    assert em.derive_behavior_signals(_P()).owner_initiated is False


TESTS = [
    test_group_ambient_no_signal,
    test_group_addressed_and_about_me_signal,
    test_group_ambient_update_writes_nothing,
    test_private_path_unchanged,
]


def main() -> bool:
    ok = True
    for t in TESTS:
        try:
            t()
        except AssertionError as exc:
            print(f"FAIL {t.__name__}: {exc}")
            ok = False
        else:
            print(f"PASS {t.__name__}")
    if ok:
        print(f"emotion group gate checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
