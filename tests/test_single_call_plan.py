from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import planner

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


def t_default_on() -> None:
    os.environ.pop("YINYUE_SINGLE_CALL_PLAN", None)
    check("默认开（B 路径）", planner.single_call_plan() is True)


def t_env_off() -> None:
    for raw in ("0", "false", "off", "no", "FALSE"):
        os.environ["YINYUE_SINGLE_CALL_PLAN"] = raw
        check(f"{raw} → 回退旧路径", planner.single_call_plan() is False)
    os.environ.pop("YINYUE_SINGLE_CALL_PLAN", None)


def t_planner_silent_when_single() -> None:
    os.environ.pop("YINYUE_SINGLE_CALL_PLAN", None)
    calls: list[str] = []

    async def send(system: str, user: str) -> str:
        calls.append(user)
        return "no"

    out = asyncio.run(
        planner.maybe_open_job(user_text="在群里说一句：记得带伞", chat_id="c1", send=send)
    )
    check("单调用时规划器不开事", out is None)
    check("单调用时一次模型都不调", calls == [])


def t_static_no_double_call() -> None:
    src = (ROOT / "core/jobs/planner.py").read_text(encoding="utf-8")
    check("planner 有 single_call 闸", "job_planner_skipped reason=single_call_plan" in src)
    check("闸在 mode 判断之前", src.index("single_call_plan():") < src.index("planner_mode() == features.MODE_OFF"))


def main() -> int:
    for fn in (t_default_on, t_env_off, t_planner_silent_when_single, t_static_no_double_call):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
