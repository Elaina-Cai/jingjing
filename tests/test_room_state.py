"""批2b RoomState 测试：谁在跟谁说话（结构事实，纯函数）。

Run directly:
    python tests/test_room_state.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.room_state import build_room_situation  # noqa: E402

# 兄弟开关钉死（守卫 tests/test_env_flag_isolation.py §③）：本文件的断言都是
# [局况] / [姿态与分寸] 的**文本逐字**，姿态三兄弟必须一起表态（需要打开的用例
# 自己在那一条里显式设置并还原）；不钉的话它们会跟着生产 .env 走进来改文本。

BASE = datetime(2026, 9, 23, 13, 31, 0)


def _m(speaker: str, content: str, *, seconds: int = 0, self_: bool = False,
       mention: bool = False) -> dict:
    return {
        "sender_name": speaker,
        "content": content,
        "created_at": BASE + timedelta(seconds=seconds),
        "is_self": self_,
        "mention_self": mention,
    }


def test_others_qa_case() -> None:
    """13:32 真实案例：Elaina 问 → P7答，她不该被当成参与者。"""
    messages = [
        _m("Elaina", "[引用 P7: 好难选人]\n啥选人", seconds=29),
        _m("Elaina", "选班干部吗", seconds=36),
    ]
    current = _m("P7", "青协招新", seconds=57)
    situation = build_room_situation(messages, current=current)
    assert situation.addressed_to_bot is False
    assert situation.latest_is_answer is True
    assert situation.latest_asker == "Elaina"
    assert situation.turns and situation.turns[0].kind == "qa"
    assert situation.turns[0].answerer == "P7"
    text = situation.render()
    assert "最近对话" in text
    assert "在对 Elaina 说话，没有提到你" in text, text
    print("  OK  别人互答：识别为 Elaina 问 / P7答，未提到她")


def test_mention_bot() -> None:
    messages = [_m("P7", "青协招新", seconds=57)]
    current = _m("P8", "@鲸鲸 在吗", seconds=80, mention=True)
    situation = build_room_situation(messages, current=current)
    assert situation.addressed_to_bot is True
    text = situation.render()
    assert "点到了你" in text, text
    print("  OK  @她：标记为点到她")


def test_quote_bot() -> None:
    messages = [_m("P5", "在吗", seconds=10)]
    current = _m("P5", "[引用 鲸鲸: 记死了]\n你还记得吗", seconds=20)
    situation = build_room_situation(messages, current=current)
    assert situation.addressed_to_bot is True
    print("  OK  引用她：标记为点到她")


def test_quote_other_is_not_her() -> None:
    current = _m("P4", "[引用 P5: 跑死了]\n潘神每天起床", seconds=30)
    situation = build_room_situation([_m("P5", "跑死了", seconds=20)], current=current)
    assert situation.addressed_to_bot is False
    assert situation.turns and situation.turns[0].kind == "quote"
    text = situation.render()
    assert "引用 P5" in text, text
    print("  OK  引用别人：不算点到她")


def test_pending_and_self() -> None:
    messages = [
        _m("P6", "下午可以帮我签个到吗", seconds=1),
        _m("鲸鲸", "这猫是被谁揍的，鼻青脸肿还咧嘴笑", seconds=5, self_=True),
    ]
    situation = build_room_situation(
        messages, pending=["下午可以帮我签个到吗"], now=BASE + timedelta(seconds=40)
    )
    text = situation.render()
    assert "有人还在等回应" in text, text
    assert "你刚说过" in text, text
    assert situation.last_self.startswith("这猫")
    print("  OK  悬置与她刚说过的话都进局况")


def test_speakers_window() -> None:
    messages = [
        _m("P5", "a", seconds=0),
        _m("P4", "b", seconds=10),
        _m("P5", "c", seconds=20),
    ]
    situation = build_room_situation(messages, now=BASE + timedelta(seconds=30))
    assert situation.speakers == ("P5", "P4"), situation.speakers
    print("  OK  在场按最近发言排序：", situation.speakers)


def test_render_is_bounded() -> None:
    messages = [
        _m("Elaina", "这是一个非常长的问题" * 8, seconds=1),
        _m("P7", "这是一个非常长的回答" * 8, seconds=2),
    ]
    text = build_room_situation(messages).render(limit=120)
    assert len(text) <= 120
    print("  OK  render 有长度上限")


def test_short_buffer_scans_first_row() -> None:
    """行数少于 SCAN_LIMIT 时，第 0 条也必须被扫到（批2c 回放发现的越界）。

    历史 bug：``range(len-1, max(0, len-SCAN_LIMIT), -1)`` 在 len < SCAN_LIMIT
    时下界退化成 0，range 不含 0，第 0 条永远扫不到 ——「问」恰在首条时整轮问答
    识别不出来。
    """
    messages = [_m("Elaina", "选班干部吗", seconds=36)]
    current = _m("P7", "青协招新", seconds=57)
    situation = build_room_situation(messages, current=current)
    assert situation.turns, "首条为提问时也必须成轮"
    assert situation.latest_is_answer is True
    assert situation.latest_asker == "Elaina"
    print("  OK  短缓冲下首条提问仍能成轮")


# ------------------------------------------------- 批2b 双轨收口（窗口输入）
def _w(speaker: str, content: str, *, seconds: int = 0, self_: bool = False):
    """A ``chat_window.WindowMessage`` — the format the window actually yields."""
    from core.context.chat_window import WindowMessage

    return WindowMessage(
        occurred_at=BASE + timedelta(seconds=seconds),
        speaker=speaker,
        content=content,
        is_self=self_,
    )


def test_window_message_is_first_class_input() -> None:
    """WindowMessage 现在是一等输入：不再报错、说话人/时间都能读出来。"""
    messages = [_w("Elaina", "选班干部吗", seconds=36)]
    situation = build_room_situation(messages, current=_w("P7", "青协招新", seconds=57))
    assert situation.latest_speaker == "P7", situation.latest_speaker
    assert situation.speakers, "窗口格式也要能算出在场"
    assert "Elaina" in situation.speakers
    print("  OK  窗口消息可作为一等输入（speaker/occurred_at 都能读）")


def test_window_quote_attributed_from_marker() -> None:
    """窗口行的单行引用标记 ``[引用 X: …]`` 能拆回作者。"""
    from core.context.room_state import _as_room_row

    row = _as_room_row(_w("P4", "[引用 P5: 跑死了] 潘神每天起床", seconds=30))
    assert row["quoted_author"] == "P5", row
    assert row["sender_name"] == "P4"
    assert row["content"] == "潘神每天起床"
    situation = build_room_situation([row])
    assert situation.turns and situation.turns[0].kind == "quote"
    assert situation.turns[0].asker == "P5"
    print("  OK  窗口引用行拆回作者（[引用 X: …] → asker=X）")


def test_window_quote_without_author_is_skipped() -> None:
    """认不出作者的窗口引用行：跳过，不猜（宁可少说）。"""
    from core.context.room_state import _as_room_row

    row = _as_room_row(_w("P4", "[引用: 跑死了] 潘神每天起床", seconds=30))
    assert row["quote_attributable"] is False, row
    situation = build_room_situation([row])
    assert not any(t.kind == "quote" for t in situation.turns), situation.turns
    print("  OK  窗口引用行认不出作者时不猜（跳过该轮）")


def test_window_sourced_gate_matches_buffer_gate() -> None:
    """同一批消息，窗口来源与缓冲来源对 2c 给出同一结论（收口的意义）。"""
    from core.context.room_state import _as_room_row, build_room_situation

    buffer_rows = [
        _m("Elaina", "选班干部吗", seconds=36),
        _m("P7", "青协招新", seconds=57),
    ]
    window_rows = [
        _as_room_row(_w("Elaina", "选班干部吗", seconds=36)),
        _as_room_row(_w("P7", "青协招新", seconds=57)),
    ]
    from_buffer = build_room_situation(buffer_rows)
    from_window = build_room_situation(window_rows)
    # R0：看戏门退役，两路来源的收口改为锁「同一份局面事实」
    for field in ("latest_asker", "latest_is_answer", "latest_kind"):
        assert getattr(from_window, field) == getattr(from_buffer, field), field
    assert from_window.latest_asker == from_buffer.latest_asker == "Elaina"
    print("  OK  窗口来源与缓冲来源给出同一份局面事实（双轨收口）")


def test_window_situation_returns_none_when_flag_off() -> None:
    """窗口开关关 → 返回 None，让调用方回落 buffer（不是放行）。"""
    from core.context.room_state import build_window_room_situation

    saved = os.environ.pop("YINYUE_FEATURE_CHAT_WINDOW", None)
    try:
        result = build_window_room_situation("10000000001@chatroom")
        assert result is None, result
    finally:
        if saved is not None:
            os.environ["YINYUE_FEATURE_CHAT_WINDOW"] = saved
    print("  OK  窗口关时返回 None（回落语义，不是放行）")


def test_assembler_block_policy_registered() -> None:
    """[局况] 必须登记进 assembler 策略表（否则预算分类/trace 来源会错）。"""
    from core.context.assembler import _BLOCK_POLICY

    rows = {policy[0]: policy for policy in _BLOCK_POLICY}
    assert "[局况]" in rows, "missing [局况] block policy"
    assert rows["[局况]"][2] == "context.room_state"
    print("  OK  [局况] 已登记 assembler 策略表（预算/trace 来源正确）")


def test_window_loader_injection() -> None:
    """loader 注入必须生效：回放/复现历史时点靠它喂数（此前是死参数）。"""
    from core.context.chat_window import WindowMessage
    from core.context.room_state import build_window_room_situation

    prev = {
        key: os.environ.get(key)
        for key in ("YINYUE_FEATURE_CHAT_WINDOW", "YINYUE_CHAT_WINDOW_CHATS")
    }
    os.environ["YINYUE_FEATURE_CHAT_WINDOW"] = "1"
    os.environ["YINYUE_CHAT_WINDOW_CHATS"] = "c@chatroom"
    try:
        rows = [
            WindowMessage(
                occurred_at=BASE - timedelta(seconds=40),
                speaker="Elaina",
                content="选班干部吗",
            ),
            WindowMessage(
                occurred_at=BASE - timedelta(seconds=20),
                speaker="P7",
                content="青协招新",
            ),
        ]
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return rows

        situation = build_window_room_situation("c@chatroom", now=BASE, loader=loader)
        assert calls["n"] == 1, "loader 未被调用（死参数回归）"
        assert situation is not None
        text = situation.render()
        assert "青协招新" in text, text
    finally:
        for key, value in prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("  OK  build_window_room_situation 的 loader 注入生效")


def test_window_quote_to_bot_is_addressed() -> None:
    """窗口路径：引用她的消息必须标记 addressed。

    2b 收口后窗口行经 _as_room_row 把「[引用 鲸鲸: …]」前缀剥进 quoted_author，
    只查 text 的 _quotes_bot 会漏判，[局况] 会说成「没有提到你」（自相矛盾）。
    """
    from core.context.chat_window import WindowMessage
    from core.context.room_state import _as_room_row, build_room_situation

    rows = [
        WindowMessage(
            occurred_at=BASE - timedelta(seconds=90),
            speaker="鲸鲸",
            content="@P3 破坏啥友谊啊",
            is_self=True,
        ),
        WindowMessage(
            occurred_at=BASE - timedelta(seconds=30),
            speaker="P3",
            content="[引用 鲸鲸: @P3 破坏啥友谊啊] 你潘诣化了？",
            is_self=False,
        ),
    ]
    normalized = [_as_room_row(r, now=BASE) for r in rows]
    situation = build_room_situation(normalized, now=BASE)
    assert situation.addressed_to_bot is True, situation.render()
    text = situation.render()
    assert "点到了你" in text, text
    print("  OK  窗口路径的「引用她」被标记为点到了你")




def test_self_latest_omits_stale_line() -> None:
    """她自己是最新一条时，不该输出「最新消息（鲸鲸）没有提到你」这种自相矛盾行。"""
    rows = [_m("P5", "在吗", seconds=1), _m("鲸鲸", "在的", seconds=2, self_=True)]
    situation = build_room_situation(rows, now=BASE + timedelta(seconds=3))
    assert "最新消息（鲸鲸）" not in situation.render(), situation.render()
    print("  OK  自己是最新消息时不输出「没提到你」")
















def test_wp10_row5_additions_are_retired() -> None:
    """WP-10 第 5 行：位置/姿态/气氛/话题线/折叠 echo 已退役，基础事实行保留。"""
    rows = [
        _m("Elaina", "在吗"),
        _m("鲸鲸", "在的", self_=True),
        _m("Elaina", "@鲸鲸 帮我看看"),
    ]
    situation = build_room_situation(rows, now=BASE + timedelta(seconds=5))
    text = situation.render()
    for attr in ("position", "stance", "self_echo", "atmosphere", "relay", "topic_line"):
        assert not hasattr(situation, attr), attr
    for marker in ("你的位置", "你的姿态", "你的立场", "气氛：", "话题线", "你最近说过"):
        assert marker not in text, (marker, text)
    assert "你刚说过" in text or "在场" in text, text
    print("  OK  WP-10 第 5 行加法项已退役（基础 [局况] 事实行保留）")


def main() -> int:
    tests = [
        test_wp10_row5_additions_are_retired,
        test_others_qa_case,
        test_mention_bot,
        test_quote_bot,
        test_quote_other_is_not_her,
        test_pending_and_self,
        test_speakers_window,
        test_render_is_bounded,
        test_short_buffer_scans_first_row,
        test_window_message_is_first_class_input,
        test_window_quote_attributed_from_marker,
        test_window_quote_without_author_is_skipped,
        test_window_sourced_gate_matches_buffer_gate,
        test_window_situation_returns_none_when_flag_off,
        test_window_loader_injection,
        test_window_quote_to_bot_is_addressed,
        test_self_latest_omits_stale_line,
        test_assembler_block_policy_registered,
    ]
    for test in tests:
        test()
    print(f"ALL PASSED ({len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
