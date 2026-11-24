"""读不到标题时重新拉前台再试；读到但对不上立刻中止。

2026-11-22 16:06：主人发来身份证照片，她写好了隐私提醒，但标题硬闸连续三次读到空标题
（窗口被别的窗口盖着），消息被中止。空标题说明「这次没读到」，可以重拉再读；
读到另一个名字说明「真的换了会话」，必须立刻停。
"""
from __future__ import annotations

from core.execution import wx_sender as ws


def test_unreadable_then_readable_reactivates_and_succeeds():
    seq = [(False, ""), (False, ""), (True, "")]
    calls, acts = [], []

    def verify(display, raw_who=""):
        calls.append(display)
        return seq.pop(0)

    ok, seen = ws._verify_title("Elaina", "wxid", verify=verify,
                                reactivate=lambda: acts.append(1), pause=0)
    assert ok is True and seen == ""
    assert len(calls) == 3
    assert len(acts) == 2      # 每次读空之后重拉一次，最后一次成功就不再拉


def test_mismatch_stops_immediately():
    calls, acts = [], []

    def verify(display, raw_who=""):
        calls.append(display)
        return (False, "别人")

    ok, seen = ws._verify_title("Elaina", "wxid", verify=verify,
                                reactivate=lambda: acts.append(1), pause=0)
    assert ok is False and seen == "别人"
    assert len(calls) == 1 and acts == []


def test_all_unreadable_gives_up_after_tries():
    calls = []
    ok, seen = ws._verify_title("Elaina", "wxid", tries=3,
                                verify=lambda d, raw_who="": calls.append(1) or (False, ""),
                                reactivate=lambda: None, pause=0)
    assert ok is False and seen == ""
    assert len(calls) == 3
