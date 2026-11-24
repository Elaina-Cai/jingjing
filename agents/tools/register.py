"""把 `core.tools.declared` 里的 18 条声明 bind 到 agents 侧的实现。

为什么绑在这一侧：`scripts/check_layering.py` 规则 10 禁止 `core/tools/**` import agents，
而实现全在 `agents/tools/*`。所以「声明」在 core（数据），「实现与绑定」在 agents（代码），
中间靠这一层反向注册。

模型侧工具是 `async def execute(**kwargs)`，而 core 的 handler 是同步的
（`registry.observe/act` 直接调）。两种情形：

* 当前线程没有事件循环 → `asyncio.run(...)` 直接跑（jobs 执行器、脚本、测试属这类）；
* 当前线程已有事件循环（FastAPI/调度器主循环）→ **不硬塞**，返回
  `status="blocked"`、`reason="async_only"`，并记一行 `tool_bind_async_refused`。
  宁可不做，也不要在别人的 loop 里 `run_until_complete` 把整个服务卡死。

用法（组合根 core/bootstrap.py 里一行）：

    from agents.tools.register import bind_declared
    bind_declared()          # 幂等；失败只 log，不阻塞启动

返回值：`{"bound": [...], "skipped": {...name: reason}}`，便于自检与测试断言。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable
from safelog import warn

log = logging.getLogger(__name__)

BOUND_NAMES: list[str] = []
SKIPPED: dict[str, str] = {}


def _as_core_result(raw: Any, *, tool: str) -> Any:
    """agents 侧 ToolResult → core 侧 ToolResult（`core.tools.registry.ToolResult`）。"""
    from core.tools.registry import ToolResult as CoreResult

    output = str(getattr(raw, "output", raw) or "")
    error = str(getattr(raw, "error", "") or "")
    data = getattr(raw, "data", None)
    changed = data.get("changed") if isinstance(data, dict) else None
    return CoreResult(
        status="failed" if error else "ok",
        content=output or error[:200],
        source=f"agents:{tool}",
        changed=dict(changed) if isinstance(changed, dict) else {},
    )


def _run_coroutine(coro: Any) -> Any:
    """没有事件循环就直接跑；有就拒绝（不许把别人的 loop 卡死）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        warn("agents/tools/register.py:54 RuntimeError", err=repr(__import__("sys").exc_info()[1]))
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("async_only")


def _make_handler(name: str, tool: Any) -> Callable[..., Any]:
    def _handler(*, target: str = "", what: str = "", params: dict | None = None) -> Any:
        kwargs: dict[str, Any] = dict(params or {})
        if what and "text" not in kwargs and "query" not in kwargs and "content" not in kwargs:
            kwargs.setdefault("content", str(what))
        if target and "target" not in kwargs:
            kwargs.setdefault("target", str(target))
        call = getattr(tool, "execute", None)
        if call is None:
            raise RuntimeError("no_execute")
        raw = call(**kwargs)
        if asyncio.iscoroutine(raw):
            try:
                raw = _run_coroutine(raw)
            except RuntimeError as exc:
                if str(exc) != "async_only":
                    raise
                log.info("tool_bind_async_refused tool=%s", name)
                from core.tools.registry import ToolResult as CoreResult

                return CoreResult(status="blocked", content="async_only：当前线程有事件循环，不能同步调异步工具")
        return _as_core_result(raw, tool=name)

    return _handler


def bind_declared(registry: Any = None, *, tools: dict[str, Any] | None = None) -> dict[str, Any]:
    """把声明过的工具绑到实现。幂等：重复调用覆盖同一批名字。"""
    from core.tools.builtin import build_registry
    from core.tools.declared import declared_names

    registry = registry if registry is not None else build_registry()
    if tools is None:
        from agents.tools.base import ToolRegistry as ModelTools

        tools = dict(getattr(ModelTools.create_default(), "_tools", {}) or {})
    registry_names = {spec.name for spec in registry.specs()}
    bound: list[str] = []
    skipped: dict[str, str] = {}
    for name in sorted(declared_names()):
        tool = tools.get(name)
        if tool is None:
            skipped[name] = "no_impl"
            continue
        if name not in set(registry_names):
            skipped[name] = "not_declared"
            continue
        registry.bind(name, _make_handler(name, tool))
        _enable = getattr(registry, "enable", None)
        if callable(_enable):
            _enable(name)  # 绑上即可调：声明项默认未启用，不启用等于没绑
        bound.append(name)
    BOUND_NAMES[:] = bound
    SKIPPED.clear()
    SKIPPED.update(skipped)
    log.info("tool_bind_declared bound=%d skipped=%d", len(bound), len(skipped))
    return {"bound": bound, "skipped": skipped}


def verify_bound(registry: Any = None) -> list[str]:
    """回读一遍「声明项是否真的可调用」，给自检用。返回未绑上的名字。"""
    from core.tools.declared import declared_names

    registry = registry if registry is not None else None
    if registry is None:
        from core.tools.builtin import build_registry

        registry = build_registry()
    return [n for n in sorted(declared_names()) if registry.get(n) is None]
