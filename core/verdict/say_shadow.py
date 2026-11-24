"""M2 第一小步（影子）：每回合多一次极小调用，把 Reading 填出来，与旧判据对照。

为什么是影子：矩阵要接管「说不说」，但先要证明它与旧判据的差异是**可解释的**。
所以本模块只做三件事：问一次 → 解析 → 对照 → 记一行 `say_matrix_shadow`，绝不改变行为。

开关：`YINYUE_SAY_MATRIX_SHADOW`（默认 off：默认不花这笔钱）。
"""
from __future__ import annotations

import logging
import os

from core.verdict import matrix, shadow
from core.verdict.reading import build_reading_prompt, parse_reading
from core.infra.envflags import flag

log = logging.getLogger(__name__)

ENV = "YINYUE_SAY_MATRIX_SHADOW"


def shadow_mode() -> bool:
    return flag(ENV)


async def shadow_reading(
    *,
    observation,
    send,
    chat_id: str = "",
    old_intent: str = "",
    old_would_reply: bool | None = None,
    where: str = "scheduler",
    msg_id: str = "",
    text: str = "",
    capability: bool = True,
) -> dict:
    """问一次阅读 → 矩阵 → 与旧判据对照。返回 `{ok, disposition, reason, disagreement}`。"""
    if not shadow_mode():
        return {"ok": False, "skipped": "mode_off"}
    if send is None or observation is None:
        return {"ok": False, "skipped": "no_input"}
    try:
        messages = build_reading_prompt(observation)
        payload = await send(messages)
    except Exception as exc:  # noqa: BLE001 —— 影子绝不许拖垮回合
        log.info("say_matrix_shadow_failed chat_id=%s error=%r", chat_id, exc)
        return {"ok": False, "skipped": "send_failed"}
    reading = parse_reading(payload)
    if reading is None:
        log.info("say_matrix_shadow_unparsed chat_id=%s payload=%r", chat_id, str(payload)[:120])
        return {"ok": False, "skipped": "unparsed"}
    decision = matrix.decide(reading, capability=capability)
    levels = shadow.compare_levels(
        old_would_reply=old_would_reply,
        decision=decision,
        chat_id=chat_id,
        where=where,
        msg_id=msg_id,
        text=text,
    )
    log.info(
        "say_matrix_shadow chat_id=%s where=%s msg_id=%s text=%s disposition=%s reason=%s old_intent=%s relation=%s",
        chat_id,
        where,
        msg_id or "-",
        (text or "")[:40].replace("\n", " "),
        decision.disposition,
        decision.reason,
        old_intent,
        levels["relation"],
    )
    return {"ok": True, "disposition": decision.disposition, "reason": decision.reason,
            "relation": levels["relation"]}
