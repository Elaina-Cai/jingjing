# -*- coding: utf-8 -*-
"""工具注册表的装填：登记七字段 / 开关 / 同步桥 / 状态差异式回执。

跑法：``python -X utf8 -u tests/test_tool_registry_binding.py``
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：不给 dir= 时系统 TEMP 不可写会静默回落到仓库根。
from _tempdirs import temp_base  # noqa: E402

from core.tools import builtin  # noqa: E402
from core.tools.registry import Effect, ToolResult, ToolSpec  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name} :: {detail}")
        FAILED.append(name)


class FakeAsyncTool:
    """冒充 agents/tools/base.py::BaseTool（只用到 name / parameters / execute）。"""

    def __init__(self, name: str = "web_search", out: str = "杭州三日：西湖、灵隐寺", sleep: float = 0.0):
        self.name = name
        self.parameters = {"query": {"type": "string", "required": True}}
        self._out = out
        self._sleep = sleep
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self._sleep:
            import asyncio

            await asyncio.sleep(self._sleep)
        return self._out


def main() -> int:
    # ---- 1. 登记七字段 ----
    specs = {s.name: s for s in builtin.all_specs()}
    check("十种工具都已登记",
          set(specs)
          == {
              "web_search",
              "fetch_url",
              "gui_snapshot",
        "ui",
        "ui_state",




              "get_current_time",
              "write_note",
              "send_plan",
          },
          str(sorted(specs)))
    for name, spec in specs.items():
        ok, reason = spec.validate()
        check(f"{name} 七字段齐全", ok, reason)
    # send_plan 是唯一带 external_send 的规格，但它**不开闸**：handler 只开一件事、
    # 停在等主人批准，一个字节都不发（`check_call` 对它恒返 external_send_via_exit）。
    senders = {n for n, s in specs.items() if Effect.EXTERNAL_SEND in s.effects}
    check("只有 send_plan 带 external_send（它不发、只开事）", senders == {"send_plan"}, str(senders))

    # ---- 2. 开关：读默认开、写默认关 ----
    reg = builtin.build_registry({})
    check("读工具默认启用", reg.get("web_search").enabled and reg.get("fetch_url").enabled, "")
    check("写工具默认不启用", not reg.get("write_note").enabled, "")

    reg2 = builtin.build_registry({}, enable_writes=True)
    check("enable_writes=True 才开写", reg2.get("write_note").enabled, "")

    # ---- 3. 未绑 handler 的工具调不动 ----
    res = reg.observe("x", "杭州", {"tool": "web_search"})
    check("没绑 handler 的读工具返回 not_enabled", res.status == "not_enabled", res.status)

    # ---- 4. 同步桥：把异步实现包起来 ----
    tool = FakeAsyncTool()
    reg3 = builtin.build_registry({"web_search": tool})
    res3 = reg3.observe("web", "杭州三日", {"tool": "web_search", "turn_id": "t1"})
    check("同步桥能跑通异步工具", res3.status == "ok", res3.status)
    check("同步桥把 what 当主参数传进去", tool.calls and tool.calls[0].get("query") == "杭州三日", str(tool.calls))

    slow = FakeAsyncTool(sleep=1.0)
    ok, out = builtin._run_async(lambda: slow.execute(query="x"), timeout_s=0.05)
    check("硬超时转成失败而不是抛异常", (not ok) and out.startswith("timeout:"), out)

    # ---- 5. 写本地：状态差异式回执 ----
    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        builtin._repo_root = lambda: Path(tmp)  # type: ignore[assignment]
        r1 = builtin.write_note_handler(what="杭州三日", params={"content": "第一天：西湖"})
        check("第一次写：有 changed", r1.status == "ok" and bool(r1.changed), repr(r1.changed))
        check("第一次写：before 为空（新建）", r1.state_before == "", r1.state_before)
        check("第一次写：after 非空", bool(r1.state_after), r1.state_after)
        target = Path(tmp) / "data/workspace/notes/杭州三日.md"
        check("文件真的落盘", target.exists(), str(target))

        r2 = builtin.write_note_handler(what="杭州三日", params={"content": "第一天：西湖"})
        check("内容没变：changed 为空（上层必须判 no_change）", r2.changed == {}, repr(r2.changed))
        check("内容没变：前后哈希相同", r2.state_before == r2.state_after and r2.state_before != "",
              f"{r2.state_before}/{r2.state_after}")

        r3 = builtin.write_note_handler(what="../逃逸/../../etc/passwd", params={"content": "x"})
        check("路径分隔符被抹平，出不去 notes 目录",
              (Path(tmp) / "data/workspace/notes") in Path(str(target)).parents and r3.status == "ok",
              r3.status)

        reg4 = builtin.build_registry({}, enable_writes=True)
        res4 = reg4.act("file", "write_note", {"tool": "write_note", "turn_id": "t1", "what": "x", "content": "y"})
        check("注册表调用写工具也带状态差异", res4.status == "ok" and bool(res4.changed), repr(res4.changed))

    # ---- 6. external_send 连 enable 都不许 ----
    reg5 = builtin.build_registry({})
    bad = ToolSpec(name="send_message_probe", family="msg", effects=frozenset({Effect.EXTERNAL_SEND}),
                   args={}, returns="x", owner="test", purpose="测试用")
    check("external_send 规格能登记", reg5.register(bad)[0], "")
    ok, reason = reg5.enable("send_message_probe")
    check("external_send 不许被启用", (not ok) and reason == "external_send_via_exit", reason)

    # ---- 7. bootstrap 绑进执行器 ----
    from core.jobs import executor

    registry = builtin.bootstrap({}, enable_writes=True)
    check("bootstrap 把注册表绑给执行器", executor.default_registry() is registry, "没绑上")

    print(f"RESULT {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
