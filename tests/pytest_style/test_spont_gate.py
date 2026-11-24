"""自发开口的闸门（E-51）。

2026-11-04 18:46 她在作业群自问自答三遍：那一拍是自发醒，群里 57 分钟没人说话，
`spont_streak` 已经 12 连。
"""

from loop import spont


def test_too_many_silent_spontaneous_turns_block_the_next_one():
    ok, why = spont.gate(streak=spont.SPONT_STREAK_MAX, last_heard=1.0, now=100.0,
                         hot_chats={"10000000001@chatroom"})
    assert ok is False
    assert why.startswith("streak=")


def test_one_short_of_the_cap_is_still_allowed():
    ok, why = spont.gate(streak=spont.SPONT_STREAK_MAX - 1, last_heard=1.0, now=100.0,
                         hot_chats={"10000000001@chatroom"})
    assert (ok, why) == (True, "")


def test_nobody_spoke_recently_means_no_spontaneous_turn():
    ok, why = spont.gate(streak=0, last_heard=1.0, now=100.0, hot_chats=set())
    assert ok is False
    assert why.startswith("quiet>")


def test_empty_or_garbage_hot_chats_are_treated_as_quiet():
    for hot in (None, [], [""], [None]):
        ok, why = spont.gate(streak=0, last_heard=1.0, now=100.0, hot_chats=hot)
        assert ok is False, hot


def test_a_last_heard_in_the_future_does_not_count_as_activity():
    ok, why = spont.gate(streak=0, last_heard=500.0, now=100.0, hot_chats={"c1"})
    assert ok is False
    assert why == "clock"


def test_someone_spoke_and_the_cap_is_not_reached_lets_her_speak():
    ok, why = spont.gate(streak=2, last_heard=99.0, now=100.0, hot_chats={"c1"})
    assert (ok, why) == (True, "")


def test_the_caps_are_positive_and_configurable():
    assert spont.SPONT_STREAK_MAX >= 1
    assert spont.GROUP_QUIET_S > 0
