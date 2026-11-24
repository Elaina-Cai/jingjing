# -*- coding: utf-8 -*-
"""全仓「用到的名字都绑定过」守卫（复用 `scripts/check_layering.py::undefined_names`）。

为什么值得单独一条
==================
2026-10-23 这一天，这段静态扫描一次抓到**两个真 bug**，而且两个都被各自的
``try/except`` 吞成了静默失败：

1. ``core/routing/turn_pipeline.py``：``from core.routing.memory_notes import stage_notes
   as _stage_notes``，却按原名 ``stage_notes(...)`` 调用 ⇒ ``NameError`` 被
   ``except Exception`` 吃掉、只记一行 ``memory_notes_failed``
   ⇒ **判给的候选便签一次都没写进候选区**（而只测模块本身的用例全绿）。
2. （历史）``core/context/scene_effects.py``：直接写 ``_context = context``，可这个函数根本没有
   ``context`` 形参 ⇒ 一进「按需记忆」那段就 ``NameError``，同样被吞
   ⇒ **按需记忆召回一次都没跑过**，日志里只有一片安静。

这类 bug 的共同点：不抛给用户、不打红任何用例，只在某个分支上永远不执行。
所以它必须是被静的扫描常看着，而不是靠谁想起来去点一遍。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def test_no_module_uses_an_unbound_name() -> None:
    import check_layering as L

    offenders: list[str] = []
    files = list(L.project_files())
    for rel in files:
        for name, line in (L.undefined_names(rel) or {}).items():
            offenders.append(f"{rel}:{line} {name}")
    check("扫到的文件数像个全仓（>500）", len(files) > 500, str(len(files)))
    check("没有一个模块用到没绑定的名字", offenders == [], "; ".join(offenders[:6]))


def test_stage_notes_is_imported_by_the_name_it_is_called_with() -> None:
    """别名导入 + 原名调用 = 必然 NameError（本轮踩过的那个，守卫换到新生产者身上）。

    N1 退役（2026-10-23）后候选便签的生产者搬到 perceive 侧：落地调用只剩
    ``agents/conversational.py::_stage_perceive_notes`` 一处，规矩与当年一样——
    **导入什么名字就叫什么名字**，别名 + 原名混用必然 NameError 且会被兜底吞掉。
    """
    conv = (ROOT / "agents/conversational.py").read_text(encoding="utf-8")
    check(
        "conversational 按原名导入 stage_notes",
        "from core.routing.memory_notes import stage_notes" in conv
        and "import stage_notes as" not in conv,
    )
    check("conversational 真的按原名调用", "stage_notes(" in conv)
    tp = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    check("turn_pipeline 不再生产便签（N1 已退役）", "stage_notes" not in tp)


def main() -> int:
    for fn in (
        test_no_module_uses_an_unbound_name,
        test_stage_notes_is_imported_by_the_name_it_is_called_with,
    ):
        print(fn.__name__)
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            import traceback

            traceback.print_exc()
            FAILS.append(f"{fn.__name__} raised {exc!r}")
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    for item in FAILS:
        print(f"  - {item}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())