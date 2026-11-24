"""WP-STATE 接线锁：块登记 / 装配顺序 / off 零行为 / on 注入 / 发送与提示词接线。

跑法::

    .\\.venv\\Scripts\\python.exe tests\\test_state_assembly_wiring.py
"""

from __future__ import annotations

import ast
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context import assembler  # noqa: E402
from core.context import state_assembly as sa  # noqa: E402
from db.repo_state import ChatState, SelfMove  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FLAG = "YINYUE_FEATURE_STATE_ASSEMBLY"


class _FakeRepo:
    def __init__(self):
        self.moves = []
        self.user_turns = []
        self.mark_resets = []
        self.recent_calls = []
        self.state = ChatState(
            last_user_at=datetime(2026, 10, 11, 12, 0, 0),
            last_bot_at=datetime(2026, 10, 11, 12, 52, 0),
            last_bot_move_kind="joke",
            last_bot_engagement="pending",
        )

    def observe_user_turn(self, **kwargs):
        self.user_turns.append(kwargs)

    def get_chat_state(self, **kwargs):
        return self.state

    def recent_moves(self, **kwargs):
        self.recent_calls.append(kwargs)
        return list(self.moves)

    def mark_reset(self, **kwargs):
        self.mark_resets.append(kwargs)
        self.state.reset_at = kwargs.get("at")

    def record_self_move(self, **kwargs):
        self.moves.append(kwargs)


def _ctx(chat_id="owner@private"):
    return SimpleNamespace(extra={
        "platform": "wechat", "account_id": "bot_a", "chat_id": chat_id,
        "message_time": "2026-10-11T13:30:00",
    })


def test_block_registered():
    entry = [row for row in assembler._BLOCK_POLICY if row[0] == "[当前局面]"]
    assert entry, "块未登记"
    assert entry[0][2] == "context.state_assembly", entry
    print("  ok [当前局面] 已登记进 _BLOCK_POLICY")


def test_assembler_order_and_off(monkeypatch_build):
    original = monkeypatch_build
    try:
        bundle, _ = assembler._build(None, state_block="[当前局面]\n- 测试")
        text = bundle.render()
        assert "[当前局面]" in text
        # 顺序：状态块在 [本轮步骤] 之后（本用例没有 content_plan/focus，只验证存在与 owner）
        names = [block.name for block in bundle.blocks]
        assert "[当前局面]" in names
        entry = [b for b in bundle.blocks if b.name == "[当前局面]"][0]
        assert entry.source == "context.state_assembly"
    finally:
        import core.terminal.persona_brain as pb
        pb.build_persona_blocks = original
    print("  ok 装配顺序/来源")


def test_off_is_zero_behavior():
    repo_calls = []

    def _boom(*args, **kwargs):
        repo_calls.append(1)
        raise AssertionError("off 不应碰仓库")

    old_repo = sa._repo
    sa._repo = _boom
    os.environ.pop(FLAG, None)
    try:
        assert sa.build_state_block(_ctx(), SimpleNamespace(turn=None), is_group=False) == ""
        assert repo_calls == []
    finally:
        sa._repo = old_repo
    print("  ok off：不碰仓库、返回空")


def test_shadow_and_on_injection():
    fake = _FakeRepo()
    fake.moves = [
        SelfMove(sent_at=datetime.now() - timedelta(minutes=30), move_kind="joke",
                 bit_keys=["joke"], engagement="replied"),
        SelfMove(sent_at=datetime.now() - timedelta(hours=1), move_kind="joke",
                 bit_keys=["joke"], engagement="pending"),
        SelfMove(sent_at=datetime.now() - timedelta(hours=2), move_kind="joke",
                 bit_keys=["joke"]),
    ]
    old_repo = sa._repo
    sa._repo = lambda: fake
    try:
        os.environ[FLAG] = "shadow"
        assert sa.build_state_block(_ctx(), SimpleNamespace(turn=None), is_group=False) == ""
        assert fake.user_turns, "shadow 也应记录 user turn"
        os.environ[FLAG] = "on"
        fake.user_turns.clear()
        block = sa.build_state_block(_ctx(), SimpleNamespace(turn=None), is_group=False)
        assert block.startswith("[当前局面]"), block
        assert fake.user_turns, "on 也要记录 user turn"
        # 2026-10-11 主人拍板：作用域放开到「群聊 + 所有私聊」。
        # 原契约「群聊/非主人不注入」已废——这里反向锁住新契约。
        group_block = sa.build_state_block(
            _ctx("group@chatroom"), SimpleNamespace(turn=None), is_group=True)
        assert group_block.startswith("[当前局面]"), group_block
        nonowner_block = sa.build_state_block(
            _ctx("nonowner@private"), SimpleNamespace(turn=None), is_group=False)
        assert nonowner_block.startswith("[当前局面]"), nonowner_block
    finally:
        sa._repo = old_repo
        os.environ.pop(FLAG, None)
    print("  ok shadow 不注入 / on 注入 / 群聊与非主人也注入（2026-10-11 放开）")


def test_record_self_move_gate():
    fake = _FakeRepo()
    old_repo = sa._repo
    sa._repo = lambda: fake
    try:
        os.environ.pop(FLAG, None)
        sa.record_self_move(_ctx(), platform="wechat", account_id="bot_a",
                            chat_id="owner@private", reply="在，怎么了？")
        assert fake.moves == [], "off 不应写台账"
        os.environ[FLAG] = "shadow"
        sa.record_self_move(_ctx(), platform="wechat", account_id="bot_a",
                            chat_id="owner@private", reply="在，怎么了？")
        assert len(fake.moves) == 1
        assert fake.moves[0]["move_kind"] == "question_back"
        assert fake.moves[0]["expects_reply"] == 1
    finally:
        sa._repo = old_repo
        os.environ.pop(FLAG, None)
    print("  ok record_self_move：off 不写 / shadow 写")


def test_reset_mark_called():
    fake = _FakeRepo()
    fake.state.reset_at = datetime(2026, 10, 11, 14, 0, 0)
    old_repo = sa._repo
    sa._repo = lambda: fake
    try:
        os.environ[FLAG] = "shadow"
        ctx = _ctx()
        ctx.extra["user_message"] = "先不管前面的聊天，我说过什么都忘记吧"
        block = sa.build_state_block(ctx, SimpleNamespace(turn=None), is_group=False)
        assert block == ""
        assert len(fake.mark_resets) == 1, "翻篇指令应落边界"
        assert fake.recent_calls, "应查询最近动作"
        assert fake.recent_calls[-1]["since"] >= fake.state.reset_at, "查询起点不早于翻篇边界"
    finally:
        sa._repo = old_repo
        os.environ.pop(FLAG, None)
    print("  ok 翻篇指令落边界 + 查询起点过滤")


def test_source_wiring_locks():
    tp = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    assert tp.index('log.info("router_sent"') < tp.index("state_assembly.record_self_move"), \
        "record_self_move 必须在 router_sent 之后"
    import inspect

    from agents.conversational import ConversationalAgent

    prompt_src = inspect.getsource(ConversationalAgent._build_system_prompt_from_snapshot)
    assert "build_state_block(" in prompt_src, "提示词没调状态块构建"
    assert "state_block=state_block," in prompt_src, "状态块没传进提示词"
    assert "if state_block:" in prompt_src, "空状态块没跳过"
    print("  ok 发送钩子/提示词接线的源码顺序锁")


def _install_fake_persona_builder():
    import core.terminal.persona_brain as pb

    original = pb.build_persona_blocks
    pb.build_persona_blocks = lambda snapshot, **kwargs: ["你是鲸鲸。", "[人格]\n- 测试"]
    return original


def test_usage_fact_wiring():
    import core.state.drivers as drivers

    fake = _FakeRepo()
    old_repo = sa._repo
    original = drivers.budget_pressure
    sa._repo = lambda: fake
    drivers.budget_pressure = lambda now=None: 0.47
    try:
        os.environ[FLAG] = "on"
        block = sa.build_state_block(_ctx(), SimpleNamespace(turn=None), is_group=False)
        assert "- 今天的消耗：约日常参考量的 47%" in block, block
    finally:
        sa._repo = old_repo
        drivers.budget_pressure = original
        os.environ.pop(FLAG, None)
    print("  ok usage 事实接线：build_state_block -> [当前局面]")


def main() -> int:
    original = _install_fake_persona_builder()
    try:
        test_block_registered()
        test_assembler_order_and_off(original)
        test_off_is_zero_behavior()
        test_shadow_and_on_injection()
        test_record_self_move_gate()
        test_reset_mark_called()
        test_usage_fact_wiring()
        test_source_wiring_locks()
    finally:
        import core.terminal.persona_brain as pb
        pb.build_persona_blocks = original
    print("ALL OK: 7 tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
