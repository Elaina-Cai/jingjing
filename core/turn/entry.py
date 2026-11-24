"""唯一回合入口（M0：只有骨架，呼叫不做任何事）。

纪律：
1. 九步按序；某步抛异常 -> 该步记 error、**后面不再跑**（fail-closed），
   但 `finish` 一定跑（释放资源）。
2. 没注册处理器的步骤记 `skipped / not_wired`，不是错误——M0 就是空架子。
3. 入口不做业务判断：判断在 `judge` 步，说话在 `say` 步。
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from core.turn import trace as _trace
from core.turn.steps import STEP_NAMES, is_step

log = logging.getLogger(__name__)

Handler = Callable[..., Any]
HANDLERS: dict[str, Handler] = {}


@dataclass(frozen=True)
class TurnResult:
    """一次回合的结果（M0 只报走了哪几步）。"""

    chat_id: str = ""
    turn_id: str = ""
    status: str = "ok"
    done: tuple[str, ...] = ()
    failed: str = ""
    note: str = ""
    ms: int = 0


def register(name: str, handler: Handler, *, replace: bool = False) -> None:
    """注册某一步的处理器（同名重复注册要显式 replace）。"""
    if not is_step(name):
        raise ValueError(f"不是九步之一：{name!r}")
    if not callable(handler):
        raise TypeError("handler 必须可调用")
    if name in HANDLERS and not replace:
        raise ValueError(f"{name} 已经有处理器，替换请 replace=True")
    HANDLERS[name] = handler


def unregister(name: str) -> None:
    """摘掉某一步的处理器（测试与 M0 收尾用）。"""
    HANDLERS.pop(name, None)


def reset() -> None:
    """清空全部处理器。"""
    HANDLERS.clear()


def handlers() -> dict[str, Handler]:
    """当前注册表（副本）。"""
    return dict(HANDLERS)


def _ids(ctx: Any) -> tuple[str, str]:
    def pick(key: str) -> str:
        if isinstance(ctx, dict):
            return str(ctx.get(key) or "")
        return str(getattr(ctx, key, "") or "")

    return pick("chat_id"), pick("turn_id")


async def _call(handler: Handler, ctx: Any) -> None:
    out = handler(ctx)
    if inspect.isawaitable(out):
        await out


async def run_turn(ctx: Any, *, handlers_map: dict[str, Handler] | None = None) -> TurnResult:
    """按九步跑一个回合。M0：几乎什么都不做，只留痕。"""
    started = time.monotonic()
    chat_id, turn_id = _ids(ctx)
    tr = _trace.begin(chat_id=chat_id, turn_id=turn_id)
    table = dict(HANDLERS if handlers_map is None else handlers_map)
    done: list[str] = []
    failed = ""
    status = "ok"
    note = ""

    try:
        for name in STEP_NAMES:
            step_started = time.monotonic()
            handler = table.get(name)
            if handler is None:
                tr.step(name, status="skipped", note="not_wired")
                done.append(name)
                continue
            try:
                await _call(handler, ctx)
            except Exception as exc:  # fail-closed：这一步坏了就不再往下走
                cost = int((time.monotonic() - step_started) * 1000)
                tr.step(name, status="error", note=_trace.safe_note(exc), ms=cost)
                failed = name
                status = "error"
                note = _trace.safe_note(exc)
                log.warning("turn_step_failed step=%s error=%s", name, _trace.safe_note(exc))
                break
            cost = int((time.monotonic() - step_started) * 1000)
            tr.step(name, ms=cost)
            done.append(name)
        else:
            pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）
    finally:
        if "finish" not in done:
            tr.step("finish", status="ok", note="always")
            done.append("finish")
        _trace.end()

    return TurnResult(
        chat_id=chat_id,
        turn_id=turn_id,
        status=status,
        done=tuple(done),
        failed=failed,
        note=note,
        ms=int((time.monotonic() - started) * 1000),
    )


__all__ = [
    "Handler",
    "TurnResult",
    "handlers",
    "register",
    "reset",
    "run_turn",
    "unregister",
]
