"""群聊里「这句是不是在跟我说」的判据（loop/addressing.py）。

这套判据只认看得见的东西：@、拍、引用、名字、时间上的接话、问句有没有人接。
它不许猜语气、不许拿相似度凑 —— 因为判错的代价是她对着空气说话，或者该答的不答。

用户 2026-11-04 选的是"松档"（B）：
群里不许开新话题；被点名必答；接着她说、没人接的问句可答可不答；其余不出声。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from loop import addressing

NAMES = ("鲸鲸", "小鲸鲸", "大肥鱼")

_BASE = datetime(2026, 11, 4, 12, 0, 0)
_SHIFT = datetime.now() - _BASE - timedelta(hours=1)


def _recent(at):
    """把固定日期平移到最近，时间间距不变（新鲜度规则需要本轮附近的时刻）。"""
    if not isinstance(at, str) or not at.startswith("2026-11-04"):
        return at
    return (datetime.strptime(at, "%Y-%m-%d %H:%M:%S") + _SHIFT).strftime("%Y-%m-%d %H:%M:%S")


class Row:
    """最小行对象：只带判据真正用到的那几个字段。"""

    def __init__(self, text, at, *, is_self=0, speaker="水牛喝水", sender_id="",
                 msg_id="", quote_id="", quote_user="", kind="text"):
        self.raw = text
        self.text = text
        self.at = _recent(at)
        self.is_self = is_self
        self.speaker = speaker
        self.sender_id = sender_id
        self.msg_id = msg_id
        self.quote_id = quote_id
        self.quote_user = quote_user
        self.kind = kind


def _v(rows, **kw):
    if isinstance(kw.get("my_last_at"), str):
        kw["my_last_at"] = _recent(kw["my_last_at"])
    kw.setdefault("names", NAMES)
    return addressing.verdict(rows, **kw)


def test_a_private_chat_message_always_needs_an_answer():
    v = _v([Row("在么", "2026-11-04 12:00:00")], is_private=True)
    assert v.kind == "named"
    assert v.must_answer is True


def test_at_me_is_named():
    v = _v([Row("@鲸鲸 帮我看下这个报错", "2026-11-04 12:00:00")])
    assert v.kind == "named"


def test_at_a_display_name_that_contains_my_name_still_counts():
    # 微信的 @ 后面跟的是显示名，可能带前缀（现场见过 @Elainaの鲸鲸）
    v = _v([Row("@Elainaの鲸鲸 出来冒个泡", "2026-11-04 12:00:00")])
    assert v.kind == "named"


def test_pat_and_quote_are_named():
    assert _v([Row('"鲸鲸" 拍了拍我', "2026-11-04 12:00:00")]).kind == "named"
    assert _v([Row("[引用 鲸鲸] 那你觉得呢", "2026-11-04 12:00:00")]).kind == "named"


def test_a_same_sender_follow_up_becomes_the_current_request():
    rows = [Row("@鲸鲸 把日志清一下", "2026-11-04 12:00:00", sender_id="lan", msg_id="m1"),
            Row("算了，先备份再清", "2026-11-04 12:00:10", sender_id="lan", msg_id="m2")]
    v = _v(rows)
    assert v.kind == "named"
    assert v.must_answer is True
    assert v.n == 1
    assert v.request_text == "算了，先备份再清"
    assert v.request_row_id == "m2"
    assert "备份" in v.sample
    assert "接着" in v.why


def test_a_three_message_safe_sequence_keeps_earlier_rows_as_context():
    rows = [Row("@鲸鲸 第一版在这里", "2026-11-04 12:00:00", sender_id="lan", msg_id="m1"),
            Row("第二版改好了", "2026-11-04 12:00:10", sender_id="lan", msg_id="m2"),
            Row("只看第二版", "2026-11-04 12:00:20", sender_id="lan", msg_id="m3")]
    v = _v(rows)
    assert v.kind == "named"
    assert v.must_answer is True
    assert v.request_row_id == "m3"
    assert v.request_text == "只看第二版"
    assert v.request_context_row_ids == ("m1", "m2")


def test_a_same_display_name_from_another_sender_does_not_change_the_request():
    rows = [Row("@鲸鲸 帮我看看这个", "2026-11-04 12:00:00",
                speaker="水牛喝水", sender_id="lan", msg_id="m1"),
            Row("我也刚看到", "2026-11-04 12:00:10",
                speaker="水牛喝水", sender_id="chi", msg_id="m2")]
    v = _v(rows)
    assert v.kind == "named"
    assert v.n == 1
    assert v.request_row_id == "m1"
    assert v.request_text == "@鲸鲸 帮我看看这个"


def test_an_interjection_stops_the_continuation():
    rows = [Row("@鲸鲸 帮我看下配置", "2026-11-04 12:00:00", sender_id="lan", msg_id="m1"),
            Row("我插一句", "2026-11-04 12:00:05", sender_id="chi", msg_id="m2"),
            Row("其实不用看了", "2026-11-04 12:00:10", sender_id="lan", msg_id="m3")]
    v = _v(rows)
    assert v.kind == "named"
    assert v.request_row_id == "m1"
    assert v.request_text == "@鲸鲸 帮我看下配置"
    assert v.request_context_row_ids == ()


@pytest.mark.parametrize("fields", [{"quote_id": "m99", "quote_user": "chi"}, {"kind": "image"}])
def test_explicit_quote_or_media_does_not_replace_a_request(fields):
    rows = [Row("@鲸鲸 看下这个", "2026-11-04 12:00:00", sender_id="lan", msg_id="m1"),
            Row("你说呢", "2026-11-04 12:00:10", sender_id="lan", msg_id="m2", **fields)]
    v = _v(rows)
    assert v.kind == "named"
    assert v.request_row_id == "m1"
    assert v.request_text == "@鲸鲸 看下这个"


def test_only_the_name_without_at_is_called():
    v = _v([Row("鲸鲸今天怎么这么安静", "2026-11-04 12:00:00")])
    assert v.kind == "called"
    assert v.request_text == "鲸鲸今天怎么这么安静"


@pytest.mark.parametrize("sender_id, expected", [("lan", "following"), ("chi", ""), ("", "")])
def test_following_uses_identity_across_my_last_line(sender_id, expected):
    # 显示名刻意相同。只有同一账号可以沿用之前的接话线索。
    previous = Row("@鲸鲸 flask 装好了吗？", "2026-11-04 11:59:50", sender_id="lan")
    own = Row("我看了一下文档。", "2026-11-04 12:00:00", is_self=1)
    incoming = Row("那怎么部署？", "2026-11-04 12:00:40", sender_id=sender_id)
    assert _v([previous, own, incoming]).kind == expected
    assert _v([incoming], my_last_at=own.at, peer_before_mine=previous).kind == expected
    if expected == "following":
        assert _v([incoming], my_last_at=own.at, peer_before_mine=previous).request_text == "那怎么部署？"


def test_same_person_can_switch_to_speaking_to_someone_else():
    previous = Row("@鲸鲸 帮我看看", "2026-11-04 11:59:50", sender_id="lan")
    incoming = Row("@小池 晚上开黑吗？", "2026-11-04 12:00:40", sender_id="lan")
    assert _v([incoming], my_last_at="2026-11-04 12:00:00", peer_before_mine=previous).kind == ""


def test_someone_else_interjecting_breaks_the_inferred_followup():
    previous = Row("@鲸鲸 帮我看看", "2026-11-04 11:59:50", sender_id="lan")
    interjection = Row("我刚进来。", "2026-11-04 12:00:30", sender_id="chi")
    current = Row("那后来呢？", "2026-11-04 12:00:30", sender_id="lan")
    verdict = _v([interjection, current], my_last_at="2026-11-04 12:00:00", peer_before_mine=previous)
    assert verdict.kind == ""


def test_a_reply_two_hours_later_is_not_following():
    rows = [Row("@鲸鲸 你装了吗", "2026-11-04 11:59:50", sender_id="lan"),
            Row("我查了下 flask 的文档", "2026-11-04 12:00:00", is_self=1),
            Row("在吗", "2026-11-04 14:00:00", sender_id="lan")]
    assert _v(rows).kind == ""


def test_an_unanswered_question_is_questioned():
    v = _v([Row("这个怎么部署？", "2026-11-04 12:00:00")])
    assert v.kind == "questioned"
    assert v.request_text == "这个怎么部署？"


def test_a_question_someone_else_is_also_on_is_not_ours():
    rows = [Row("我也想知道", "2026-11-04 11:59:00"),
            Row("这个怎么部署？", "2026-11-04 12:00:00")]
    assert _v(rows).kind == ""


def test_after_i_answer_nothing_is_aimed_at_me():
    rows = [Row("@鲸鲸 你看这个", "2026-11-04 12:00:00"),
            Row("看过了，是依赖没装", "2026-11-04 12:00:10", is_self=1)]
    assert _v(rows).kind == ""


def test_system_messages_are_not_someone_talking():
    rows = [Row('<?xml version="1.0"?><sysmsg type="revokemsg"><revoke>', "2026-11-04 12:00:00")]
    assert _v(rows).kind == ""


def test_an_empty_name_list_never_lights_up():
    # 名单是手填的（data/persona/me_names.json）；空了就宁可什么都不认
    assert _v([Row("@鲸鲸 在吗", "2026-11-04 12:00:00")], names=()).kind == ""
    assert _v([Row("鲸鲸在吗", "2026-11-04 12:00:00")], names=()).kind == ""


def test_no_rows_at_all():
    assert _v([]).kind == ""
    assert _v([], is_private=True).kind == ""


def test_short_text_says_whether_an_answer_is_required():
    must = _v([Row("@鲸鲸 你看", "2026-11-04 12:00:00")])
    may = _v([Row("我觉得还行", "2026-11-04 12:00:00"), Row("真的", "2026-11-04 12:00:30", is_self=1)])
    assert "必答" in must.short()
    assert must.must_answer is True
    assert may.short() == "" or "可答可不答" in may.short()


def test_the_sample_is_the_line_that_decided_it():
    rows = [Row("有人吗", "2026-11-04 11:50:00"),
            Row("@鲸鲸 帮我看看", "2026-11-04 12:00:00")]
    v = _v(rows)
    assert v.kind == "named"
    assert "帮我看看" in v.sample
