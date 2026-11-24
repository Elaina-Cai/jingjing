"""Step 4 的锁：日级段进 system、此刻段+分界线+视界留 user，且开关关着时一行都不许变。

锁的是分区与顺序，不是措辞：改文案不会红，把「此刻」搬进 system、把视界搬到中间、
或者开关默认变成开，都会红。
"""
from __future__ import annotations

import ast
from pathlib import Path

from core.infra import envflags
from loop import brain

ROOT = Path(__file__).resolve().parents[2]
BRAIN = ROOT / "loop" / "brain.py"

PARTS = [
    ("视界（她看到的世界）", "SNAP-TEXT"),
    ("别处也在动", "OTHERS"),
    ("这一拍为什么醒", "WAKE"),
    ("此刻（心境 / 手头 / 对谁）", "SELF"),
    ("我身上的状况（系统告诉我的事实）", "TROUBLE"),
    ("我的小本本", "BEN"),
    ("这三天的账", "DIGEST"),
    ("还没销账的承诺", "PROMISE"),
    ("我最近开口", "FACTS"),
    ("分界线（以下是这个世界里发生的事）", brain._E1_LINE),
]
HEAD = "HEAD"


def test_flag_defaults_off_and_is_read_once():
    src = BRAIN.read_text(encoding="utf-8")
    assert src.count('flag("PROMPT_SHAPE_V2")') == 1
    envflags_reset = envflags.flag("PROMPT_SHAPE_V2")
    assert envflags_reset is False, "PROMPT_SHAPE_V2 默认必须是关"


def test_reshape_moves_only_daily_sections():
    system, user = brain._reshape(HEAD, PARTS, "SNAP-TEXT")
    for daily in ("BEN", "DIGEST", "PROMISE", "FACTS"):
        assert daily in system, daily
        assert daily not in user, daily
    for now in ("OTHERS", "WAKE", "SELF", "TROUBLE"):
        assert now in user, now
        assert now not in system, now
    assert "SNAP-TEXT" in user and "SNAP-TEXT" not in system
    assert system.startswith(HEAD)


def test_reshape_keeps_exactly_one_separator_and_it_sits_before_the_world():
    system, user = brain._reshape(HEAD, PARTS, "SNAP-TEXT")
    assert system.count(brain._E1_LINE) == 0
    assert user.count(brain._E1_LINE) == 1
    assert user.index(brain._E1_LINE) < user.index("SNAP-TEXT")
    assert user.rstrip().endswith("SNAP-TEXT"), "视界必须是 user 的最后一段"


def test_reshape_keeps_the_order_of_her_own_state():
    _, user = brain._reshape(HEAD, PARTS, "SNAP-TEXT")
    order = [user.index(x) for x in ("OTHERS", "WAKE", "SELF", "TROUBLE")]
    assert order == sorted(order), order


def test_reshape_without_daily_is_a_noop_for_the_head():
    parts = [("视界（她看到的世界）", "S"), ("此刻（心境 / 手头 / 对谁）", "SELF"),
             ("分界线（以下是这个世界里发生的事）", brain._E1_LINE)]
    system, user = brain._reshape("H", parts, "S")
    assert system == "H"
    assert user.split("\n\n")[0] == "SELF"


def test_reshape_is_only_reachable_behind_the_flag() -> None:
    """`_reshape` 只许有一个调用点，而且必须在「开关为真」的分支里。

    开关的值可以被提到变量里（`_v2 = envflags.flag("PROMPT_SHAPE_V2")`），所以判据认两种写法：
    直接 `if flag("PROMPT_SHAPE_V2"):`，或者 `if _v2:`——只要那个变量确实由这条 flag 赋值。
    锁的是"能不能绕过开关"，不是开关写在第几行。
    """
    tree = ast.parse(BRAIN.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_reshape"]
    assert len(calls) == 1, "只许有一个调用点"

    name = "PROMPT_SHAPE_V2"
    flag_vars = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if getattr(node.value.func, "attr", "") != "flag":
            continue
        if not any(getattr(a, "value", "") == name for a in node.value.args):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name):
                flag_vars.add(t.id)
    assert flag_vars, "brain.py 里必须有一次 `flag(\"PROMPT_SHAPE_V2\")` 赋值"

    def is_guard(test: ast.AST) -> bool:
        if isinstance(test, ast.Name):
            return test.id in flag_vars
        return (isinstance(test, ast.Call)
                and getattr(test.func, "attr", "") == "flag"
                and any(getattr(a, "value", "") == name for a in test.args))

    owned = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not is_guard(node.test):
            continue
        for sub in node.body:
            owned.extend(c for c in ast.walk(sub) if c is calls[0])
    assert owned, "唯一的 `_reshape` 调用点必须被开关分支包着"

def test_legacy_join_is_still_there_for_the_flag_off_path():
    src = BRAIN.read_text(encoding="utf-8")
    assert '{"role": "user", "content": "\\n\\n".join(t for _, t in _nparts)}' in src
