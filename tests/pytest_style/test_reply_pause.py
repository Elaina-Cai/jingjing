"""「这条先别回」的各种说法。

2026-11-22：只认「别回」，「别急着回我」「不用马上回」「先别理我」都会漏。
这些都是明确的等待请求，漏了就会在她该收住的时候开口。
"""
from __future__ import annotations

import pytest

from loop import addressing


@pytest.mark.parametrize("text", [
    "这条先别回",
    "别急着回我",
    "不用马上回",
    "先别理我",
    "你现在别回，我忙完再说",
])
def test_pause_requests_are_recognized(text):
    assert addressing.explicit_reply_pause(text) is True


@pytest.mark.parametrize("text", [
    "别回错了人",          # 是提醒，不是让我停
    "我马上回你",
    "他让我别回这条",
    "这条可以回吗",
    "他现在别回了",        # 在说别人的状态，不是让我停
    "她先别理我",
])
def test_other_sentences_are_not_pause(text):
    assert addressing.explicit_reply_pause(text) is False
