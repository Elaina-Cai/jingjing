from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents import planner
from core.tools import builtin, turnloop

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool) -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}")


def t_view_allows() -> None:
    check("send_plan 在只读视图白名单里", "send_plan" in planner.PLAN_LOOP_READ_ONLY_TOOLS)
    reg = builtin.build_registry()
    view = planner.PlanExecToolView(reg)
    check("视图能取到 send_plan", view.get("send_plan") is not None)
    check("视图仍挡发送类工具", view.get("send_message") is None)


def t_still_no_direct_send() -> None:
    src = (ROOT / "core/tools/builtin.py").read_text(encoding="utf-8")
    i = src.find("def send_plan_handler")
    body = src[i : i + 2000]
    check("handler 仍不直发", all(bad not in body for bad in ("send_and_verify", "press_enter", "gateway.send")))


def t_narrow_for_task_turns() -> None:
    names = ["acg_lookup", "send_plan", "web_search", "get_weather", "gui_state"]
    schemas = [{"name": n} for n in names]
    picked = [x["name"] for x in turnloop.narrow_schemas(schemas)]
    check("任务回合收窄后只留能动手的", picked == ["send_plan", "gui_state", "web_search"])
    check("收窄后必然含 send_plan", "send_plan" in picked)
    check("一个都不命中就原样返回（宁可不收窄）",
          turnloop.narrow_names(["x", "y"]) == ["x", "y"])


def main() -> int:
    for fn in (t_view_allows, t_still_no_direct_send, t_narrow_for_task_turns):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
