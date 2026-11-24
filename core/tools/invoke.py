"""工具调用的唯一入口：check_call -> 按 effects 选 observe/act -> 一行日志。

为什么要有这一层：真机里「工具能登记、清单能进 prompt，但没人真调」曾经同时存在
两条调用路径（模型侧 `agents/tools` 与执行器侧 `core/tools`）。新代码只许走这里，
一行日志回答「调了哪个工具、什么效果、成没成、花了多久」。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

log = logging.getLogger(__name__)

WRITE_EFFECTS = frozenset({"write_local", "ui_control", "external_send"})


def _effects(registry: Any, name: str) -> frozenset[str]:
    # 查不到 spec 就 fail closed（真 registry 的 get 对已注册工具不会失败）
    spec = registry.get(name)
    raw = getattr(spec, "effects", ()) or ()
    if isinstance(raw, str):
        raw = (raw,)
    out = set()
    for item in raw:
        key = str(getattr(item, "value", item) or "").strip()
        if key:
            out.add(key)
    return frozenset(out)


def invoke(
    registry: Any,
    name: str,
    params: Mapping[str, Any] | None = None,
    *,
    turn_id: str = "",
    target: str = "",
    mode: str = "auto",
) -> Any:
    """唯一调用点：读效果走 observe()，写效果走 act()；mode 可强制 observe/act。"""
    params = dict(params or {})
    started = time.monotonic()
    effects = _effects(registry, name)
    use_act = mode == "act" or (mode != "observe" and bool(effects & WRITE_EFFECTS))
    try:
        if use_act:
            result = registry.act(name, params, params)
        else:
            result = registry.observe(name, params, params)
    except Exception as exc:
        cost = int((time.monotonic() - started) * 1000)
        log.warning(
            "tool_invoke_failed name=%s effects=%s turn=%s error=%s ms=%d",
            name, ",".join(sorted(effects)) or "-", turn_id or "-", type(exc).__name__, cost,
        )
        raise
    cost = int((time.monotonic() - started) * 1000)
    status = str(getattr(result, "status", "") or "-")
    log.info(
        "tool_invoke name=%s effects=%s via=%s status=%s turn=%s target=%s ms=%d",
        name,
        ",".join(sorted(effects)) or "-",
        "act" if use_act else "observe",
        status,
        turn_id or "-",
        target or "-",
        cost,
    )
    return result


def catalog_names() -> frozenset[str]:
    """执行器侧目录里现在有哪些工具（给影子对照用；导入失败返回空集）。"""
    try:
        from core.tools.builtin import build_registry

        registry = build_registry()
        return frozenset(spec.name for spec in registry.specs())
    except Exception as exc:
        log.info("tool_catalog_names_failed error=%s", exc)
        return frozenset()


def covered_by_catalog(name: str) -> bool:
    """这个工具名在统一目录里有没有实现（模型侧与执行器侧合并的进度尺）。"""
    return str(name or "").strip() in catalog_names()
