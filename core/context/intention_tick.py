# -*- coding: utf-8 -*-
"""意图调度 tick（Step 12 / 交接 §5 TODO 1）：按时间把到点意图接到执行器。

**唯一读 ``INTENTION_TICK`` 三态 flag 的地方**（用 :func:`core.features.mode`，别用
``enabled``——它会把 shadow 读成 False）：

* ``off``    ← 默认：一行都不走，与接线前**逐字节一致**（回滚位）；
* ``shadow`` → 只枚举到点意图 + 记 ``intention_tick_shadow``，不执行、不改状态；
* ``on``     → 每个会话调 ``run_due_retryable``：成功关闭、可重试失败留 open、
               到止损线关闭；执行器由 scheduler 注入（reminders / work_items）。

本模块不 import scheduler，只依赖中枢与执行半环，便于单测注入假执行器。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

import structlog

from core import features
from core.context.intention_runner import run_due_retryable
from core.context.situation import Intention, WorldModel, due_intentions
from safelog import warn

log = structlog.get_logger()

__all__ = ["TickReport", "run_tick", "tick_mode", "tick_now_text"]

TickExecutor = Callable[[str, Intention], Any]


@dataclass(frozen=True)
class TickReport:
    mode: str = features.MODE_OFF
    chats: int = 0          # 已知世界模型数
    chats_due: int = 0      # 其中有到点意图的会话数
    due: int = 0            # 到点意图总数
    executed: int = 0       # 执行成功
    retried: int = 0        # 可重试失败，仍 open
    closed: int = 0         # 已关闭（成功 + 终局失败）
    failed: int = 0         # 终局失败（closed 且 not ok）


def tick_mode() -> str:
    """三态读点；调度 job 每轮都读一次（免重启开关）。"""
    return features.mode(features.INTENTION_TICK, features.MODE_OFF)


def tick_now_text(now: datetime | None = None) -> str:
    """与 ``reminder_due_text`` / ``due_intentions`` 同一口径的分钟时间戳。"""
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M")


async def run_tick(
    execute: TickExecutor,
    *,
    now: str = "",
    models: Mapping[str, WorldModel] | None = None,
    on_source_revoked: Callable[[Intention], Any] | None = None,
) -> TickReport:
    """跑一轮 tick；``execute(chat_id, intention)`` 同步/异步均可。"""
    mode = tick_mode()
    if mode == features.MODE_OFF:
        return TickReport(mode=mode)
    if models is None:
        from core.execution.data_channels import known_worlds

        models = known_worlds()
    stamp = str(now or "") or tick_now_text()
    chats = len(models)
    chats_due = due_total = 0
    executed = retried = closed = failed = 0
    for chat_id, model in models.items():
        due = due_intentions(model, stamp)
        if not due:
            continue
        chats_due += 1
        due_total += len(due)
        if mode == features.MODE_SHADOW:
            log.info(
                "intention_tick_shadow",
                chat_id=str(chat_id),
                due=len(due),
                keys=[item.key for item in due],
                kinds=[item.kind for item in due],
            )
            continue
        def _close(
            key: str, at: str, result: str, evidence_ref: str, state: str = ""
        ) -> bool:
            """执行侧关闭：先写认知库；写不进去由 runner 本地兜底（防重放）。"""
            from core.context.goal_close import commit_close

            return commit_close(
                model, key, at=at, result=result,
                evidence_ref=evidence_ref, mark=True, state=state,
            )

        results = await run_due_retryable(
            model, stamp, lambda intent, cid=str(model.scope.get("chat_id") or chat_id): execute(cid, intent),
            close=_close,
            on_source_revoked=on_source_revoked,
        )
        ok_n = sum(1 for item in results if item.ok)
        retry_n = sum(1 for item in results if not item.closed)
        closed_n = sum(1 for item in results if item.closed)
        executed += ok_n
        retried += retry_n
        closed += closed_n
        failed += closed_n - ok_n
        try:
            from core.execution.data_channels import save_world

            save_world(chat_id, scope=model.scope)
        except Exception:  # noqa: BLE001 - 落盘 best-effort，不阻断 tick
            warn("core/context/intention_tick.py:102 Exception", err=repr(__import__("sys").exc_info()[1]))
        log.info(
            "intention_tick",
            chat_id=str(chat_id),
            due=len(due),
            executed=ok_n,
            retried=retry_n,
            closed=closed_n,
            failed=closed_n - ok_n,
        )
    if due_total:
        log.info(
            "intention_tick_done",
            mode=mode,
            chats=chats,
            chats_due=chats_due,
            due=due_total,
            executed=executed,
            retried=retried,
            closed=closed,
            failed=failed,
        )
    return TickReport(
        mode=mode,
        chats=chats,
        chats_due=chats_due,
        due=due_total,
        executed=executed,
        retried=retried,
        closed=closed,
        failed=failed,
    )
