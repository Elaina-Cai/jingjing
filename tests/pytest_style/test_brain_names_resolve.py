"""brain.py 里的名字必须真的存在（2026-10-30 事故：三处静默 NameError 让循环连续崩了四步）。

事故形态：`_v2 = envflags.flag(...)` 用了 `envflags` 却没导入模块名；`if brief.enabled()` 用了
`brief` 而 `from . import ...` 列表里没有它；`_exec` 里局部 `_text = str(args.get("text"))`
遮蔽了模块别名 `_text`，于是同一函数后面的 `_text("YINYUE_SAY_TRIES", "3")` 变成"读了未赋值的局部变量"。

三处都被外层 `except Exception: pass` 吃掉 → 表现只是"她不理人"，没有报错。

所以这条锁不测行为，只测三件静态事实：
1. 用到的那几个模块级别名，必须在模块顶部真的导入过；
2. 任何函数里**不许**给一个模块级别名赋新值（遮蔽）；
3. 任何函数里同一个名字不许既被赋值又被当函数调用（`UnboundLocalError` 的经典形状）。

注意：**只测“赋值 + 同一函数里当函数调用”这一种形状**。单纯遮蔽（比如局部 `text = ...`）不报——
那是风格问题，不是崩溃点；而 `_exec` 里 `_text = ...` 又 `_text(...)` 才是真正炸过的那一种。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRAIN = ROOT / "loop" / "brain.py"

# 这个模块靠"别名即模块"的风格拼提示词，所以显式列出必须存在的别名。
ALIASES = (
    "envflags", "brief", "promptlog", "selfcard", "selfstate", "ledger",
    "memo", "history", "cards", "bond", "ports", "tempo", "duration", "channel",
)


def _tree() -> ast.Module:
    return ast.parse(BRAIN.read_text(encoding="utf-8").lstrip("\ufeff"))


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def _functions(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_aliases_are_imported() -> None:
    tree = _tree()
    bound = _module_level_names(tree)
    used = {
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in ALIASES
    }
    missing = sorted(used - bound)
    assert missing == [], "brain.py 用了这些别名却没导入：%r" % (missing,)


def test_no_name_is_both_assigned_and_called() -> None:
    tree = _tree()
    bad = []
    for fn in _functions(tree):
        stored, called = set(), set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                stored.add(node.id)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)
        for name in sorted(stored & called):
            bad.append("%s 里 %r 既被赋值又被当函数调用 → 会 UnboundLocalError" % (fn.name, name))
    assert bad == [], "\n".join(bad)
