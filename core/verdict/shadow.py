"""影子对照：旧意图 vs 新矩阵，只记一行日志，不接管任何出口。"""

from __future__ import annotations

import logging

from core.verdict.matrix import ABSTAIN, ASK, DO, SAY, Decision

log = logging.getLogger(__name__)

# 旧意图词 -> 新处置（用于「算不算一致」，不是行为映射）
OLD_TO_DISPOSITION = {
    "闲聊": SAY, "陪伴": SAY, "安慰": SAY,
    "提问": SAY, "问事": SAY, "求建议": SAY,
    "指令": DO, "办事": DO, "代办": DO,
    "澄清": ASK, "不确定": ASK,
    "拒答": ABSTAIN, "弃权": ABSTAIN,
}


def map_old(old_intent: str) -> str:
    """旧意图 -> 新处置（认不出返空串）。"""
    key = str(old_intent or "").strip()
    return OLD_TO_DISPOSITION.get(key, "")


def compare(
    *,
    old_intent: str,
    decision: Decision,
    chat_id: str = "",
    where: str = "",
    old_would_reply: bool | None = None,
) -> bool:
    """记一行对照，返回两者是否一致（认不出旧意图也算不一致）。"""
    mapped = map_old(old_intent)
    agree = bool(mapped) and mapped == decision.disposition
    if old_would_reply is False and decision.disposition != ABSTAIN:
        agree = False
    log.info(
        "verdict_shadow old=%s old_disposition=%s new=%s reason=%s agree=%s chat_id=%s where=%s",
        str(old_intent or "-"),
        mapped or "-",
        decision.disposition,
        decision.reason,
        "1" if agree else "0",
        chat_id or "-",
        where or "-",
    )
    return agree


def compare_levels(
    *,
    old_would_reply: bool | None,
    decision: Decision,
    chat_id: str = "",
    where: str = "",
    msg_id: str = "",
    text: str = "",
) -> dict:
    """按「说/不说、说到什么程度」比较新旧两侧，返回 {old_level,new_level,relation}。

    为什么不用原来的 agree/disagree：旧判据能走到这里就说明它已经决定要回，
    于是 agree 永远是 False（旧意图词表也对不上）——那种指标跑七天也不会有信息量。

    relation 四值：
      same   旧说 → 矩阵也说（SAY）
      finer  旧说 → 矩阵要问一句 / 要动手（更细，信息更多但多一轮）
      silent 旧说 → 矩阵认为不该说（会丢一条回复，最需要人看）
      loud   旧不说 → 矩阵要说（会多嘴，最需要人看）
    """
    spoke = bool(old_would_reply)
    old_level = SAY if spoke else ABSTAIN
    new_level = decision.disposition
    if spoke and new_level == SAY:
        relation = "same"
    elif spoke and new_level in (ASK, DO):
        relation = "finer"
    elif spoke and new_level == ABSTAIN:
        relation = "silent"
    elif (not spoke) and new_level != ABSTAIN:
        relation = "loud"
    else:
        relation = "same"
    log.info(
        "verdict_shadow old_level=%s new_level=%s relation=%s reason=%s chat_id=%s where=%s msg_id=%s text=%s",
        old_level, new_level, relation, decision.reason,
        chat_id or "-", where or "-", msg_id or "-", (text or "")[:40].replace("\n", " "),
    )
    return {"old_level": old_level, "new_level": new_level, "relation": relation}

