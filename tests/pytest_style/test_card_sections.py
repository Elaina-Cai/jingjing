"""人设卡分节（施工 Step 6）。

约定：正文里可用 `## 我是谁` / `## 样本` / `## 我不知道` / `## 怎么说` 分节。
没写标题的卡必须**逐字**回到旧行为（整块当「我是谁」）—— 这是向后兼容的底线：
仓库里现有的卡都没分节，改代码不能让她的输入悄悄变样。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loop import brain, selfcard  # noqa: E402

RULES = brain._channel.rules("wechat")


def test_empty_body_has_no_sections() -> None:
    assert selfcard._split_sections("") == {}


def test_plain_card_is_one_section() -> None:
    body = "我是鲸鲸。\n住在电脑里。"
    assert selfcard._split_sections(body) == {"我是谁": body}


def test_four_sections_are_split() -> None:
    body = ("## 我是谁\n鲸鲸。\n## 样本\n「哦。」\n## 我不知道\n外面的事。\n## 怎么说\n一两个句号。")
    got = selfcard._split_sections(body)
    assert list(got) == ["我是谁", "样本", "我不知道", "怎么说"]
    assert got["样本"] == "「哦。」"
    assert got["怎么说"] == "一两个句号。"


def test_unknown_heading_stays_with_the_current_section() -> None:
    body = "## 我是谁\n鲸鲸。\n## 别的标题\n这段不该丢。"
    got = selfcard._split_sections(body)
    assert "这段不该丢。" in got["我是谁"]


def test_card_without_sections_keeps_the_old_bytes() -> None:
    card = "我是鲸鲸。\n住在电脑里。"
    old = brain._system_head(card, {}, v2=False)
    new = brain._system_head(card, selfcard._split_sections(card), v2=True)
    assert new == old


def test_v2_puts_the_speaking_rules_last() -> None:
    parts = {"我是谁": "鲸鲸。", "样本": "「哦。」", "我不知道": "外面的事。", "怎么说": "一两个句号。"}
    head = brain._system_head("鲸鲸。", parts, v2=True)
    assert head.rstrip().endswith("一两个句号。")
    assert head.index("鲸鲸。") < head.index("「哦。」") < head.index("[怎么读图]") < head.index("一两个句号。")


def test_v2_off_ignores_sections() -> None:
    parts = {"我是谁": "鲸鲸。", "样本": "「哦。」", "怎么说": "一两个句号。"}
    head = brain._system_head("鲸鲸。", parts, v2=False)
    assert "「哦。」" not in head and "一两个句号。" not in head
