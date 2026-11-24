"""输入框残留计数（loop/residue.py）的行为。

2026-11-04：主人在旁边看到她在三个会话之间来回切 40 秒才发出去。真因是
desk 单线程串行 + 输入框残句清不掉，两边一起打转。这里锁住"数到第几次停下"。
"""
from __future__ import annotations

from loop import residue


def test_the_first_two_failures_are_only_counted():
    residue.reset(91)
    why, n, dead = residue.note_strike(91, "输入框里已经有字")
    assert (n, dead) == (1, False)
    assert why == "输入框里已经有字"
    why, n, dead = residue.note_strike(91, "输入框里已经有字")
    assert (n, dead) == (2, False)
    assert not residue.should_terminate(91)


def test_the_third_failure_stops_the_row():
    residue.reset(92)
    residue.note_strike(92, "输入框里已经有字")
    residue.note_strike(92, "输入框里已经有字")
    why, n, dead = residue.note_strike(92, "输入框里已经有字")
    assert dead is True
    assert n == residue.RESIDUE_STRIKE_LIMIT == 3
    assert "连续 3 次清不掉输入框" in why
    assert residue.should_terminate(92) is True


def test_the_stop_flag_is_taken_only_once():
    residue.reset(93)
    for _ in range(3):
        residue.note_strike(93, "输入框里已经有字")
    assert residue.take_terminal(93) is True
    assert residue.take_terminal(93) is False
    assert residue.should_terminate(93) is False


def test_a_delivered_row_starts_counting_again():
    residue.reset(94)
    residue.note_strike(94, "x")
    residue.note_strike(94, "x")
    residue.reset(94)
    _, n, dead = residue.note_strike(94, "x")
    assert (n, dead) == (1, False)


def test_two_rows_do_not_share_a_counter():
    residue.reset(95)
    residue.reset(96)
    residue.note_strike(95, "x")
    residue.note_strike(95, "x")
    _, n, dead = residue.note_strike(96, "x")
    assert (n, dead) == (1, False)
    assert residue.should_terminate(95) is False


def test_the_marker_matches_what_the_sender_says():
    # desk 按这句认"这是残留失败"；发送层改文案就得一起改，否则计数形同虚设。
    assert residue.RESIDUE_MARKER == "输入框里已经有字"
