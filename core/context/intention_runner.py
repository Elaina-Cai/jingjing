# -*- coding: utf-8 -*-
"""意图执行半环（Step 10/12）：到点执行、观察匹配、过期关闭。

中枢拥有意图；本模块只做"把 open 变 closed"的触发与回调，
不实现具体工具（工具由调用方注入 execute 回调，或在调度器侧接 reminders）。
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from core.context.goal_source import goal_source_state
from core.context.situation import (
    Intention,
    WorldModel,
    close_intention,
    due_intentions,
    open_intentions,
)
from safelog import warn

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "IntentionExecution",
    "IntentionRunResult",
    "expire_intentions",
    "match_observe",
    "run_due",
    "run_due_retryable",
]

#: 可重试失败的上限（与 ``scheduler.main.REMINDER_MAX_ATTEMPTS`` 同值；那边是权威
#: 止损线，这里只是"执行器谎报 retryable"时的兜底，防止意图永远 open）。
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class IntentionRunResult:
    key: str
    kind: str
    ok: bool
    result: str
    #: False = 可重试失败，意图仍 open，下一 tick 再来（Step 12）。
    closed: bool = True
    #: 执行事实的可引用 id（如 ``reminder:12``）；空串表示没有可引用证据。
    evidence_ref: str = ""


@dataclass(frozen=True)
class IntentionExecution:
    """一次到点执行的结论（调度侧执行器 -> 本模块的重试策略）。

    ``retryable`` 表示这次失败**不该关闭**意图：下一 tick 还会再试；
    ``attempts`` 是权威侧（``reminders.attempts``）记下的累计尝试次数，
    达到 ``max_attempts`` 时即使 ``retryable`` 也关闭，避免无限重试。
    ``evidence_ref`` 是这次执行留下的事实 id（到点行/动作事件），关闭时要引用它。
    """

    ok: bool
    result: str = ""
    retryable: bool = False
    attempts: int = 0
    evidence_ref: str = ""


def _close_or_local(
    model: WorldModel,
    key: str,
    at: str,
    result: str,
    evidence_ref: str,
    close: Callable[[str, str, str, str, str], bool] | None,
    state: str = "",
) -> None:
    """先走统一提交；写不进去才本地兜底。

    兜底不是双写：只在「执行已经发生、状态写失败」时用，目的是**防止下一 tick
    把已经做过的动作再做一遍**。兜底事件留在文件里，补偿任务可以再导入。
    """
    if close is not None and evidence_ref:
        try:
            if close(key, at, result, evidence_ref, state):
                return
        except Exception as exc:  # noqa: BLE001 - 提交失败退本地兜底
            warn("intention_close_callback_failed", err=repr(exc)[:120])
    from core.context.goal_close import close_local

    close_local(
        model, key, at=at, result=result, evidence_ref=evidence_ref, state=state
    )


def run_due(
    model: WorldModel,
    now: str,
    execute: Callable[[Intention], Any],
    *,
    close: Callable[[str, str, str, str, str], bool] | None = None,
) -> tuple[IntentionRunResult, ...]:
    """执行所有到点的延时意图；无论成功失败都关闭并回写结果。"""
    out: list[IntentionRunResult] = []
    for intent in due_intentions(model, now):
        if goal_source_state(intent, model.scope) == "dead":
            text = "来源请求已撤回，未执行"
            _close_or_local(
                model,
                intent.key,
                now,
                text,
                evidence_ref="",
                close=close,
                state="abandoned",
            )
            out.append(
                IntentionRunResult(
                    intent.key,
                    intent.kind,
                    False,
                    text,
                    True,
                    "",
                )
            )
            continue
        evidence_ref = ""
        try:
            result = execute(intent)
            if isinstance(result, IntentionExecution):
                evidence_ref = str(result.evidence_ref or "")
                result = result.result or ("ok" if result.ok else "failed")
            ok = True
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            warn("core/context/intention_runner.py:72 Exception", err=repr(exc))
            result = f"{type(exc).__name__}: {exc}"
            ok = False
        _close_or_local(
            model, intent.key, now, str(result), evidence_ref, close,
            state="completed" if ok else "abandoned",
        )
        out.append(IntentionRunResult(
            key=intent.key, kind=intent.kind, ok=ok, result=str(result),
            evidence_ref=evidence_ref,
        ))
    return tuple(out)


async def run_due_retryable(
    model: WorldModel,
    now: str,
    execute: Callable[[Intention], Any],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    close: Callable[[str, str, str, str, str], bool] | None = None,
    on_source_revoked: Callable[[Intention], Any] | None = None,
) -> tuple[IntentionRunResult, ...]:
    """到点执行的**调度器版**：execute 可以是协程，可重试失败保持 open。

    与 :func:`run_due` 的三点不同：

    1. ``execute`` 允许是 async 函数（调度器里投递要 await）；
    2. 执行器返回 :class:`IntentionExecution`：``retryable=True`` 且尝试次数
       未到上限时不关闭意图，留给下一 tick 重试；
    3. ``execute`` 抛异常按终局失败关闭（与 run_due 同口径）——真正可重试的
       失败应在执行器内部被翻译成 ``IntentionExecution(retryable=True)``。

    终局/耗尽上限时总是 ``close_intention``：事件流里不会留下永远 open 的意图。
    """
    out: list[IntentionRunResult] = []
    limit = max(1, int(max_attempts or DEFAULT_MAX_ATTEMPTS))
    for intent in due_intentions(model, now):
        if goal_source_state(intent, model.scope) == "dead":
            text = "来源请求已撤回，未执行"
            cancelled = True
            if on_source_revoked is not None:
                try:
                    verdict = on_source_revoked(intent)
                    if inspect.isawaitable(verdict):
                        verdict = await verdict
                    cancelled = verdict is not False
                except Exception as exc:  # noqa: BLE001 - 取消失败时继续占用，避免扫描器误发
                    warn(
                        "intention_source_cancel_failed",
                        key=intent.key,
                        err=repr(exc)[:120],
                    )
                    cancelled = False
            if not cancelled:
                out.append(
                    IntentionRunResult(
                        intent.key,
                        intent.kind,
                        False,
                        "来源已撤回，等待取消权威任务",
                        False,
                        "",
                    )
                )
                continue
            _close_or_local(
                model,
                intent.key,
                now,
                text,
                evidence_ref="",
                close=close,
                state="abandoned",
            )
            out.append(
                IntentionRunResult(
                    intent.key,
                    intent.kind,
                    False,
                    text,
                    True,
                    "",
                )
            )
            continue
        try:
            outcome = execute(intent)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            warn("core/context/intention_runner.py:108 Exception", err=repr(exc))
            outcome = IntentionExecution(
                ok=False, result=f"{type(exc).__name__}: {exc}", retryable=False,
            )
        if not isinstance(outcome, IntentionExecution):
            outcome = IntentionExecution(
                ok=bool(getattr(outcome, "ok", False)),
                result=str(getattr(outcome, "result", "") or ""),
                retryable=bool(getattr(outcome, "retryable", False)),
                attempts=int(getattr(outcome, "attempts", 0) or 0),
                evidence_ref=str(getattr(outcome, "evidence_ref", "") or ""),
            )
        evidence_ref = str(outcome.evidence_ref or "")
        text = str(outcome.result or ("ok" if outcome.ok else "failed"))
        if outcome.ok:
            _close_or_local(
                model, intent.key, now, text, evidence_ref, close, state="completed"
            )
            out.append(
                IntentionRunResult(intent.key, intent.kind, True, text, True, evidence_ref)
            )
            continue
        if outcome.retryable and outcome.attempts < limit:
            out.append(IntentionRunResult(
                intent.key, intent.kind, False,
                f"retry {outcome.attempts}/{limit}: {text}", False, evidence_ref,
            ))
            continue
        _close_or_local(
            model, intent.key, now, text, evidence_ref, close, state="abandoned"
        )
        out.append(
            IntentionRunResult(intent.key, intent.kind, False, text, True, evidence_ref)
        )
    return tuple(out)


def match_observe(
    model: WorldModel,
    *,
    text: str = "",
    speaker: str = "",
    at: str = "",
) -> tuple[str, ...]:
    """观察意图：condition 里的说话人回了话（或正文命中）→ 关闭。"""
    body = " ".join(str(text or "").split())
    who = str(speaker or "")
    closed: list[str] = []
    for intent in open_intentions(model, kind="observe"):
        condition = str(intent.condition or "")
        if not condition:
            continue
        hit = (who and condition == who) or (body and condition in body)
        if hit:
            close_intention(
                model, intent.key, at=at,
                result=f"matched:{who or body[:20]}",
            )
            closed.append(intent.key)
    return tuple(closed)


def expire_intentions(
    model: WorldModel,
    now: str,
    *,
    keys: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """显式过期一批意图（默认所有到点未完成的延时意图）。"""
    stamp = str(now or "")
    if not stamp:
        return ()
    wanted = set(keys)
    expired: list[str] = []
    for intent in open_intentions(model):
        if wanted and intent.key not in wanted:
            continue
        if wanted or (intent.due_at and intent.due_at <= stamp):
            close_intention(model, intent.key, at=stamp, result="expired（超时未完成）")
            expired.append(intent.key)
    return tuple(expired)
