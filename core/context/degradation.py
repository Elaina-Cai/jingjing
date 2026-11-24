# -*- coding: utf-8 -*-
"""模型输出降级账本（批D 路线 5）：把「模型输出不可用 -> 走了哪条降级」记成一条可收数的审计。

边界：
* 只审计、不改行为：各调用点的降级逻辑保持原样（本模块不参与判定）；
* off = 一行不走；shadow/on 都只记 ``llm_degradation``（on 留给未来：降级表可驱动熔断/重试）；
* 纯函数 + 一次 log；不读库、不调 LLM、不 import 业务模块。
"""
from __future__ import annotations

import structlog
from safelog import warn

log = structlog.get_logger(__name__)

__all__ = ["degradation_mode", "note_degradation", "STAGES", "FAILURES", "FALLBACKS"]

#: 会产出模型的阶段（收数分桶用；新阶段先加这里，别写自由文本）。
STAGES = ("llm", "planner", "n1", "admission", "scene_read", "render", "classify", "unknown")
#: 失败类型（结构不可用 / 降级 / 熔断）。
FAILURES = ("json_parse", "invalid_steps", "section_missing", "degraded", "schema", "circuit_open", "timeout", "unknown")
#: 实际走的降级路径。
FALLBACKS = ("caller_fallback", "clarify_plan", "standalone", "merged_read", "provider_fallback", "noop", "keep", "unknown")


def degradation_mode() -> str:
    """``YINYUE_FEATURE_LLM_DEGRADATION`` 三态；读不到按 off。"""
    try:
        from core import features

        return features.mode(features.LLM_DEGRADATION, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到按 off，不阻断
        warn("core/context/degradation.py:31 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def note_degradation(
    *,
    stage: str,
    failure: str,
    fallback: str = "unknown",
    chat_id: str = "",
    detail: str = "",
) -> None:
    """记一条降级审计；off 直接返回（零副作用）。"""
    if degradation_mode() == "off":
        return
    try:
        log.info(
            "llm_degradation",
            stage=str(stage or "unknown"),
            failure=str(failure or "unknown"),
            fallback=str(fallback or "unknown"),
            chat_id=str(chat_id or ""),
            detail=str(detail or "")[:160],
        )
    except Exception:  # noqa: BLE001 - 审计失败绝不拖垮调用点
        warn("core/context/degradation.py:55 Exception", err=repr(__import__("sys").exc_info()[1]))
        return
