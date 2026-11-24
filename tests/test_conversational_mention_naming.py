# -*- coding: utf-8 -*-
"""``ConversationalAgent._observe_group_mention_target`` 命名契约（原 ``_prefix_group_mention``）。

评审发现：名字说"给群聊回复加 @ 前缀"，四条 return 分支全是原样返回——行为是
"观测 + 记日志"，不是"改文本"。本用例把两件事钉住：

1. **行为**：四条早退分支与正常分支都原样返回（一字不改）；
2. **命名**：旧名在 ``agents/`` 下不再存在（叫 prefix 就会再被当成 @ 前缀的实现处），
   调用点也不再写 ``reply = self._prefix_group_mention(...)`` 这种死赋值。

Run directly:
    python tests/test_conversational_mention_naming.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "agents"


def _agent(**attrs):
    from agents.conversational import ConversationalAgent

    agent = ConversationalAgent.__new__(ConversationalAgent)
    agent._is_group = attrs.get("is_group", True)
    agent._sender_nickname = attrs.get("nickname", "小A")
    agent._inbound_content = attrs.get("inbound", "")
    # persona 是只读 property：走私有字段（测试只需要 .name）。
    agent._persona = type("P", (), {"name": attrs.get("self_name", "鲸鲸")})()
    return agent


def test_returns_reply_unchanged_on_every_branch():
    """四条分支 + 正常路径都原样返回（不加 @、不删字、不 trim）。"""
    reply = "在的，你说。"
    cases = [
        ("非群聊", _agent(is_group=False), reply, "随便说的"),
        ("空回复", _agent(), "", "随便说的"),
        ("昵称缺失", _agent(nickname=""), reply, "随便说的"),
        ("昵称是占位符", _agent(nickname="unknown"), reply, "随便说的"),
        ("正常群聊（@ 别人）", _agent(), reply, "@Elaina 你说的那个我记着了"),
        ("正常群聊（无 @）", _agent(), reply, "今天谁值班"),
    ]
    for label, agent, text, inbound in cases:
        got = agent._observe_group_mention_target(text, inbound)
        assert got == text, (label, repr(got), repr(text))
    # 带首尾空白的回复也不许被 trim（它不是"清洗"环节）
    padded = "  在的。  "
    assert _agent()._observe_group_mention_target(padded, "在吗") == padded
    return True


def test_old_name_is_gone_and_call_site_is_not_a_dead_assignment():
    """结构守卫：旧名不残留；调用点不再把返回值赋回 ``reply``。"""
    src = (AGENTS / "conversational.py").read_text(encoding="utf-8")
    assert "_prefix_group_mention" not in src, "旧名（prefix）必须已经改掉"
    assert "_observe_group_mention_target" in src, "新名（observe）必须存在"
    assert not re.search(r"reply\s*=\s*self\._observe_group_mention_target\(", src), \
        "返回值必然等于入参，赋回 reply 是死赋值"
    # 调用点保留（它仍要打 group_mention_retargeted 日志）
    assert "self._observe_group_mention_target(reply, user_message)" in src, \
        "观测调用点必须保留（否则丢掉 @ 目标日志）"
    for path in AGENTS.rglob("*.py"):
        assert "_prefix_group_mention" not in path.read_text(encoding="utf-8"), path
    return True


def test_docstring_states_it_never_changes_text():
    """docstring 必须写明"不改一个字"，否则下一个人还会按名字理解它。"""
    from agents.conversational import ConversationalAgent

    doc = ConversationalAgent._observe_group_mention_target.__doc__ or ""
    assert "不改" in doc or "从不" in doc, doc
    assert "日志" in doc, doc
    return True


def main() -> bool:
    tests = [
        test_returns_reply_unchanged_on_every_branch,
        test_old_name_is_gone_and_call_site_is_not_a_dead_assignment,
        test_docstring_states_it_never_changes_text,
    ]
    print("=" * 60)
    print("conversational mention naming checks")
    print("=" * 60)
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
            import traceback

            traceback.print_exc()
    print(f"\nconversational-naming checks: "
          f"{len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
