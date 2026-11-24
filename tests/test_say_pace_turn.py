# -*- coding: utf-8 -*-
"""同一回合里的多气泡，不许被「跨回合冷却」误伤（2026-11-02）。

病：`brain.same_target`（默认 25 秒）本来是**跨回合**的「刚跟这个人说完，等对方先回
一句」，防的是两拍之间贴脸连发。可它不分回合，一回合内她想发第二条气泡，1.5 秒后就被
判「刚跟这个人说过话，等对方先回一句」——而 1.5 秒里没人回得了话。提示词又明确允许她
「想发几条就几条」，于是「说得出来、发不出去」。2026-11-02 账本 3 次实测都是这个。

钉五件事：
1. 同一回合 → 放行（不够最小间隔就等，不拦）；
2. 跨回合 → 仍按 25 秒拦（修 bug 不许把这条一起放开）；
3. 旋钮归零 → 完全不拦；
4. `said_this_turn` 真按 turn_id 分，不是按时间猜；
5. 提示词里那句「停 1.5 秒」和闸用的数必须一致（两处各写一个数迟早对不上）。
"""
from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _tempdirs import make_db_dir  # noqa: E402

from core.infra import actions, tempo  # noqa: E402
from core.policy import channel  # noqa: E402
from loop import brain  # noqa: E402


def test_same_turn_bubble_passes() -> None:
    # 同一回合、上一句 0.3 秒前：不该拦，只补一个最小间隔
    verdict, wait_ms = brain.say_pace_verdict(300, True)
    assert verdict == "wait", (verdict, wait_ms)
    assert 0 < wait_ms <= brain.BUBBLE_GAP_MS, wait_ms
    # 同一回合、间距已够：直接放行
    assert brain.say_pace_verdict(brain.BUBBLE_GAP_MS + 100, True) == ("ok", 0)
    # 同一回合、甚至比冷却还久：当然放行
    assert brain.say_pace_verdict(60_000, True) == ("ok", 0)


def test_cross_turn_still_blocked() -> None:
    if brain.SAME_TARGET_COOLDOWN_MS <= 0:      # 旋钮被关掉时这条不适用
        assert brain.say_pace_verdict(1_000, False) == ("ok", 0)
        return
    verdict, wait_ms = brain.say_pace_verdict(1_000, False)
    assert verdict == "blocked", (verdict, wait_ms)
    assert wait_ms == brain.SAME_TARGET_COOLDOWN_MS - 1_000, wait_ms
    # 冷却过了就放行
    assert brain.say_pace_verdict(brain.SAME_TARGET_COOLDOWN_MS + 1, False) == ("ok", 0)


def test_never_said_anything() -> None:
    assert brain.say_pace_verdict(None, False) == ("ok", 0)
    assert brain.say_pace_verdict(None, True) == ("ok", 0)


def test_zero_cooldown_disables_gate() -> None:
    old = brain.SAME_TARGET_COOLDOWN_MS
    try:
        brain.SAME_TARGET_COOLDOWN_MS = 0
        assert brain.say_pace_verdict(1_000, False) == ("ok", 0)
    finally:
        brain.SAME_TARGET_COOLDOWN_MS = old


def test_said_this_turn_reads_ledger() -> None:
    """账本里最后一条 sent 的 turn_id 才作数 —— 不是「最近 25 秒内说过就算同一回合」。"""
    old_dir = actions.DIR
    tmp = make_db_dir("saypace_")
    try:
        actions.DIR = tmp
        chat = "10000000002@chatroom"
        assert actions.said_this_turn(chat, "t1") is False     # 还没说过
        actions.append_receipt(act="say", target=chat, result="sent",
                               turn_id="t1", text="第一句")
        assert actions.said_this_turn(chat, "t1") is True
        assert actions.said_this_turn(chat, "t2") is False     # 换了回合就是跨回合
        assert actions.said_this_turn("另一个会话", "t1") is False   # 换了人也不算
        # 被拦的那条不算「说过」——否则一次拦截会把后面全放开
        actions.append_receipt(act="say", target=chat, result="blocked",
                               turn_id="t2", blocked_reason="test")
        assert actions.said_this_turn(chat, "t2") is False
        # 新回合真的发了，就跟着换
        actions.append_receipt(act="say", target=chat, result="sent",
                               turn_id="t2", text="第二回合")
        assert actions.said_this_turn(chat, "t2") is True
        assert actions.said_this_turn(chat, "t1") is False
    finally:
        actions.DIR = old_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_prompt_advice_matches_gate() -> None:
    """提示词让「停 %.1f 秒」，闸就按这个数兜 —— 两处对不上她就成了说一套做一套。"""
    assert brain.BUBBLE_GAP_MS == tempo.ms("brain.bubble_gap"), brain.BUBBLE_GAP_MS
    # 群里那条 1.5 秒是 _GROUP_RULES 给的（不传 chat_id 就只看私聊默认 0）
    grp = channel.rules("wechat", "10000000002@chatroom")
    assert grp.delay_ms == brain.BUBBLE_GAP_MS, (grp.delay_ms, brain.BUBBLE_GAP_MS)
    assert "停 %.1f 秒" % (brain.BUBBLE_GAP_MS / 1000.0) in channel.render(grp)


def main() -> bool:
    tests = [
        test_same_turn_bubble_passes,
        test_cross_turn_still_blocked,
        test_never_said_anything,
        test_zero_cooldown_disables_gate,
        test_said_this_turn_reads_ledger,
        test_prompt_advice_matches_gate,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"say pace checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
