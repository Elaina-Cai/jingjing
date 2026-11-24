"""Owner-named stickers must be described with the owner's own wording.

Why this file exists
--------------------
Two systems describe the same picture and they never talked to each other.

* The **send** side (``db.sticker_store``) holds the owner's names -- "太菜了",
  "喝奶茶，看戏" -- so the model can ask for one by name.
* The **receive** side (``core.preprocess.emoji_recognition``) described
  incoming stickers with whatever the vision model guessed for the picture.

The owner asked that their own wording win whenever a sticker they set up is
recognised.  It never did: the description cache held only vision output, and
nothing ever loaded the owner's names, so "太菜了" came back as "奖杯无奈叹气".

The join key is the md5, which lives in the template filename
(``templates/<md5>.png``) and is the same md5 an inbound message carries.

Run directly: python tests/test_owner_sticker_description.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _owner_index() -> dict:
    from db import sticker_store

    return sticker_store.owner_description_index("wechat", None)


def test_owner_index_is_keyed_by_md5() -> None:
    """Every owner-named sticker contributes its template md5."""
    index = _owner_index()
    assert index, "expected the live registry to name at least one sticker"
    for md5, emotion in index.items():
        assert re.fullmatch(r"[0-9a-f]{32}", md5), md5
        assert emotion and emotion.strip() == emotion, emotion


def test_owner_name_wins_over_vision_description() -> None:
    """A named sticker reads back as the owner's words, not the model's guess."""
    index = _owner_index()
    if not index:
        return
    from core.preprocess import emoji_recognition as er

    # Pick a sticker the owner named, and give the vision cache a *different*
    # description for the same md5: the owner's wording still has to win.
    md5, emotion = next(iter(sorted(index.items())))
    cache = er._load_cache()
    previous = cache.get(md5)
    try:
        cache[md5] = "视觉模型瞎猜的描述"
        assert er.get_emoji_description(md5) == emotion
        assert er.replace_emoji_descriptions(f"[表情包:{md5}]") == (
            f"[表情包:{emotion}]"
        )
    finally:
        if previous is None:
            cache.pop(md5, None)
        else:
            cache[md5] = previous


def test_unnamed_sticker_falls_back_to_vision() -> None:
    """Stickers the owner never set up keep the vision description."""
    index = _owner_index()
    from core.preprocess import emoji_recognition as er

    cache = er._load_cache()
    unnamed = [k for k in cache if k not in index]
    if not unnamed:
        return
    md5 = sorted(unnamed)[0]
    desc = cache[md5]
    er._reset_owner_index()
    assert er.get_emoji_description(md5) == desc
    assert er.replace_emoji_descriptions(f"[表情包:{md5}]") == f"[表情包:{desc}]"


def test_unknown_md5_left_alone() -> None:
    """An md5 nobody has described survives untouched."""
    from core.preprocess import emoji_recognition as er

    md5 = "f" * 32
    assert er.get_emoji_description(md5) is None
    assert er.replace_emoji_descriptions(f"[表情包:{md5}]") == f"[表情包:{md5}]"


def test_replace_is_cheap_when_nothing_to_do() -> None:
    """No placeholder -> no registry read, and the text comes back verbatim."""
    from core.preprocess import emoji_recognition as er

    er._reset_owner_index()
    assert er._owner_index_loaded is False
    text = "普通文本，没有表情包占位符"
    assert er.replace_emoji_descriptions(text) == text
    assert er._owner_index_loaded is False, (
        "a text with no placeholder must not pay for the registry read"
    )


def test_owner_index_survives_a_broken_registry() -> None:
    """A registry that cannot be read degrades, it does not break the cache."""
    from core.preprocess import emoji_recognition as er
    from db import sticker_store

    original = sticker_store.owner_description_index
    try:
        def _boom(*_a, **_kw):
            raise RuntimeError("registry unavailable")

        sticker_store.owner_description_index = _boom
        er._reset_owner_index()
        assert er._load_owner_index() == {}
        # The cache path still answers.
        cache = er._load_cache()
        if cache:
            md5 = sorted(cache)[0]
            assert er.get_emoji_description(md5) == cache[md5]
    finally:
        sticker_store.owner_description_index = original
        er._reset_owner_index()


def test_md5_case_is_normalised() -> None:
    """An upper-case md5 names the same sticker as its lower-case form.

    Only one side used to be normalised, so a named sticker could silently
    fall back to the vision guess purely because of letter case.
    """
    index = _owner_index()
    if not index:
        return
    from core.preprocess import emoji_recognition as er

    md5, emotion = next(iter(sorted(index.items())))
    assert er.get_emoji_description(md5.upper()) == emotion
    assert er.replace_emoji_descriptions(f"[表情包:{md5.upper()}]") == (
        f"[表情包:{emotion}]"
    )


def main() -> bool:
    tests = [
        test_owner_index_is_keyed_by_md5,
        test_owner_name_wins_over_vision_description,
        test_unnamed_sticker_falls_back_to_vision,
        test_unknown_md5_left_alone,
        test_replace_is_cheap_when_nothing_to_do,
        test_owner_index_survives_a_broken_registry,
        test_md5_case_is_normalised,
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
