# -*- coding: utf-8 -*-
"""引用内容清洗检查（standalone + pytest 兼容）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.conversation.quote_text import clean_quote_text  # noqa: E402

NESTED = (
    '<?xml version="1.0"?><msg><appmsg appid="" sdkver="0">'
    "<title>[引用 鲸鲸: @P5 在。刚醒，怎么了？]\n起来嗨</title>"
    "<type>57</type></appmsg></msg>"
)
NESTED_ONLY_QUOTE = (
    '<?xml version="1.0"?><msg><appmsg><title>[引用 鲸鲸: @P5 在。刚醒，怎么了？]</title></appmsg></msg>'
)


def test_plain_text_passthrough() -> None:
    assert clean_quote_text("起来嗨") == "起来嗨"


def test_name_prefix_stripped() -> None:
    assert clean_quote_text("P5:\n起来嗨") == "起来嗨"


def test_nested_xml_quote_keeps_text() -> None:
    assert clean_quote_text(NESTED) == "起来嗨"


def test_nested_xml_only_quote_returns_quote_line() -> None:
    got = clean_quote_text(NESTED_ONLY_QUOTE)
    assert got.startswith("[引用") and "起来嗨" not in got


def test_image_xml_placeholder() -> None:
    xml = '<?xml version="1.0"?><msg><img aeskey="abc" cdnmidimgurl="http://x"/></msg>'
    assert clean_quote_text(xml) == "[图片]"


def test_emoji_xml_placeholder() -> None:
    xml = '<?xml version="1.0"?><msg><emoji md5="abc"/></msg>'
    assert clean_quote_text(xml) == "[表情]"


def test_unknown_xml_media_placeholder() -> None:
    xml = '<?xml version="1.0"?><msg><unknown/></msg>'
    assert clean_quote_text(xml) == "[媒体消息]"


def test_empty() -> None:
    assert clean_quote_text("") == ""
    assert clean_quote_text("  ") == ""


def test_content_extract_and_platform_use_owner() -> None:
    conv = (ROOT / "core/ingestion/platform_messages.py").read_text(encoding="utf-8")
    extract = (ROOT / "db/content_extract.py").read_text(encoding="utf-8")
    assert "clean_quote_text" in conv and "clean_quote_text" in extract
    assert "ref_content = \"[媒体消息]\"" not in extract


TESTS = [
    test_plain_text_passthrough,
    test_name_prefix_stripped,
    test_nested_xml_quote_keeps_text,
    test_nested_xml_only_quote_returns_quote_line,
    test_image_xml_placeholder,
    test_emoji_xml_placeholder,
    test_unknown_xml_media_placeholder,
    test_empty,
    test_content_extract_and_platform_use_owner,
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
        print(f"quote-text checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
