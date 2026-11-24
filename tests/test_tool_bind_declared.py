"""B 尾：声明绑定到实现（agents 侧反向注册，不改 core 的 import 方向）。"""
from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.tools import register as R  # noqa: E402
from core.tools.builtin import build_registry  # noqa: E402
from core.tools.declared import declared_names  # noqa: E402

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name + " " + str(detail))


class SyncTool:
    async def execute(self, **kwargs):
        class _R:
            output = "ok"
            data = {"changed": {"hit": 1}}
            error = ""
        return _R()


class BoomTool:
    async def execute(self, **kwargs):
        raise RuntimeError("boom")


def t_bind():
    names = sorted(declared_names())
    registry = build_registry()
    out = R.bind_declared(registry, tools={n: SyncTool() for n in names})
    check("全部绑上", sorted(out["bound"]) == names, len(out["bound"]))
    check("无跳过", out["skipped"] == {}, out["skipped"])
    check("verify_bound 为空", R.verify_bound(registry) == [], R.verify_bound(registry))
    res = registry.observe("t", "hello", {"tool": names[0]})
    check("同步路径调到实现", "ok" in str(getattr(res, "content", "")), str(res)[:80])
    check("changed 透传", dict(getattr(res, "changed", {}) or {}) == {"hit": 1}, getattr(res, "changed", None))


def t_skip():
    out = R.bind_declared(build_registry(), tools={})
    check("没实现就跳过", not out["bound"] and set(out["skipped"].values()) == {"no_impl"})


def t_async_refused():
    names = sorted(declared_names())
    registry = build_registry()
    R.bind_declared(registry, tools={n: SyncTool() for n in names})

    async def _inside_loop():
        return str(getattr(registry.observe("t", "x", {"tool": names[0]}), "status", ""))

    check("有事件循环时拒绝而不是卡死", asyncio.run(_inside_loop()) == "blocked")


def t_boom():
    names = sorted(declared_names())
    registry = build_registry()
    R.bind_declared(registry, tools={n: BoomTool() for n in names})
    raised = False
    try:
        registry.observe("t", "x", {"tool": names[0]})
    except Exception:
        raised = True
    check("实现炸了会抛出（不静默）", raised)


def t_static():
    core_tools = "".join(p.read_text(encoding="utf-8-sig") for p in pathlib.Path("core/tools").glob("*.py"))
    check("core/tools 顶层不 import agents", "\nfrom agents" not in core_tools.replace("\r\n","\n"))
    reg = pathlib.Path("agents/tools/register.py").read_text(encoding="utf-8")
    check("register 里无硬编码工具名", "send_sticker" not in reg and "web_search" not in reg)
    boot = pathlib.Path("core/bootstrap.py").read_text(encoding="utf-8-sig")
    check("组合根真的绑了", "bind_declared()" in boot)


def main():
    for fn in (t_bind, t_skip, t_async_refused, t_boom, t_static):
        print(fn.__name__)
        fn()
    print("RESULT %d failed / %d checks" % (len(FAILS), COUNT))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
