"""WP-STATE 状态装配 v1 纯逻辑用例（不碰 DB、不读 flag）。

跑法::

    .\\.venv\\Scripts\\python.exe tests\\test_state_assembly.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context import state_assembly as sa  # noqa: E402
from db.repo_state import ChatState, SelfMove  # noqa: E402


def test_classify_move_table():
    cases = [
        ("你还欠我一个笑话没还呢", "", "debt"),
        ("第三遍了哦", "", "counting"),
        ("听好，从前有只狼", "", "joke"),
        ("好，讲个笑话", "", "joke"),
        ("《不写》\n你让我写诗", "", "poem"),
        ("会，给我一分钟", "你会不会写诗", "poem"),
        ("我在，陪你一会儿", "", "comfort"),
        ("明天多云 25°C，带件外套", "", "weather"),
        ("那当然，也不看看我是谁", "", "self_praise"),
        ("在，怎么了？", "", "question_back"),
        ("哦", "", "other"),
    ]
    for reply, inbound, expected in cases:
        kind, keys, expects = sa.classify_move(reply, inbound)
        assert kind == expected, f"{reply!r} -> {kind} != {expected}"
        assert keys, f"{reply!r} bit_keys 为空"
    # 优先级与多命中
    kind, keys, expects = sa.classify_move("你还欠我一个笑话呢，行吧？")
    assert kind == "debt", kind
    assert keys == ["debt", "joke", "question_back"], keys
    assert expects == 1
    # 纯问句
    kind, keys, expects = sa.classify_move("什么事把你掏空了？")
    assert kind == "question_back" and expects == 1
    print("  ok classify_move：11 例 + 优先级/多命中/问句")


def test_age_and_engagement():
    now = datetime(2026, 10, 11, 13, 30, 0)
    state = ChatState(
        last_bot_at=now - timedelta(minutes=5),
        last_bot_move_kind="joke",
        last_bot_engagement="pending",
    )
    assert "等你回" in sa._engagement_text(state, now)
    state.last_bot_at = now - timedelta(hours=1)
    assert "还没回" in sa._engagement_text(state, now)
    state.last_bot_at = now - timedelta(hours=10)
    assert "大概率没接" in sa._engagement_text(state, now)
    state.last_bot_engagement = "replied"
    state.last_bot_at = now - timedelta(minutes=5)
    assert "已回" in sa._engagement_text(state, now)
    assert sa._age_text(timedelta(seconds=20)) == "刚刚"
    assert sa._age_text(timedelta(minutes=7)) == "7 分钟"
    assert sa._age_text(timedelta(hours=3)) == "3 小时"
    assert sa._age_text(timedelta(days=2)) == "2 天"
    print("  ok 反馈/年龄文本")


def test_render_block_lines_and_cap():
    now = datetime(2026, 10, 11, 13, 30, 0)
    state = ChatState(
        last_user_at=now - timedelta(hours=10),
        last_bot_at=now - timedelta(minutes=37),
        last_bot_move_kind="joke",
        last_bot_engagement="pending",
    )
    moves = [
        SelfMove(sent_at=now - timedelta(minutes=37), move_kind="joke",
                 bit_keys=["joke", "question_back"], engagement="replied"),
        SelfMove(sent_at=now - timedelta(hours=3), move_kind="joke", bit_keys=["joke"]),
        SelfMove(sent_at=now - timedelta(hours=4), move_kind="joke", bit_keys=["joke"]),
        SelfMove(sent_at=now - timedelta(hours=1), move_kind="counting",
                 bit_keys=["counting", "joke"]),
        SelfMove(sent_at=now - timedelta(hours=2), move_kind="self_praise",
                 bit_keys=["self_praise"]),
    ]
    block = sa.render_state_block(state, moves, now)
    assert block.startswith("[当前局面]"), block
    assert "我上一条：讲笑话" in block
    assert "还没回" in block
    assert "最近 24 小时我用过：" in block
    assert "数次数×1（冷却中）" in block
    assert "需要留意：" in block
    assert len(block) <= sa.MAX_CHARS
    # 空状态 → 空块
    assert sa.render_state_block(None, [], now) == ""
    # 超限整行丢：造很多 kind
    many = []
    for i, kind in enumerate(("joke", "poem", "comfort", "weather", "self_praise", "other")):
        many.append(SelfMove(sent_at=now - timedelta(minutes=i + 1), move_kind=kind,
                             bit_keys=[kind], engagement="replied"))
    long_block = sa.render_state_block(state, many, now)
    assert len(long_block) <= sa.MAX_CHARS
    print("  ok 状态块渲染：行结构 / 冷却 / 疲劳 / 400 字上限")


def test_reset_request_and_boundary():
    assert sa.is_reset_request("先不管前面的聊天，我说过什么都忘记吧")
    assert sa.is_reset_request("忘记前面聊的")
    assert sa.is_reset_request("翻篇")
    assert not sa.is_reset_request("继续讲刚才那个笑话")
    assert not sa.is_reset_request("我把这事忘了")

    now = datetime(2026, 10, 11, 15, 0, 0)
    state = ChatState(
        last_user_at=now - timedelta(minutes=1),
        last_bot_at=now - timedelta(minutes=10),
        last_bot_move_kind="counting",
        last_bot_engagement="pending",
        reset_at=now - timedelta(minutes=5),
    )
    block = sa.render_state_block(state, [], now)
    assert "我上一条" not in block, "翻篇边界之前的上一条不该展示"
    assert "本轮" in block
    print("  ok 翻篇指令识别 + 边界前记录不展示")


def test_usage_fact_line():
    now = datetime(2026, 10, 11, 13, 30, 0)
    state = ChatState(last_user_at=now - timedelta(minutes=1))
    assert "今天的消耗" not in sa.render_state_block(state, [], now)
    assert sa.situation_facts() == []
    block = sa.render_state_block(state, [], now, usage_pct=47.0)
    assert "- 今天的消耗：约日常参考量的 47%" in block, block
    assert "约日常的" not in block, "措辞必须带参考量，避免含糊"
    for bad in ("很多", "注意", "省着", "请"):
        assert bad not in block, (bad, block)
    print("  ok 处境事实行：只在有真量时出现；纯数字，无评价/指令")


def test_usage_pct_from_driver():
    import core.state.drivers as drivers
    original = drivers.budget_pressure

    def _at(value):
        return lambda now=None: value

    try:
        drivers.budget_pressure = _at(0.47)
        assert abs(sa._usage_pct() - 47.0) < 1e-9
        drivers.budget_pressure = _at(0.009)
        assert sa._usage_pct() is None, "<1% 的噪点不注入"
        drivers.budget_pressure = _at(1.0)
        assert sa._usage_pct() == 100.0

        def _boom(now=None):
            raise RuntimeError("boom")

        drivers.budget_pressure = _boom
        assert sa._usage_pct() is None, "状态读不到 = 这一条不出现"
    finally:
        drivers.budget_pressure = original
    print("  ok _usage_pct：真量换算 / <1% 不出现 / 异常降级")


def main() -> int:
    tests = [test_classify_move_table, test_age_and_engagement,
             test_render_block_lines_and_cap, test_usage_fact_line,
             test_usage_pct_from_driver, test_reset_request_and_boundary]
    for fn in tests:
        fn()
    print("ALL OK:", len(tests), "tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
