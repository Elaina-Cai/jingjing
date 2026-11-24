# -*- coding: utf-8 -*-
"""meme provider（⑩ 按需召回 · ② 一侧）检查：standalone + pytest 兼容。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context.situation import WorldModel, note_meme  # noqa: E402
from core.memory.hub import MemoryHub, build_default_memory_hub  # noqa: E402
from core.memory.providers import MemeMemoryProvider  # noqa: E402


class _P:
    chat_id = "c1"


def _hub():
    hub = MemoryHub()
    hub.register(MemeMemoryProvider())
    return hub


def test_registered_in_default_hub():
    assert "meme" in build_default_memory_hub().kinds()


def test_recall_matches_and_ranks():
    import core.context.situation_store as store

    model = WorldModel()
    note_meme(model, key="魔↗术↘技↘巧↗", kind="phrase", count=9, evidence="拍 P3", at="2026-10-15 10:00")
    note_meme(model, key="没神之眼", count=2, evidence="原神", at="2026-10-15 10:01")
    original = store.get_world_model
    store.get_world_model = lambda chat_id: model if chat_id == "c1" else None
    try:
        items = _hub().recall(_P(), "魔术技巧", kinds=["meme"])
        assert len(items) == 1 and items[0].kind == "meme"
        assert "魔" in items[0].content
        all_items = _hub().recall(_P(), "", kinds=["meme"], limit=5)
        assert len(all_items) == 2
        assert all_items[0].importance >= all_items[1].importance
    finally:
        store.get_world_model = original


def test_recall_empty_when_no_model_or_no_match():
    import core.context.situation_store as store

    original = store.get_world_model
    store.get_world_model = lambda chat_id: None
    try:
        assert _hub().recall(_P(), "x", kinds=["meme"]) == []
    finally:
        store.get_world_model = original
    model = WorldModel()
    note_meme(model, key="魔↗术↘技↘巧↗", at="2026-10-15 10:00")
    store.get_world_model = lambda chat_id: model if chat_id == "c1" else None
    try:
        assert _hub().recall(_P(), "不存在的词", kinds=["meme"]) == []
    finally:
        store.get_world_model = original


def test_provider_purity():
    src = (ROOT / "core/memory/providers.py").read_text(encoding="utf-8")
    assert "class MemeMemoryProvider" in src


TESTS = [
    test_registered_in_default_hub,
    test_recall_matches_and_ranks,
    test_recall_empty_when_no_model_or_no_match,
    test_provider_purity,
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
        print(f"meme-provider checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
