"""做不到的事必须说绝（2026-11-03 P3-2 的守卫）。

背景：10-21 那天她被问了 16 次「在群里说一句…」，每次都答「我按不到那个键」，
主人试了 16 次后放弃。做不到不是错，说得像还能再试一次才是错。
"""

from __future__ import annotations

import io
import os

from core.contracts import limits
from core.policy import channel

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_every_channel_carries_the_refusal_rule():
    """任何平台、群聊或私聊，提示词里都必须有这一段。"""
    for platform in ("wechat", "qq", ""):
        for chat_id in ("", "10000000001@chatroom", "wxid_seed02"):
            text = channel.render(channel.rules(platform, chat_id))
            assert limits.REFUSAL_HEAD in text, (platform, chat_id)


def test_refusal_says_it_definitively():
    """2026-11-17 表达改造：不再强制「别等了」这句固定台词。

    语义边界没变——说清能做/不能做、不承诺做不到的结果；被替换掉的只是
    命令式的附加句。旧断言锁的是台词，不是行为。
    """
    text = limits.refusal_text()
    assert "能做和不能做的部分" in text
    assert "不承诺做不到的结果" in text
    assert "别等了" not in text, "固定附加句已按方案移除"


def test_refusal_bans_every_soft_promise():
    """三条最容易让人"再试一次"的说法必须被点名禁掉。"""
    text = limits.refusal_text()
    for banned in ("我再试试", "我马上去办", "按不到那个键"):
        assert banned in text, banned
    assert "不说" in text or "不许" in text


def test_refusal_handles_being_asked_again():
    """追问时的判据是「核对新条件/误解」，不是复读「刚说了」。

    事故防线仍在：不能因为被追问就改口承诺做得到。
    """
    text = limits.refusal_text()
    assert "追问" in text
    assert "新条件" in text and "误解" in text
    assert "刚说了" not in text, "复读模板已按方案移除"


def test_refusal_stays_small():
    """这一段每一轮都在提示词里，不许长成报表。"""
    text = limits.refusal_text()
    assert len(text) <= 400, len(text)
    assert 4 <= len(text.split("\n")) <= 8, len(text.split("\n"))


def test_wording_has_a_single_source():
    """文案只在 limits.py 一处；channel.py 只负责把这一段接上去。"""
    src = io.open(os.path.join(ROOT, "core", "policy", "channel.py"), encoding="utf-8").read()
    assert "limits.refusal_text()" in src
    assert "别等了" not in src
