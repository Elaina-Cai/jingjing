"""6 行矩阵：Reading + risk + others -> 四处置之一。这是全部的「判」。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.verdict.reading import Reading
from core.verdict.risk import RISK_OUT, RISK_READ, risk_hint

log = logging.getLogger(__name__)

SAY = "say"          # 说话（答 / 安慰 / 陪伴）
ASK = "ask"          # 问一句（缺一个槽 / 不敢猜）
DO = "do"            # 动手（必要时先批准）
ABSTAIN = "abstain"  # 弃权（指不回原文 / 没这个手 / 不该说）
DISPOSITIONS = (SAY, ASK, DO, ABSTAIN)


@dataclass(frozen=True)
class Decision:
    """矩阵的结论（唯一裁决点）。"""

    disposition: str = SAY
    reason: str = ""
    ask_slot: str = ""
    risk: int = RISK_READ
    others: int = 0
    hints: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        """给日志/测试用。"""
        return {
            "disposition": self.disposition,
            "reason": self.reason,
            "ask_slot": self.ask_slot,
            "risk": self.risk,
            "others": self.others,
        }


def decide(
    reading: Reading,
    *,
    risk: int = RISK_READ,
    others: int = 0,
    capability: bool = True,
) -> Decision:
    """唯一的裁决表。顺序即优先级，改动等于改行为。"""
    if not capability:
        return Decision(ABSTAIN, "no_capability", risk=risk, others=others,
                        hints=("没这个手",))
    if reading.wants_action() and not reading.has_quote():
        return Decision(ABSTAIN, "no_quote", risk=risk, others=others,
                        hints=("指不回原文就弃权",))
    # 2026-10-21：去掉 confidence 门。confidence 校准差、不可验证，
    # 让它当门等于让模型给个数字决定要不要说话。真实判据只用可验证信号：
    # quotes（指不回原文就弃权）/ gaps（缺槽就问）/ risk（不可逆要确认）。
    if reading.gaps > 0:
        slot = next(iter(reading.slots.keys()), "")
        return Decision(ASK, "missing_slot", ask_slot=slot, risk=risk, others=others)
    if reading.wants_action() and risk == RISK_OUT:
        return Decision(DO, "needs_confirm", risk=risk, others=others,
                        hints=(risk_hint(risk),))
    if reading.wants_action():
        return Decision(DO, "direct", risk=risk, others=others)
    if reading.affect < -0.3:
        return Decision(SAY, "comfort", risk=risk, others=others)
    return Decision(SAY, "short", risk=risk, others=others)
