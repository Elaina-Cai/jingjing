"""「只想被倾听」的识别：别查 / 别给建议 / 别急着安慰，配一句「听我说」。

2026-11-22：线上只认「先别查」，于是「别给我建议，听我说」会被当成普通求助，
她直接开始给方案。这里锁住扩展后的判据，也锁住不该误伤的反例。
"""
from __future__ import annotations

import pytest

from loop import addressing


@pytest.mark.parametrize("text", [
    "@鲸鲸 先别查，听我说",
    "别给我建议，先听我说就好",
    "你先别急着安慰我，听我说",
    "听我说完，别给建议",
    "别分析，听我说两句",
])
def test_listening_requests_are_recognized(text):
    assert addressing.explicit_listening_request(text) is True


@pytest.mark.parametrize("text", [
    "别给建议，直接告诉我怎么办",   # 第二句是在要方案，不是要倾听
    "先查一下资料",
    "别查了，直接说结论",           # 同样是在要结论
    "他跟我说「先别查，听我说」",     # 转述别人的话，不是对我说的
    "如果是我的话会先别查",          # 假设句
])
def test_other_requests_stay_normal(text):
    assert addressing.explicit_listening_request(text) is False
