"""工具清单进 prompt：`core/tools/prompt.py` + `agents/conversational.py::_tool_specs_block`。

对应 `docs/decisions/工具包.md` §5（注入与上限）与 U-09 切片。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features
from core.tools import builtin, prompt as tp
from core.tools.registry import TOOL_IN_PROMPT_MAX, Effect, ToolRegistry, ToolSpec


def _dummy(name, *, returns="某物", args=("x",), family="dummy", effect=Effect.READ):
    return ToolSpec(
        name=name,
        family=family,
        effects=frozenset({effect}),
        args={a: "str" for a in args},
        returns=returns,
        owner="tests/test_tool_prompt_block.py",
    )


def _registry(specs, *, enable=True):
    reg = ToolRegistry()
    for spec in specs:
        ok, reason = reg.register(spec)
        assert ok, reason
        if enable:
            reg.enable(spec.name)
    return reg


def main() -> int:
    failed = 0

    def check(name, cond, detail=""):
        nonlocal failed
        if not cond:
            failed += 1
        print(("PASS " if cond else "FAIL ") + name + ("" if not detail else "  :: " + str(detail)))

    # ---------- 渲染：形状与预算 ----------
    reg = _registry([_dummy("alpha"), _dummy("beta", returns="很长" * 80)])
    block = tp.build_tool_block(reg)
    lines = block.split("\n")
    check("有抬头", lines[0] == tp.TOOL_BLOCK_HEADER, lines[0])
    check("有收尾（一回合两件 + 失败说没查到）", tp.TOOL_BLOCK_FOOTER in block)
    check("每个工具一行", sum(1 for ln in lines if ln.startswith("- ")) == 2, len(lines))
    check("行首带工具名", "- alpha(x)" in block, block)
    check("单行不超过上限", all(len(ln) <= tp.TOOL_LINE_MAX_CHARS for ln in lines),
          max(len(ln) for ln in lines))
    check("整段不超过上限", len(block) <= tp.TOOL_BLOCK_MAX_CHARS, len(block))
    check("长返回被截断（带省略号）", "…" in block)
    check("渲染是纯函数（两次相同）", tp.render_tool_block(reg.select_for_prompt()) == tp.render_tool_block(reg.select_for_prompt()))

    many = _registry([_dummy(f"t{i}") for i in range(12)])
    block_many = tp.build_tool_block(many)
    check("最多 6 个工具", sum(1 for ln in block_many.split("\n") if ln.startswith("- ")) <= TOOL_IN_PROMPT_MAX,
          sum(1 for ln in block_many.split("\n") if ln.startswith("- ")))
    check("超预算也不超 700 字", len(block_many) <= tp.TOOL_BLOCK_MAX_CHARS, len(block_many))

    fat = _registry([_dummy(f"fat{i}", returns="很长的返回说明" * 40) for i in range(6)])
    block_fat = tp.build_tool_block(fat)
    # 断言改成「每行要么是抬头/收尾、要么是整行 - 开头」——footer 换行后会变成多行，
    # 旧写法把 footer 当单一整行比较，footer 一变长就假红（2026-10-21）。
    _foot_lines = set(tp.TOOL_BLOCK_FOOTER.split("\n"))
    check("行太长时整行丢弃（不会出现半行）",
          all(ln == tp.TOOL_BLOCK_HEADER or ln in _foot_lines or ln.startswith("- ")
              for ln in block_fat.split("\n")), block_fat[:80])
    check("整段仍不超预算", len(block_fat) <= tp.TOOL_BLOCK_MAX_CHARS, len(block_fat))

    # ---------- 空输入 ----------
    check("没有 schema → 空串", tp.render_tool_block([]) == "")
    check("注册表没有启用的工具 → 空串", tp.build_tool_block(_registry([_dummy("off1")], enable=False)) == "")
    check("没有注册表 → 空串", tp.build_tool_block(None) == "" if tp._default_registry() is None else True)
    check("limit=0 → 空串", tp.build_tool_block(reg, limit=0) == "")

    # ---------- 真注册表：四个只读工具 ----------
    real = builtin.bootstrap()
    real_block = tp.build_tool_block(real)
    check("真注册表能渲染", real_block.startswith(tp.TOOL_BLOCK_HEADER), real_block[:40])
    check("真注册表里带上 gui_snapshot", "gui_snapshot(" in real_block, real_block)
    check("真注册表整段不超预算", len(real_block) <= tp.TOOL_BLOCK_MAX_CHARS, len(real_block))
    check("只写注册表里没有 write_note（未启用）", "write_note(" not in real_block)

    # ---------- 开关三档 ----------
    sys.path.insert(0, str(ROOT / "agents"))
    from agents import conversational as conv

    saved = conv.__dict__.get("_saved_mode")
    real_mode = features.mode
    state = {"mode": features.MODE_OFF}
    features.mode = lambda name, default=features.MODE_SHADOW, **kw: state["mode"]
    tp_mode_holder = features.mode

    state["mode"] = features.MODE_OFF
    check("off 档不注入", conv._tool_specs_block() == "")
    state["mode"] = features.MODE_SHADOW
    check("shadow 档只留痕不注入", conv._tool_specs_block() == "")
    state["mode"] = features.MODE_ON
    on_block = conv._tool_specs_block()
    check("on 档注入", on_block.startswith(tp.TOOL_BLOCK_HEADER), on_block[:40])
    check("on 档内容与渲染器一致", on_block == real_block, (len(on_block), len(real_block)))
    features.mode = real_mode

    # ---------- 静态纪律 ----------
    src_conv = io.open(ROOT / "agents/conversational.py", encoding="utf-8").read()
    # 定义带注解（``(extra: Any = None)``），所以这里数的是**调用点**：只许一处，且必须带 extra。
    check("注入点只有一处", src_conv.count("_tool_specs_block(extra)") == 1
          and "prompt, _tool_specs_block(extra)" in src_conv,
          str(src_conv.count("_tool_specs_block(extra)")))
    check("注入点在 _job_readout_block 之后（顺序稳定）",
          src_conv.index("_job_readout_block(extra)") < src_conv.index("prompt, _tool_specs_block(extra)"))
    check("helpers 定义在 system 拼装之前", src_conv.index("def _tool_specs_block") < src_conv.index("def _build_system_prompt_from_snapshot"))
    src_tp = io.open(ROOT / "core/tools/prompt.py", encoding="utf-8").read()
    check("prompt.py 不 import 出口/路由/agents",
          "core.speech" not in src_tp and "core.routing" not in src_tp and "from agents" not in src_tp)
    check("注册表默认 off（行为改动必须能退回）",
          features.FLAG_REGISTRY[features.TOOL_PACKAGE_PROMPT].default == features.MODE_OFF)

    total = 30
    print(f"RESULT {failed} failed / {total - failed} checked")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
