"""引用一条媒体时的渲染（2026-10-27）。

事故：Elaina 引用鲸鲸自己发的表情包问「你能看见这个表情包吗」，她回「我这边只看到
一串编号」。微信把被引用的媒体写成定位串

    [引用 Elaina: wxid_seed01:1793059376769:1:9b1203029ed1f0f8cbbf7178bf846bd2::0]

而 loop/ports.py、loop/snapshot.py、core/context/chat_window.py 三处渲染器都只认字面量
``[图片]``，于是整串定位串原样进了 prompt。这里钉住解析、渲染与"查不到就原样"的规矩。
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

if __name__ == "__main__":  # 直接 `python tests/x.py` 时也要能 import core
    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)

from contextlib import contextmanager

from core.conversation import media_ref

KEY = "9b1203029ed1f0f8cbbf7178bf846bd2"
LOCATOR = ("[引用 Elaina: wxid_seed01:1793059376769:1:%s::0]" % KEY)
TEXT = LOCATOR + "\n你能看见这个表情包吗"
DESC = "黄色小玩偶戴蓝帽趴在粉色胡萝卜图案枕头上。"


@contextmanager
def _offline():
    """这段用例不碰生产库、不问视觉模型（mini_pytest 下也能用）。"""
    saved = (media_ref._from_account_db, media_ref._from_emoji_cache,
             media_ref._from_media_row, media_ref._ask_vision)
    media_ref._from_account_db = lambda md5: ""
    media_ref._from_emoji_cache = lambda md5: ""
    media_ref._from_media_row = lambda **kw: ""
    media_ref._ask_vision = lambda path, *, mode, budget_s: ""
    media_ref._MEM.clear()
    media_ref.reset_budget()
    try:
        yield
    finally:
        (media_ref._from_account_db, media_ref._from_emoji_cache,
         media_ref._from_media_row, media_ref._ask_vision) = saved
        media_ref._MEM.clear()


def test_parse_locator():
    (loc,) = media_ref.parse_locators(LOCATOR)
    assert loc.md5 == KEY
    assert loc.author == "Elaina"
    assert loc.chat_key == "wxid_seed01"
    assert loc.ts_ms == 1793059376769
    assert loc.moment.year == 2026


def test_parse_ignores_plain_text():
    assert media_ref.parse_locators("今天好热") == []
    assert media_ref.parse_locators("[引用 Elaina: [图片]]\n这是啥") == []


def test_has_locator():
    assert media_ref.has_locator(TEXT) is True
    assert media_ref.has_locator("今天好热") is False


def test_render_uses_description_table():
    with _offline():
        descs = {KEY + ".gif": DESC}
        out = media_ref.render_media_locators(TEXT, descs=descs)
        assert out == "[引用 Elaina: [图片: %s]]\n你能看见这个表情包吗" % DESC


def test_render_keeps_raw_without_description():
    with _offline():
        out = media_ref.render_media_locators(TEXT, descs={})
        assert out == TEXT


def test_render_plain_quoted_image_uses_own_path():
    with _offline():
        text = "[引用 Elaina: [图片]]\n你能看见这个表情包吗"
        descs = {"9b12.gif": DESC}
        out = media_ref.render_media_locators(text, descs=descs, path="data/image_cache/9b12.gif")
        assert out == "[引用 Elaina: [图片: %s]]\n你能看见这个表情包吗" % DESC


def test_render_leaves_other_text_alone():
    with _offline():
        assert media_ref.render_media_locators("今天好热", descs={KEY: DESC}) == "今天好热"


def test_describe_sticker_is_short():
    with _offline():
        descs = {KEY + ".gif": "x" * 80}
        assert len(media_ref.describe_sticker(KEY, descs=descs)) <= media_ref._STICKER_DESC


def test_chat_window_needs_media_desc():
    from core.context.chat_window import _needs_media_desc

    assert _needs_media_desc(TEXT) is True
    assert _needs_media_desc("[图片]") is True
    assert _needs_media_desc("今天好热") is False


def test_appattach_gif_keeps_md5_not_file_label():
    # 2026-10-27 实案：她引用一张 gif 问「能看见吗」，鲸鲸只能答「就一个字 文件」。
    from core.conversation.quote_text import clean_quote_text

    xml = ('<?xml version="1.0"?><msg><appmsg><title></title><type>6</type>'
           '<appattach><fileext>gif</fileext><md5>' + KEY + '</md5></appattach>'
           '</appmsg></msg>')
    assert clean_quote_text(xml) == "[图片:%s]" % KEY


def test_appattach_file_keeps_name():
    from core.conversation.quote_text import clean_quote_text

    xml = ('<?xml version="1.0"?><msg><appmsg><title>app-release.apk</title>'
           '<appattach><fileext>apk</fileext><md5>' + "0" * 32 + '</md5></appattach>'
           '</appmsg></msg>')
    assert clean_quote_text(xml) == "[文件: app-release.apk]"


def test_md5_tag_rendered_to_description():
    with _offline():
        text = "[引用 Elaina: [图片:%s]]" % KEY
        descs = {KEY + ".gif": DESC}
        assert media_ref.render_md5_tags(text, descs=descs) == "[引用 Elaina: [图片: %s]]" % DESC
        # 查不到描述时不能把 md5 漏给模型看（换一张没被记进内存缓存的 md5）
        unknown = "[引用 Elaina: [图片:%s]]" % ("0" * 32)
        assert media_ref.render_md5_tags(unknown, descs={}) == "[引用 Elaina: [图片]]"


def test_is_image_path():
    assert media_ref.is_image_path("data/image_cache/%s.gif" % KEY) is True
    assert media_ref.is_image_path("data/x/app-release.apk") is False


def main() -> int:
    import traceback

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("ok   " + name)
        except Exception:  # noqa: BLE001 - 这里是测试入口
            failed += 1
            print("FAIL " + name)
            traceback.print_exc()
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
