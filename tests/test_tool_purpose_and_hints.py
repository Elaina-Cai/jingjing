# -*- coding: utf-8 -*-
"""工具说明要像人话、失败要给出"下一步"。

判据（2026-10-21 真机）：模型下一轮唯一读得到的是这几行字。
- 只有 `no_target` 这种码，它只能猜或复读"我发不了"；
- 有"先看窗口是不是最小化到托盘"，它就能换方法。

跑法：`.venv_it\\Scripts\\python.exe -X utf8 -u tests\\test_tool_purpose_and_hints.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.tools import builtin, turnloop  # noqa: E402
from core.tools.prompt import render_spec_line, render_tool_block  # noqa: E402
from core.tools.registry import NEXT_STEP_HINTS, hint_for  # noqa: E402
from core.tools.turnloop import ToolOutcome  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if ok:
        print("  ok  " + name)
    else:
        FAILS.append(name + " :: " + str(detail))
        print("  FAIL " + name + " :: " + str(detail))


def t_every_spec_has_purpose() -> None:
    specs = builtin.all_specs()
    check("工具数 ≥ 8", len(specs) >= 8, len(specs))
    for spec in specs:
        purpose = str(getattr(spec, "purpose", "") or "").strip()
        check(spec.name + " 有 purpose", bool(purpose), purpose)
        check(spec.name + " purpose ≤ 40 字", len(purpose) <= 40, len(purpose))
    line = render_spec_line(next(x for x in specs if x.name == "send_plan"))
    check("清单行带 ｜purpose", "｜" in line, line)
    check("send_plan 说明里有「批准」", "批准" in line, line)
    block = render_tool_block(specs)
    check("整块里能读到 send_plan", "send_plan" in block, block[:120])


def t_hints() -> None:
    for code in ("no_target", "not_connected", "ui_blocked", "not_enabled", "too_many_calls"):
        check(code + " 有提示", bool(hint_for(code)), hint_for(code))
    check("ok 不给提示", hint_for("ok") == "")
    check("空码不给提示", hint_for("") == "")
    check("不认识的码不编", hint_for("some_new_code") == "")
    verbs = ("先", "换", "把", "不要", "缩小", "等", "按", "只", "看", "告", "用", "别")
    bad = [k for k, v in NEXT_STEP_HINTS.items() if not any(b in v for b in verbs)]
    check("每条提示都在说该怎么做（含动作词）", not bad, bad)
    check("提示不重复码本身", all(v.strip() != k for k, v in NEXT_STEP_HINTS.items()))


def t_turnloop_appends_hint() -> None:
    outs = (
        ToolOutcome(name="gui_click", purpose="点一下", status="no_target", content="没找到元素"),
        ToolOutcome(name="web_search", purpose="查一下", status="ok", content="三条结果"),
    )
    text = turnloop.render_results(outs)
    check("失败行带「下一步」", "下一步：" in text, text)
    check("成功行不带", text.count("下一步：") == 1, text)
    check("提示说的是动作不是码", "先确认目标名字" in text, text)


def t_static() -> None:
    src = (ROOT / "core/tools/registry.py").read_text(encoding="utf-8")
    check("registry 定义了 NEXT_STEP_HINTS", "NEXT_STEP_HINTS" in src)
    check("registry 有 hint_for", "def hint_for(" in src)
    prompt_src = (ROOT / "core/tools/prompt.py").read_text(encoding="utf-8")
    check("prompt 用 purpose", 'getattr(spec, "purpose"' in prompt_src)
    loop_src = (ROOT / "core/tools/turnloop.py").read_text(encoding="utf-8")
    check("turnloop 用 hint", "_hint_for(o.status)" in loop_src)


def main() -> int:
    t_every_spec_has_purpose()
    t_hints()
    t_turnloop_appends_hint()
    t_static()
    print("")
    print("RESULT " + str(len(FAILS)) + " failed / " + str(COUNT) + " checks")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
