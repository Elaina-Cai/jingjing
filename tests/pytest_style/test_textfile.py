# -*- coding: utf-8 -*-
"""写回契约（CB-069）的测试：归一化不改变含义、blockers 说人话、不合格一个字节都不写。"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.infra import textfile  # noqa: E402

CARD = textfile.Contract(name="测试卡", max_bytes=200, head_keys=("name", "voice", "looks"))


def _good(body="正文。"):
    return "name: x\nvoice: y\nlooks: z\n---\n" + body + "\n"


def test_normalize_crlf_and_cr():
    assert textfile.normalize("a\r\nb\rc\n") == "a\nb\nc\n"


def test_normalize_trims_only_trailing_blank_lines():
    assert textfile.normalize("a\n\n\n") == "a\n"
    assert textfile.normalize("a\n   \nb\n") == "a\n   \nb\n"


def test_blockers_size_uses_utf8_bytes_not_chars():
    raw = _good("字" * 100)
    assert any("超过上限" in b for b in textfile.blockers(raw, CARD))


def test_blockers_missing_head_key():
    raw = "name: x\nlooks: z\n---\n正文\n"
    assert any("voice" in b for b in textfile.blockers(raw, CARD))


def test_blockers_missing_terminator():
    assert any("分隔行" in b for b in textfile.blockers("name: x\n正文\n", CARD))


def test_empty_optional_head_value_is_legal():
    raw = "name: x\nvoice: \nlooks: \n---\n正文\n"
    assert textfile.blockers(raw, CARD) == [], textfile.blockers(raw, CARD)


def test_empty_name_is_not_legal():
    raw = "name: \nvoice: y\nlooks: z\n---\n正文\n"
    assert any("name" in b for b in textfile.blockers(raw, CARD))


def test_blockers_empty_body():
    raw = "name: x\nvoice: y\nlooks: z\n---\n   \n"
    assert any("正文是空的" in b for b in textfile.blockers(raw, CARD))


def test_blockers_trailing_blank_lines_and_no_final_newline():
    assert any("尾部" in b for b in textfile.blockers(_good() + "\n", CARD))
    assert any("末尾没有换行" in b for b in textfile.blockers(_good().rstrip("\n"), CARD))


def test_save_refuses_and_leaves_bytes_untouched(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("旧内容\n", encoding="utf-8")
    before = p.read_bytes()
    with pytest.raises(textfile.ContractError):
        textfile.save(str(p), "name: x\nvoice: y\nlooks: z\n---\n", CARD)
    assert p.read_bytes() == before


def test_save_normalizes_then_writes(tmp_path):
    p = tmp_path / "x.md"
    textfile.save(str(p), _good() + "\n\n", CARD)
    assert p.read_text(encoding="utf-8") == _good()


def test_selfcard_no_longer_truncates():
    src = open(os.path.join(ROOT, "loop", "selfcard.py"), encoding="utf-8").read()
    assert "_CARD_MD_CHARS" not in src
    assert "这张卡太长" not in src
    from loop import selfcard
    assert selfcard.CARD.max_bytes == 60000
    assert selfcard.CARD.head_keys == ("name", "voice", "looks")
