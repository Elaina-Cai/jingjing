# -*- coding: utf-8 -*-
"""P1（2026-10-13）：当前消息块带说话人 —— 只在读场作用域内，其余逐字节旧形。

2026-10-23 收尾：本文件原先写成 pytest 风格（`def test_xxx(monkeypatch)`），
但本仓 tests/ 是**独立可执行脚本**套件、pytest 被刻意配置为不收集任何用例
（见 pyproject.toml [tool.pytest.ini_options] 与 requirements-dev.txt 顶部注释），
所以 `python tests/test_current_message_speaker.py` 会退出 0 而**一条用例都没跑**。
现在改成仓库通用形状：`check()` + `main()`，并顺手把最后那条
「conversational.py 里 current_message_line( 出现 3 次」的文本锁，
换成**真跑三个 prompt 路径**、看交给模型的 prompt 里到底有没有说话人。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# "env 未设" ≠ "关"：本文件碰到的开关在 import 期显式钉死（env 隔离纪律③）。
os.environ.setdefault("YINYUE_FEATURE_GROUP_SCENE", "off")
os.environ.setdefault("YINYUE_FEATURE_INTENTION_TICK", "off")

from core.context.user_prompt import current_message_line  # noqa: E402

FAILED: list[str] = []
CHECKED = 0

_OLD = '用户现在说: "在吗"\n'


def check(name: str, cond: bool, detail: str = "") -> None:
    global CHECKED
    CHECKED += 1
    if cond:
        return
    FAILED.append(name)
    print(f"FAIL {name}" + (f"\n     {detail}" if detail else ""))


class _env:
    """临时改开关（None = 删掉），退出 with 时还原。"""

    def __init__(self, **kw: str | None) -> None:
        self._kw = kw
        self._old: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self._kw.items():
            self._old[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, *exc: object) -> None:
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_off_keeps_old_line() -> None:
    with _env(YINYUE_FEATURE_GROUP_SCENE=None, YINYUE_GROUP_SOCIAL_CHATS="c1"):
        got = current_message_line(
            "在吗", is_group=True, sender_name="P5", chat_id="c1",
        )
    check("开关未设 → 逐字节旧形", got == _OLD, repr(got))


def test_scoped_group_on_labels_speaker() -> None:
    with _env(YINYUE_FEATURE_GROUP_SCENE="on", YINYUE_GROUP_SOCIAL_CHATS="c1"):
        got = current_message_line(
            "在吗", is_group=True, sender_name="P5", chat_id="c1",
        )
    check("作用域内群聊 on → 带说话人", got == '用户现在说（P5）: "在吗"\n', repr(got))


def test_shadow_keeps_old_line() -> None:
    """shadow 的合同是"只审计，不改 prompt"：说话人标签必须等 on。"""
    with _env(YINYUE_FEATURE_GROUP_SCENE="shadow", YINYUE_GROUP_SOCIAL_CHATS="c1"):
        got = current_message_line(
            "在吗", is_group=True, sender_name="P5", chat_id="c1",
        )
    check("shadow → 仍逐字节旧形", got == _OLD, repr(got))


def test_private_out_of_scope_or_unnamed_keeps_old_line() -> None:
    with _env(YINYUE_FEATURE_GROUP_SCENE="on", YINYUE_GROUP_SOCIAL_CHATS="c1"):
        # 私聊不动（§0：不在私聊加多方读场）
        got_private = current_message_line(
            "在吗", is_group=False, sender_name="P5", chat_id="c1",
        )
        # 作用域外不动
        got_out = current_message_line(
            "在吗", is_group=True, sender_name="P5", chat_id="c2",
        )
        # 说话人不可知 → 不猜
        got_unnamed = current_message_line(
            "在吗", is_group=True, sender_name="", chat_id="c1",
        )
    check("私聊不动", got_private == _OLD, repr(got_private))
    check("作用域外不动", got_out == _OLD, repr(got_out))
    check("说话人不可知不猜", got_unnamed == _OLD, repr(got_unnamed))


# ---------------------------------------------------------------------------
# 三个 prompt 路径都真的把说话人带上
# 原来锁的是「conversational.py 里 current_message_line( 恰好 3 次」和
# 「旧的 用户现在说: {user_message} 字面量不在」——那是拿调用次数当装配证据。
# 现在真跑 _synthesize_reply / _build_research_prompt（stream 版已于 2026-10-23 退役）
# 三条路（前两条把 prompt 交给假 LLM，第三第五条直接返回），看产出里有没有说话人。
# ---------------------------------------------------------------------------


class _ProbeLLM:
    """把交给模型的 user 侧 prompt 全录下来。"""

    def __init__(self) -> None:
        self.prompts: list[dict] = []

    async def chat(self, **kw) -> str:
        self.prompts.append(kw)
        return "[emo:日常] 好。"

    def stream_chat(self, **kw):
        self.prompts.append(kw)

        async def _gen():
            yield "好。"

        return _gen()


def _agent(llm: _ProbeLLM):
    from agents import conversational as _conv

    a = _conv.ConversationalAgent.__new__(_conv.ConversationalAgent)
    a._llm = llm  # llm 是 property（agents/conversational.py:1624），底层是 _llm
    a.name = "月"
    a._persona = SimpleNamespace(id=1)
    a.max_iterations = 1
    for name in ("_trace", "_tools_used", "_plan_loop_checks"):
        setattr(a, name, [])
    for name in (
        "_message_time", "_speech_snapshot", "_affect_ctx", "_affect_manager_instance",
        "_repo", "_uq", "_memory", "_planner", "_feedback", "_evolve",
        "_turn_decision",
    ):
        setattr(a, name, None)
    for name in ("_affect_block", "_inbound_content"):
        setattr(a, name, "")
    a._is_group = True
    a._sender_nickname = "P5"
    a._last_context = SimpleNamespace(extra={"chat_id": "c1"})
    a._last_assistant_msg = ""
    a._reason_text = ""
    a._group_recent_context = ""
    a._turn_intent = ""
    a._unknown_answer = False
    a._task_sessions = {}
    a._build_system_prompt = lambda is_group: "SYS"
    return a


async def _drive(agent) -> None:
    await agent._synthesize_reply("在吗", "", "", [])



def test_all_three_render_points_label_the_current_message() -> None:
    with _env(YINYUE_FEATURE_GROUP_SCENE="on", YINYUE_GROUP_SOCIAL_CHATS="c1"):
        llm = _ProbeLLM()
        agent = _agent(llm)
        asyncio.run(_drive(agent))
        research = str(agent._build_research_prompt("在吗", "", "") or "")

    labelled = '用户现在说（P5）: "在吗"'
    bare = '用户现在说: "在吗"'
    prompts = [str(kw.get("user") or "") for kw in llm.prompts]
    check("_synthesize_reply 是唯一一次生成（stream 版已退役）",
          len(prompts) == 1, f"n={len(prompts)}")
    for idx, text in enumerate(prompts):
        check(f"渲染点 {idx + 1} 带上说话人", labelled in text, text[-400:])
        check(f"渲染点 {idx + 1} 没有裸的旧行", bare not in text, text[-400:])
    check("_build_research_prompt 带上说话人", labelled in research, research[-400:])
    check("_build_research_prompt 没有裸的旧行", bare not in research, research[-400:])


def main() -> int:
    test_off_keeps_old_line()
    test_scoped_group_on_labels_speaker()
    test_shadow_keeps_old_line()
    test_private_out_of_scope_or_unnamed_keeps_old_line()
    test_all_three_render_points_label_the_current_message()
    print(f"current message speaker: {CHECKED - len(FAILED)}/{CHECKED} checks passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())