"""person_notes：``[场外补充]`` 渲染规则、上限与开关作用域。

2026-09-24：``[在场人·备注]`` 块整块删除（它与 ``[群成员名单·权威]`` 共用同一批
逐人卡，身份被渲染两次；活跃统计一并砍掉）。本文件保留 ``[场外补充]`` 的用例，
并加一条"块确实没了"的结构守卫。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.assembler import _classify  # noqa: E402
from core.context.person_notes import (  # noqa: E402
    build_chat_context_block,
    notes_enabled_for,
)


def test_chat_context_block_shape() -> None:
    """场外补充：群基线 + 她的立场（多行用 | 分隔）。"""
    block = build_chat_context_block(
        "c@chatroom",
        lines=["示例宿舍群：日常拆台、玩梗，玩笑为主 | 她拒过换皮咒语，立场：我是鲸鲸"],
    )
    assert block.startswith("[场外补充]")
    lines = block.splitlines()
    assert len(lines) == 3, lines
    assert lines[1].startswith("- 示例宿舍群")
    assert "立场：我是鲸鲸" in lines[2]


def test_chat_context_block_caps() -> None:
    assert build_chat_context_block("c@chatroom", lines=[]) == ""
    assert build_chat_context_block("c@chatroom", lines=["  "]) == ""
    many = build_chat_context_block(
        "c@chatroom", lines=["一", "二", "三", "四", "五", "六"], max_lines=3
    )
    assert len(many.splitlines()) == 4  # 头 + 3 行
    tiny = build_chat_context_block("c@chatroom", lines=["很长" * 100], max_chars=20)
    assert tiny == ""


def test_assembler_knows_chat_notes() -> None:
    blocks = _classify(["[场外补充]\n- 群基线", "[人格]\n- 短句。"])
    assert blocks[0].source == "context.chat_notes"
    assert blocks[0].name == "[场外补充]"


def test_present_people_block_is_removed() -> None:
    """[在场人·备注] 已删除：模块里不应再有它的构造函数；即使有人把这段文本塞进来，
    assembler 也不把它当"认可的块"（显式标 source=undeclared，不被静默砍、也不顶名）。

    2026-09-24 起：未登记块的处理从"沿用上一块 name/source"改成"显式标记"
    （见 tests/test_context_budget_contract.py）——这条守卫改为断言标记结果。
    """
    import core.context.person_notes as pn

    assert not hasattr(pn, "build_person_notes_block"), "构造器应已删除"
    blocks = _classify(["[在场人·备注]\n- Y-K：爱起哄。", "[人格]\n- 短句。"])
    assert blocks[0].source == "undeclared", blocks[0]  # 不是被认可的块
    assert blocks[1].name == "[人格]" and blocks[1].source == "persona.identity", blocks[1]


def test_flag_scoping() -> None:
    os.environ.pop("YINYUE_FEATURE_PERSON_NOTES", None)
    os.environ.pop("YINYUE_PERSON_NOTES_CHATS", None)
    os.environ.pop("YINYUE_CHAT_WINDOW_CHATS", None)
    assert notes_enabled_for("a@chatroom") is False
    os.environ["YINYUE_FEATURE_PERSON_NOTES"] = "1"
    assert notes_enabled_for("a@chatroom") is True
    os.environ["YINYUE_PERSON_NOTES_CHATS"] = "a@chatroom"
    assert notes_enabled_for("a@chatroom") is True
    assert notes_enabled_for("b@chatroom") is False
    # 未配自己的名单时回落到窗口名单
    os.environ.pop("YINYUE_PERSON_NOTES_CHATS", None)
    os.environ["YINYUE_CHAT_WINDOW_CHATS"] = "b@chatroom"
    assert notes_enabled_for("b@chatroom") is True
    assert notes_enabled_for("a@chatroom") is False
    for key in (
        "YINYUE_FEATURE_PERSON_NOTES",
        "YINYUE_PERSON_NOTES_CHATS",
        "YINYUE_CHAT_WINDOW_CHATS",
    ):
        os.environ.pop(key, None)


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
