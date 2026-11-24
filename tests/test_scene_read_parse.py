# -*- coding: utf-8 -*-
"""``core/context/scene_read_parse.py`` 的边界锁（R2 从 ``intent.py`` 迁出）。

这个模块存在的唯一理由是**纯解析可以脱离回合单独跑**，所以锁四件事：

1. 顶层只 import ``typing``（``llm.schema`` 是函数内局部导入 —— 单独 import 本模块
   不该把 llm 拖起来）；
2. 不许反向依赖 ``core.context.intent``（那是 4200 行、会把整条上下文链拉起来的宿主）；
3. 不许碰 IO / 数据库 / 模型（它是形状守门人，不是判官）；
4. 宿主导入面只剩本模块自己（``core.context.intent`` 已随 N1 退役删除）。

Run directly: python tests/test_scene_read_parse.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULE = ROOT / "core" / "context" / "scene_read_parse.py"

FAILS: list[str] = []
COUNT = [0]


def check(label: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok  {label}")
    else:
        FAILS.append(label if not detail else f"{label} :: {detail}")
        print(f"  --  {label} :: {detail}")


def _tree() -> ast.Module:
    return ast.parse(MODULE.read_text(encoding="utf-8-sig"))


def t_top_level_imports_are_typing_only() -> None:
    allowed = {"typing", "__future__"}
    got: list[str] = []
    for node in _tree().body:
        if isinstance(node, ast.Import):
            got.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            got.append(node.module)
    extra = [name for name in got if name not in allowed]
    check("顶层只 import typing/__future__", not extra, str(extra))


def t_no_back_reference_to_intent() -> None:
    src = MODULE.read_text(encoding="utf-8")
    check("不 import intent", "core.context.intent" not in src and "from core.context import intent" not in src)


def t_llm_is_imported_lazily() -> None:
    src = MODULE.read_text(encoding="utf-8")
    tree = _tree()
    top_llm = [
        node for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and (getattr(node, "module", "") or "") == "llm"
    ]
    check("llm 不在顶层 import", not top_llm)
    check("llm 在函数体内 import", "    from llm import schema" in src)


def t_parsing_only() -> None:
    src = MODULE.read_text(encoding="utf-8")
    for word in ("sqlite3", "open(", "llm.chat", "structlog", "requests", "time.sleep"):
        check(f"解析模块不碰 {word}", word not in src, word)


def t_roles_are_the_four_chairs() -> None:
    from core.context import scene_read_parse

    check("SCENE_READ_KEY 一致", scene_read_parse.SCENE_READ_KEY == "scene_read")
    check(
        "SCENE_READ_ROLES 一致",
        scene_read_parse.SCENE_READ_ROLES == ("her", "owner", "others", "room"),
        str(scene_read_parse.SCENE_READ_ROLES),
    )


def t_behaviour_is_unchanged() -> None:
    from core.context.scene_read_parse import scene_read_of

    check("空载荷 → None", scene_read_of({}) is None)
    check("没有 scene_read 段 → None", scene_read_of({"intent": "闲聊"}) is None)
    ok = scene_read_of({"scene_read": {"roles": {"her": 0.5}, "about": "药"}})
    check("正常段返回 dict", isinstance(ok, dict), str(type(ok)))
    check("roles 归一成四把椅子", isinstance(ok.get("roles"), dict) and set(ok["roles"]) == {"her", "owner", "others", "room"}, str(ok.get("roles")))
    bad = scene_read_of({"scene_read": {"roles": {"her": "高"}}})
    check("坏类型进 problems 不抛", bool(bad.get("problems")), str(bad))


def main() -> int:
    print()
    print("scene_read_parse (R2)")
    for fn in (
        t_top_level_imports_are_typing_only,
        t_no_back_reference_to_intent,
        t_llm_is_imported_lazily,
        t_parsing_only,
        t_roles_are_the_four_chairs,
        t_behaviour_is_unchanged,
    ):
        print(f"[{fn.__name__}]")
        fn()
    print()
    print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
    for line in FAILS:
        print("  -", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
