# -*- coding: utf-8 -*-
"""② 群体记忆（梗）投影检查：standalone + pytest 兼容。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.context.situation as st  # noqa: E402
from core.context.situation import WorldModel  # noqa: E402


def test_note_meme_lands_and_merges():
    model = WorldModel()
    st.note_meme(model, key="魔↗术↘技↘巧↗", kind="pat_suffix", count=1, evidence="拍一拍", at="10:00")
    st.note_meme(model, key="魔↗术↘技↘巧↗", kind="pat_suffix", count=2, at="10:05")
    view = st.project(model.events)
    assert len(view.memes) == 1
    m = view.memes[0]
    assert m.key == "魔↗术↘技↘巧↗"
    assert m.count == 3
    assert m.first_at == "10:00" and m.last_at == "10:05"
    assert m.evidence == "拍一拍"


def test_tail_cap():
    model = WorldModel()
    for i in range(st.MEME_TAIL_MAX + 5):
        st.note_meme(model, key=f"m{i}", at=f"10:{i:02d}")
    assert len(st.project(model.events).memes) == st.MEME_TAIL_MAX


def test_empty_and_missing_key():
    model = WorldModel()
    st.note_meme(model, key="", at="10:00")
    assert len(st.project(model.events).memes) == 0


def test_kind_and_count_defaults():
    model = WorldModel()
    st.note_meme(model, key="x", at="10:00")
    m = st.project(model.events).memes[0]
    assert m.kind == "" and m.count == 1


def test_registered_in_kinds_and_all():
    assert "meme_observed" in st.EVENT_KINDS
    assert "note_meme" in st.__all__
    assert "Meme" in st.__all__


TESTS = [
    test_note_meme_lands_and_merges,
    test_tail_cap,
    test_empty_and_missing_key,
    test_kind_and_count_defaults,
    test_registered_in_kinds_and_all,
]


def main() -> bool:
    ok = True
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
            ok = False
        else:
            print(f"PASS {test.__name__}")
    if ok:
        print(f"meme-projection checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
