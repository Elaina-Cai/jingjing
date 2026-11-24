"""U-18 回合内工具调用闭环：解析 → 调 → 进留痕与下一轮。"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.tools import turnloop as t  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
        return
    print(f"FAIL {name} :: {detail}")
    FAILED.append(name)


class FakeSpec:
    def __init__(self, name: str, args: dict | None = None) -> None:
        self.name = name
        self.args = {"query": "str"} if args is None else args


class FakeRegistry:
    def __init__(self, specs: dict | None = None, status: str = "ok", raise_on: str = "") -> None:
        self.specs = {
            "web_search": FakeSpec("web_search"),
            "gui_state": FakeSpec("gui_state", {"target": "str"}),
        }
        if specs:
            self.specs.update(specs)
        self.calls: list[tuple[str, str, dict]] = []
        self.status = status
        self.raise_on = raise_on

    def get(self, name):
        return self.specs.get(name)

    def observe(self, target, what, params=None):
        name = str((params or {}).get("tool") or "")
        self.calls.append((name, what, dict(params or {})))
        if name and name == self.raise_on:
            raise RuntimeError("boom")
        return SimpleNamespace(
            status=self.status,
            content=f"{name} 的结果",
            source="https://example.com",
            truncated=False,
        )


class MP:
    def __init__(self) -> None:
        self._saved: list[tuple[object, str, object]] = []

    def set(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self) -> None:
        for obj, name, value in reversed(self._saved):
            setattr(obj, name, value)
        self._saved.clear()


def main() -> int:
    t.reset()

    # ── 解析 ────────────────────────────────────────────────
    ok_payload = {
        "decision": "respond",
        "tools": [
            {"name": "web_search", "purpose": "行程看天气", "args": {"query": "杭州天气"}},
            {"name": "gui_state", "purpose": "看一眼页面"},
        ],
    }
    calls = t.parse_tool_calls(ok_payload)
    check("解析：两条", len(calls) == 2, str(calls))
    check("解析：第一条字段", calls[0].name == "web_search" and calls[0].purpose == "行程看天气"
          and calls[0].args == {"query": "杭州天气"}, str(calls[0]))
    check("解析：第二条 args 缺省为空", dict(calls[1].args) == {}, str(calls[1]))

    three = dict(ok_payload, tools=ok_payload["tools"] + [
        {"name": "fetch_url", "purpose": "看原文", "args": {"url": "https://x"}},
    ])
    check("解析：最多两条（第三条丢）", len(t.parse_tool_calls(three)) == 2,
          str(len(t.parse_tool_calls(three))))

    check("解析：缺 purpose 丢掉", t.parse_tool_calls(
        {"tools": [{"name": "web_search", "args": {}}]}) == (),
        str(t.parse_tool_calls({"tools": [{"name": "web_search", "args": {}}]})))
    check("解析：purpose 超 20 字丢掉", t.parse_tool_calls(
        {"tools": [{"name": "web_search", "purpose": "一" * 21}]}) == ())
    check("解析：purpose 恰好 20 字保留", len(t.parse_tool_calls(
        {"tools": [{"name": "web_search", "purpose": "一" * 20}]})) == 1)
    check("解析：args 不是 mapping 丢掉", t.parse_tool_calls(
        {"tools": [{"name": "web_search", "purpose": "查", "args": "q=1"}]}) == ())
    check("解析：tools 是字符串不算", t.parse_tool_calls({"tools": "web_search"}) == ())
    check("解析：没有 tools 键", t.parse_tool_calls({"decision": "respond"}) == ())
    check("解析：载荷不是 mapping", t.parse_tool_calls(None) == ())

    # ── 排块 ────────────────────────────────────────────────
    outs = (
        t.ToolOutcome("web_search", "行程看天气", "ok", "杭州周六多云", "https://x"),
        t.ToolOutcome("gui_state", "看一眼页面", "ui_blocked", ""),
    )
    block = t.render_results(outs)
    check("排块：抬头", block.startswith("[工具结果]\n"), repr(block[:20]))
    check("排块：有状态与正文", "web_search(行程看天气): ok | 杭州周六多云" in block, block)
    check("排块：失败也写一行", "gui_state(看一眼页面): ui_blocked" in block, block)
    check("排块：收尾", block.endswith("（查不到就说没查到，不许编）"), repr(block[-30:]))
    check("排块：空结果 -> 空串", t.render_results(()) == "")

    long_body = t.render_results((t.ToolOutcome("web_search", "查", "ok", "字" * 900),))
    check("排块：单条正文截到 600（末尾是省略号）",
          "字" * 599 in long_body and "字" * 601 not in long_body and long_body.count("…") == 1,
          str(len(long_body)))
    many = tuple(t.ToolOutcome(f"tool{i}", "查", "ok", "字" * 600) for i in range(6))
    tight = t.render_results(many, max_chars=800)
    check("排块：放不下整行丢弃（不留半行）", tight.count("\n- ") <= 1, repr(tight[:120]))
    check("排块：整块不超预算", len(tight) <= 800, str(len(tight)))

    # ── 调工具 ──────────────────────────────────────────────
    reg = FakeRegistry()
    got = t.run_calls(calls, registry=reg, turn_id="t1")
    check("调：两条都跑", len(got) == 2 and len(reg.calls) == 2, str(reg.calls))
    check("调：what 用 purpose", reg.calls[0][1] == "行程看天气", str(reg.calls[0]))
    check("调：params 带 tool 与 turn_id",
          reg.calls[0][2].get("tool") == "web_search" and reg.calls[0][2].get("turn_id") == "t1",
          str(reg.calls[0][2]))
    check("调：结果字段透传", got[0].status == "ok" and got[0].source == "https://example.com",
          str(got[0]))

    unknown = t.run_calls((t.ToolCall("nope", "查", {}),), registry=reg)
    check("调：未注册工具", unknown[0].status == "unknown_tool", str(unknown[0]))
    bad = t.run_calls((t.ToolCall("gui_state", "查", {"weird": 1}),), registry=reg)
    check("调：多余参数", bad[0].status == "bad_args", str(bad[0]))
    none_ = t.run_calls((t.ToolCall("web_search", "查", {}),), registry=None)
    check("调：没有注册表", none_[0].status == "not_enabled", str(none_[0]))
    boom = t.run_calls((t.ToolCall("web_search", "查", {}),),
                       registry=FakeRegistry(raise_on="web_search"))
    check("调：工具炸了算失败", boom[0].status == "failed", str(boom[0]))
    over = t.run_calls((t.ToolCall("web_search", "查", {}),) * 3, registry=reg)
    check("调：最多两条（第三条不跑）", len(over) == 2, str(len(over)))

    # ── 三档 ────────────────────────────────────────────────
    mp = MP()
    try:
        mp.set(t, "loop_mode", lambda: features.MODE_OFF)
        before = len(reg.calls)
        r_off = t.handle_verdict(ok_payload, turn_id="t1", chat_id="c1", registry=reg)
        check("off：什么都不做", r_off.calls == () and len(reg.calls) == before, str(r_off))
        check("off：信箱空", t.consume_block("c1") == "")

        mp.set(t, "loop_mode", lambda: features.MODE_SHADOW)
        before = len(reg.calls)
        r_sh = t.handle_verdict(ok_payload, turn_id="t1", chat_id="c1", registry=reg)
        check("shadow：解析了", [c.name for c in r_sh.calls] == ["web_search", "gui_state"],
              str(r_sh.calls))
        check("shadow：一个工具都不调", len(reg.calls) == before and r_sh.outcomes == (),
              str(len(reg.calls) - before))
        check("shadow：信箱空", t.consume_block("c1") == "")

        mp.set(t, "loop_mode", lambda: features.MODE_ON)
        t.reset()
        traced: list[dict] = []
        import core.terminal.turn_trace as trace

        mp.set(trace, "record_full_detail", lambda **kw: traced.append(kw) or True)
        r_on = t.handle_verdict(ok_payload, turn_id="t9", chat_id="c9", registry=reg)
        check("on：真调了", len(r_on.outcomes) == 2, str(r_on.outcomes))
        check("on：块排出来", r_on.block.startswith("[工具结果]"), repr(r_on.block[:15]))
        check("on：留痕写了 tools", traced and traced[0]["detail"]["tools"][0]["name"] == "web_search",
              str(traced))
        check("on：留痕 turn_id", traced and traced[0]["turn_id"] == "t9", str(traced[:1]))
        blk = t.consume_block("c9")
        check("on：下一轮取到块", blk == r_on.block, repr(blk[:20]))
        check("on：取一次就没了", t.consume_block("c9") == "")
        check("on：会话兜底也取不到", t.consume_block() == "")
    finally:
        mp.undo()
        t.reset()

    # ── 静态纪律 ────────────────────────────────────────────
    src = io.open(ROOT / "core/tools/turnloop.py", encoding="utf-8").read()
    for bad_import in ("core.speech", "core.routing", "from agents", "core.jobs.runner"):
        check(f"静态：不 import {bad_import}", bad_import not in src, bad_import)
    check("静态：不自己调 check_call（记账在 observe 里，一次调用只算一次）",
          "check_call(" not in src)
    check("静态：默认三态", str(features.FLAG_REGISTRY[features.TOOL_LOOP].default) == "off",
          str(features.FLAG_REGISTRY[features.TOOL_LOOP]))

    conv = io.open(ROOT / "agents/conversational.py", encoding="utf-8").read()
    check("静态：判决落地调 handle_verdict",
          conv.count("import handle_verdict") == 1
          and conv.count("handle_verdict(payload") == 1,
          f"import={conv.count('import handle_verdict')} call={conv.count('handle_verdict(payload')}")
    check("静态：提示词注入点存在", "_tool_results_block(extra)" in conv)
    check("静态：注入顺序在工具清单之后",
          conv.index("prompt, _tool_specs_block(extra)")
          < conv.index("prompt, _tool_results_block(extra)"))

    print(f"RESULT {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
