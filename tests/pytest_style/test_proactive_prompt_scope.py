"""自发开口的允许名单要与「这一拍能看哪里」一致。

2026-11-22 15:25：她醒来想先 look 一眼「P5」再决定说什么，look 被 read_scope_denied
挡下（这一拍只能看当前会话），只好 rest。提示只写了「可以在这几个地方开口」，
没写「那些地方的内容看不到」，她白试了一步。
"""
from __future__ import annotations

from loop import brain


def test_names_block_states_the_read_limit():
    line = brain._proactive_names_block(["Elaina", "P5"])
    assert "「Elaina」「P5」" in line
    assert "只能看见当前会话" in line
    assert "凭记忆" in line


def test_empty_names_gives_no_block():
    assert brain._proactive_names_block([]) == ""
