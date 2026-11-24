"""监管器的退场判据：主程序活着不等于她能说话（2026-10-30 事故的锁）。

事故原文（`logs/supervisor.out`）：主程序心跳还在 → 监管器退出 → 引擎已死没人拉起。
所以判据必须是「引擎是不是 alive」，不是「主程序在不在」。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUP = ROOT / "scripts" / "supervisor.py"


def _supervisor():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("sup_under_test", SUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("sched,engine,want", [
    (True, "alive", False),    # 两边都活 → 不接管（真有人在跑）
    (True, "stale", True),     # 主程序活、引擎卡死 → 必须接管
    (True, "gone", True),      # 引擎进程没了 → 接管
    (True, "unknown", True),   # 心跳不合契约 → 接管（互斥体兜住不会双开）
    (False, "alive", True),    # 主程序没了 → 接管
])
def test_take_over_follows_the_engine(sched, engine, want):
    assert _supervisor()._take_over(sched, engine) is want


def test_main_does_not_stand_down_on_scheduler_alone():
    """`main()` 里不许再出现 `if _scheduler_alive(): return 0` 这种单条件退场。

    用 AST 判结构，不判字面量：这条判据存在的目的就是"别只看主程序心跳"，
    而文档注释里会引用那句原文，子串断言会把它自己咬掉（今天已经咬过一次）。
    """
    import ast

    tree = ast.parse(SUP.read_text(encoding="utf-8"))
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    bad = [
        n for n in main.body
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Call)
        and getattr(n.test.func, "id", "") == "_scheduler_alive"
    ]
    assert bad == [], "main() 仍然只凭主程序心跳就退场"
    src = SUP.read_text(encoding="utf-8")
    assert "_take_over(_sched, _eng)" in src
