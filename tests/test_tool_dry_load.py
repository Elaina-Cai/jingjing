# -*- coding: utf-8 -*-
"""工具声明面**干装载**：全 None 依赖构造全部工具，只验声明——不执行、不连平台、不写状态。

对应《从 Cortico 学什么》的 3 号。Cortico 的 ``check:extension`` 在假部署里调
``create() / tools() / console()``，**不调 ``start()``**；这里同一套思路。

四条检查
========
1. **干装载**：``ToolRegistry.create_default()``（全 None 依赖）不抛，工具数 >= 15。
2. **声明面**：name 唯一且为 snake_case；description 非空；每个参数有 type + description。
3. **schema 形状**：``schema()`` 结构合法——**模型看到的就是它**。
4. **死代码**：``agents/tools/__init__.py`` 的 ``__all__`` 里每个工具类都必须
   **至少有一个注册点**，否则就是「写了但没接线」。

第 4 条 2026-10-10 首次跑就抓到 ``CorrectEmojiDescTool``：类、name、description、
parameters、``execute()`` 全都有，全仓 **0 个 register 调用点**。白名单里登记着，
逼一次「接线 or 删除」的决定，而不是让它继续藏着。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MIN_TOOLS = 15

# 「写了但没接线」的豁免名单。**只许减少**：每一条都必须在注释里写明待办。
# 2026-10-10：CorrectEmojiDescTool 已接线（agents/tools/base.py），白名单清空。
# 这条守卫的价值就在这儿——它逼了一次决定，而不是让死代码继续藏着。
UNWIRED_ALLOWED: dict[str, str] = {}


def _registry():
    from agents.tools.base import ToolRegistry

    return ToolRegistry.create_default()


def test_tools_dry_load_with_null_deps():
    """全 None 依赖必须能构造出工具表（构造期不碰库、不联网、不起进程）。"""
    tools = _registry().all_tools()
    assert len(tools) >= MIN_TOOLS, f"干装载只得到 {len(tools)} 个工具，少于 {MIN_TOOLS}"
    return True


def test_tool_declarations_are_complete():
    """name 合法且唯一；description 非空；每个参数有 type 与 description。"""
    tools = _registry().all_tools()
    problems: list[str] = []

    names = list(tools)
    if len(names) != len(set(names)):
        dupes = sorted({n for n in names if names.count(n) > 1})
        problems.append(f"工具名重复（register 会静默覆盖）：{dupes}")

    for name, tool in sorted(tools.items()):
        if not name or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            problems.append(f"{name}: name 不是 snake_case")
        if len(str(tool.description or "").strip()) < 10:
            problems.append(f"{name}: description 过短（模型只靠它判断何时用）")
        for pname, info in (tool.parameters or {}).items():
            if not isinstance(info, dict):
                problems.append(f"{name}.{pname}: 参数声明不是 dict")
                continue
            if not info.get("type"):
                problems.append(f"{name}.{pname}: 缺 type")
            if not str(info.get("description") or "").strip():
                problems.append(f"{name}.{pname}: 缺 description（模型猜不到怎么填）")

    assert not problems, "工具声明面有问题：\n  " + "\n  ".join(problems)
    return True


def test_tool_schemas_have_openai_shape():
    """``schema()`` 是模型真正看到的东西，形状必须合法。"""
    problems: list[str] = []
    for name, tool in sorted(_registry().all_tools().items()):
        schema = tool.schema()
        fn = (schema or {}).get("function") or {}
        if schema.get("type") != "function":
            problems.append(f"{name}: schema.type != function")
        if fn.get("name") != name:
            problems.append(f"{name}: schema.function.name 对不上（{fn.get('name')!r}）")
        if not str(fn.get("description") or "").strip():
            problems.append(f"{name}: schema 没有 description")
        params = fn.get("parameters") or {}
        if params.get("type") != "object":
            problems.append(f"{name}: parameters.type != object")
        if "properties" not in params:
            problems.append(f"{name}: parameters 缺 properties")
    assert not problems, "schema 形状不合法：\n  " + "\n  ".join(problems)
    return True


def test_every_exported_tool_is_wired():
    """``__all__`` 里的工具类必须有注册点；豁免名单只许减少。"""
    init_src = (ROOT / "agents" / "tools" / "__init__.py").read_text(encoding="utf-8")
    # 基类不是工具，不在守卫范围内。
    exported = set(re.findall(r'"(\w+Tool)"', init_src)) - {"BaseTool"}

    wired: set[str] = set()
    for path in (ROOT / "agents").rglob("*.py"):
        try:
            src = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        # 两种注册写法都要认：
        #   ① 内联  register(SomeTool(...))
        #   ② 变量  tool = SomeTool(...)  然后  register(tool)
        wired |= set(re.findall(r"register\(\s*(\w+Tool)\s*\(", src))
        wired |= set(re.findall(r"=\s*(\w+Tool)\s*\(", src))

    dead = sorted(exported - wired - set(UNWIRED_ALLOWED))
    assert not dead, (
        "这些工具类写了但没有任何注册点（死代码）：\n  "
        + "\n  ".join(f"{n} —— 要么接线，要么删掉" for n in dead)
        + "\n\n若确属有意保留，加进本文件的 UNWIRED_ALLOWED 并写明待办。"
    )

    # 豁免名单只许减少：已经接线的要从白名单里摘掉。
    stale = sorted(n for n in UNWIRED_ALLOWED if n in wired)
    assert not stale, f"这些已经在白名单里但已经接线了，请从 UNWIRED_ALLOWED 摘掉：{stale}"
    return True

if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)