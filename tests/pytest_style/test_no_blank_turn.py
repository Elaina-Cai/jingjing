"""不许交白卷（2026-11-03 P3-3 的守卫）。

背景：`feedback_samples` 里洗出两类"什么都没说"的错——同一张表情包被发了 4 次、
12 次空回。她不是不想回，是收到"没有文字的消息"时就当没消息。这一节把话说死：
表情包不是沉默的理由；私聊被问到必须回一句；群里没人点名可以不开口。
"""

from __future__ import annotations

import io
import os

from core.contracts import limits
from core.policy import channel

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _src(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_no_blank_rule_exists():
    text = limits.no_blank_text()
    assert text.startswith(limits.NO_BLANK_HEAD)
    for key in ("表情", "拍了拍", "私聊", "群里"):
        assert key in text, key


def test_no_blank_rule_is_decisive():
    """2026-11-17：保留「必须回一句」，删掉「第二遍答刚说了」的复读模板。

    第二遍仍要答，只是答什么由当下信息决定，不再背固定台词。
    """
    text = limits.no_blank_text()
    assert "必须回一句" in text
    assert "可以不开口" in text
    assert "刚说了" not in text, "复读模板已按方案移除"
    assert "别把原话再说一遍" not in text


def test_every_channel_carries_the_no_blank_rule():
    """任何平台、群聊或私聊，提示词里都必须有这一段。"""
    for platform in ("wechat", "qq", ""):
        for chat_id in ("", "10000000001@chatroom", "wxid_seed02"):
            text = channel.render(channel.rules(platform, chat_id))
            assert limits.NO_BLANK_HEAD in text, (platform, chat_id)


def test_no_blank_stays_small():
    """这一段每一轮都在提示词里，不许长成报表。"""
    text = limits.no_blank_text()
    assert len(text) <= 300, len(text)
    assert 3 <= len(text.split("\n")) <= 6, len(text.split("\n"))

def test_blank_private_turn_is_detected_by_the_finder():
    """白卷判定是个真函数：私聊问了没回 = 白卷；群里没人点名不算；说了话不算。"""
    from loop import brain

    assert brain._blank_private_turn("wxid_x", "在吗", []) is True
    assert brain._blank_private_turn("wxid_x", "在吗", [{"act": "say", "result": "blocked"}]) is True
    assert brain._blank_private_turn("wxid_x", "在吗", [{"act": "say", "result": "ok"}]) is False
    assert brain._blank_private_turn("wxid_x", "在吗", [{"act": "sticker", "result": "rehearsal"}]) is False
    assert brain._blank_private_turn("10000000001@chatroom", "在吗", []) is False
    assert brain._blank_private_turn("wxid_x", "", []) is False


def test_same_sticker_is_still_rate_limited():
    """「同一张表情不许连发」是老规矩，在 loop/stickers.py 的冷却里；守卫只要它还在。"""
    from loop import stickers

    assert stickers.COOLDOWN_S > 0
    assert callable(stickers.gate)
