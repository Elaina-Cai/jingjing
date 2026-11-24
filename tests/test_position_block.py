# -*- coding: utf-8 -*-
"""Step 7：位置投影渲染与注入开关测试。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.assembler import _BLOCK_POLICY  # noqa: E402
from core.context.position import (  # noqa: E402
    POSITION_HEADER,
    position_enabled,
    render_position_block,
)


def test_position_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("YINYUE_FEATURE_GROUP_SCENE", raising=False)
    monkeypatch.delenv("YINYUE_GROUP_SOCIAL_CHATS", raising=False)
    assert position_enabled("c1", True) is False
    assert render_position_block("c1", True) == ""


def test_position_on_respects_group_and_scope(monkeypatch) -> None:
    monkeypatch.setenv("YINYUE_FEATURE_GROUP_SCENE", "on")
    monkeypatch.setenv("YINYUE_GROUP_SOCIAL_CHATS", "a,b")
    block = render_position_block("a", True)
    assert block.startswith(POSITION_HEADER)
    assert "不站到他对面" in block
    assert render_position_block("c", True) == ""
    assert render_position_block("a", False) == ""


def test_position_shadow_does_not_inject(monkeypatch) -> None:
    monkeypatch.setenv("YINYUE_FEATURE_GROUP_SCENE", "shadow")
    monkeypatch.setenv("YINYUE_GROUP_SOCIAL_CHATS", "a")
    assert render_position_block("a", True) == ""


def test_extra_lines_are_appended(monkeypatch) -> None:
    monkeypatch.setenv("YINYUE_FEATURE_GROUP_SCENE", "on")
    monkeypatch.setenv("YINYUE_GROUP_SOCIAL_CHATS", "a")
    block = render_position_block("a", True, extra_lines=["- 主人刚在维护你"])
    assert "主人刚在维护你" in block


def test_position_prefix_is_registered_for_assembler() -> None:
    prefixes = tuple(item[0] for item in _BLOCK_POLICY)
    assert POSITION_HEADER in prefixes


def test_extra_lines_from_position_projection(monkeypatch) -> None:
    from core.context.situation import WorldModel, note_position, position_lines

    monkeypatch.setenv("YINYUE_FEATURE_GROUP_SCENE", "on")
    monkeypatch.setenv("YINYUE_GROUP_SOCIAL_CHATS", "a")
    model = WorldModel()
    note_position(model, "owner_teased", "主人最近被逗/被起哄")
    block = render_position_block("a", True, extra_lines=position_lines(model))
    assert "主人最近被逗" in block

if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)