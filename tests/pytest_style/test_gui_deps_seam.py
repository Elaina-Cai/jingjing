"""GUI 填字的依赖缝：真机只许在适配层碰。

`_fill_message` 曾经在函数体里 `from core.execution import wx_chat` 直接读顶栏，
绕过了注入的 deps（2026-10-29 实测：机器前台恰好是目标会话时，替身一次
`open_chat` 都收不到，`tests/test_wechat_wake.py` 确定性变红）。
复用判断属于「打开会话」这件事，住在 `_default_deps()::_Deps.open_chat` 里。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "core" / "execution" / "gui" / "wechat.py"


def _tree() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8-sig").lstrip("\ufeff"))


def _find(nodes, kind, name):
    for node in nodes:
        if isinstance(node, kind) and getattr(node, "name", "") == name:
            return node
    return None


def _wx_chat_imports(node) -> list[str]:
    out: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.ImportFrom):
            if sub.module and "wx_chat" in sub.module:
                out.append(sub.module)
            # `from core.execution import wx_chat as x` 的别名在 names 里，module 只是 core.execution
            out += [a.name for a in sub.names if "wx_chat" in a.name]
        elif isinstance(sub, ast.Import):
            out += [a.name for a in sub.names if "wx_chat" in a.name]
    return out


def test_fill_message_does_not_reach_past_the_deps() -> None:
    fn = _find(_tree().body, ast.FunctionDef, "_fill_message")
    assert fn is not None, "wechat.py 里找不到 _fill_message"
    assert _wx_chat_imports(fn) == [], _wx_chat_imports(fn)


def test_reuse_question_lives_in_the_adapter() -> None:
    tree = _tree()
    assert _find(tree.body, ast.FunctionDef, "_already_there") is not None
    factory = _find(tree.body, ast.FunctionDef, "_default_deps")
    assert factory is not None, "找不到 _default_deps"
    deps = _find(factory.body, ast.ClassDef, "_Deps")
    assert deps is not None, "_default_deps() 里没有 _Deps"
    open_chat = _find(deps.body, ast.FunctionDef, "open_chat")
    assert open_chat is not None, "_Deps 没有 open_chat"
    called = [
        n.func.id
        for n in ast.walk(open_chat)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "_already_there" in called, called


def test_top_bar_is_read_in_exactly_one_place() -> None:
    hits = [
        n
        for n in ast.walk(_tree())
        if isinstance(n, ast.Attribute) and n.attr == "verify_active_chat_title"
    ]
    assert len(hits) == 1, len(hits)


def test_protocol_does_not_grow_a_reuse_door() -> None:
    proto = _find(_tree().body, ast.ClassDef, "WechatUiDeps")
    assert proto is not None, "找不到 WechatUiDeps"
    names = [n.name for n in proto.body if isinstance(n, ast.FunctionDef)]
    assert "can_reuse_chat" not in names, names
    assert not [n for n in names if "reuse" in n], names
