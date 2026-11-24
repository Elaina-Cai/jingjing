"""触发路径的一个读数：触发原因（S1）。

为什么单独一层
--------------
``core/routing/turn_pipeline.py::_generate_trigger_turn`` 有一段**只依赖入参**的搬运：
把路由段 / 装配器单元拼成 ``trigger_reasons``。它不读环境、不发日志、不做 IO；
搬出来之后，函数体剩下的只有「什么时候调谁」。

轻拍的折叠
----------
宽限窗口里挂起的轻拍并进**已经发生的这一回合**（他刚点名 / 刚说了内容，轻拍只是「他被
呼唤」的证据），不另开一回合；复合形态进 trace。装配器交出来的单元自带复合 reason，
那种情况不再折老路。
"""
from __future__ import annotations

from typing import Any

from core.routing.turn_decision import ROUTE_SEGMENT_MENTION
from safelog import warn

__all__ = [
    "summons_of",
    "trigger_reasons_of",
]


def summons_of(question: Any) -> tuple[str, ...]:
    """单元自带的呼唤形式集合（``summons``）；缺字段给空元组。"""
    return tuple(getattr(question, "summons", ()) or ())


def trigger_reasons_of(
    decision: Any,
    question: Any,
    route: str,
    state: Any,
    unit_summons: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    """返回 ``(trigger_reason, trigger_reasons)``。

    ``unit_summons`` 由调用方传入（同一回合里别处也要用它，
    重复解析一遍没有意义）。

    ``trigger_reason`` 是**一个**字符串（可能是复合形态 ``a+b``），``trigger_reasons``
    是拆开后的元组——两者都进 trace，口径必须一致。

    单元口径优先：装配器交出来的单元自带 ``trigger_reason`` 与 ``summons``（呼唤形式
    集合），比路由段更准（一次轻拍 + 一句话是一个 ambient 单元，但 reason 必须记复合
    形态）。
    """
    trigger_reason = route or str(getattr(decision, "reason", "") or "")
    unit_reason = str(getattr(question, "trigger_reason", "") or "")
    if unit_reason:
        trigger_reason = unit_reason
    trigger_reasons: tuple[str, ...] = (trigger_reason,)
    # 只有点名那一路折宽限轻拍：路由段本身就是 canonical 的 ``patted_in_group``
    # （``core.speech.resolver`` 的 GROUP_PAT 受众、N1 的强指向判据都读它），不需要把
    # 旧词 ``pat`` 再改写一遍。折不动就退回现状——这是审计字段，不该把回合炸掉。
    if route == ROUTE_SEGMENT_MENTION and not unit_summons:
        try:
            folded = state.take_pending_pats(trigger_reason)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 折不动就退回现状
            warn("core/routing/trigger_reading.py:62 Exception", err=repr(__import__("sys").exc_info()[1]))
            folded = trigger_reason
        if folded != trigger_reason:
            trigger_reason = folded
            trigger_reasons = tuple(folded.split("+"))
    if len(trigger_reasons) == 1 and "+" in trigger_reason:
        trigger_reasons = tuple(trigger_reason.split("+"))
    return trigger_reason, trigger_reasons
