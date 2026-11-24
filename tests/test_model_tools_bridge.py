from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.tools import jobs as J
from agents.tools.base import ToolRegistry

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool) -> None:
    global COUNT
    COUNT += 1
    print(("  ok " if cond else "  FAIL ") + name)
    if not cond:
        FAILS.append(name)


class _Ctx:
    def __init__(self, chat_id: str = "") -> None:
        self.chat_id = chat_id
        self.extra: dict = {}


def t_shape() -> None:
    t = J.SendPlanTool()
    check("名字是 send_plan", t.name == "send_plan")
    # 2026-10-23（第 6 件）：多了可选 criteria（「怎么算办成了」）。
    # 必填仍然只有 target/text —— 判据可以不写，写事件不允许漏。
    check("参数有 target/text", {"target", "text"} <= set(t.parameters))
    check("只多一个可选 criteria", set(t.parameters) - {"target", "text"} == {"criteria"})
    check("target/text 必填、criteria 不必填",
          bool(t.parameters["target"].get("required"))
          and bool(t.parameters["text"].get("required"))
          and not t.parameters["criteria"].get("required"))
    check("描述里写明不直发", "不直接发送" in t.description)
    g = J.UiStateTool()
    check("ui_state 存在", g.name == "ui_state")


def t_no_chat() -> None:
    t = J.SendPlanTool()
    t.set_context(_Ctx(""))
    r = asyncio.run(t.execute(target="鲸鲸的测试群", text="在吗"))
    check("没有会话就不猜", "不猜" in str(r))


def t_missing_args() -> None:
    t = J.SendPlanTool()
    t.set_context(_Ctx("wxid_x"))
    r = asyncio.run(t.execute(target="", text=""))
    check("缺参数不开事", "没开成" in str(r))


def t_no_send_path() -> None:
    src = (ROOT / "agents/tools/jobs.py").read_text(encoding="utf-8")
    check("壳里没有真发送", all(b not in src for b in ("send_and_verify", "press_enter", "gateway.send")))
    check("壳里只走 plan_block.consume", "plan_block.consume" in src)


def t_registered_in_default() -> None:
    src = (ROOT / "agents/tools/base.py").read_text(encoding="utf-8")
    check("create_default 注册了 send_plan", "registry.register(SendPlanTool())" in src)
    check("create_default 注册了 ui_state", "registry.register(UiStateTool())" in src)
    reg = ToolRegistry()
    reg.register(J.SendPlanTool())
    reg.register(J.UiStateTool())
    names = set(reg.all_tools())
    check("注册表里能看到两个新工具", {"send_plan", "ui_state"} <= names)


def main() -> int:
    for fn in (t_shape, t_no_chat, t_missing_args, t_no_send_path, t_registered_in_default):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
