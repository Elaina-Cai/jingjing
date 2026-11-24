"""表情包不该占掉这一拍唯一的评价名额。

线上非本人消息里 21.8% 是表情包/图片。锚点落在没有描述的媒体上时，原来这一拍
只会给她一条「[表情包]」当可评估的经历 —— 真正带关系含义的那句话没进候选。
没有描述就等于没有话；有描述（[图片: 弹窗显示领取成功]）仍算内容。
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.context import experience_commit as ec


def _proof(content, *, at=0, eid=""):
    return SimpleNamespace(content=content, occurred_at=datetime(2026, 11, 20, 22, 45) - timedelta(minutes=at),
                           event_id=eid or content[:4])


def test_media_without_description_is_not_words():
    assert ec._media_only(_proof("[表情包]"))
    assert ec._media_only(_proof("[图片]"))
    assert not ec._media_only(_proof("[表情包: 睡觉咯]"))
    assert not ec._media_only(_proof("[图片: 弹窗显示领取成功]"))
    assert not ec._media_only(_proof("先领着，暂时用不到"))


def test_sticker_anchor_yields_to_the_nearest_words():
    sticker = _proof("[表情包]", at=0)
    picked = ec._prefer_words([sticker], [_proof("  ", at=1), _proof("好冷", at=3), _proof("收到！", at=9),
                                         _proof("[图片]", at=2)])
    assert [p.content for p in picked] == ["好冷"]


def test_words_anchor_is_untouched():
    anchor = _proof("先领着，暂时用不到", at=0)
    assert ec._prefer_words([anchor], [_proof("好冷", at=3)]) == [anchor]


def test_described_image_anchor_is_kept():
    anchor = _proof("[图片: 弹窗显示领取成功]", at=0)
    assert ec._prefer_words([anchor], [_proof("好冷", at=3)]) == [anchor]


def test_all_media_falls_back_to_the_sticker():
    sticker = _proof("[表情包]", at=0)
    assert ec._prefer_words([sticker], [_proof("[图片]", at=1)]) == [sticker]


def test_offers_uses_the_preference():
    src = open(ec.__file__, encoding="utf-8").read()
    assert "_prefer_words(" in src
    # 2026-11-22：定义一处、_is_words 里一处；其余调用已并进 _prefer_words，
    # 具体行为由本文件上面五条用例钉住，这里只防整个函数被删掉。
    assert src.count("_media_only(") >= 2


def test_hours_old_words_do_not_hitch_a_ride_on_a_new_sticker():
    sticker = _proof("[表情包]", at=0)
    assert ec._prefer_words([sticker], [_proof("十小时前的旧话", at=600)]) == [sticker]


def test_nearby_words_still_fill_in_for_the_sticker():
    sticker = _proof("[表情包]", at=0)
    picked = ec._prefer_words([sticker], [_proof("刚才那句", at=4)])
    assert [p.content for p in picked] == ["刚才那句"]


def test_incomparable_times_keep_the_sticker():
    sticker = _proof("[表情包]", at=0)
    aware = SimpleNamespace(content="时区不同的旧话", occurred_at=datetime(2026, 11, 20, 22, 45, tzinfo=timezone.utc),
                            event_id="tz01")
    assert ec._prefer_words([sticker], [aware]) == [sticker]