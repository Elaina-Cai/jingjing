"""风险与在场：代码算，模型无权定价。"""

from __future__ import annotations

import logging

from core.intake.observation import Observation

log = logging.getLogger(__name__)

RISK_READ, RISK_LOCAL, RISK_OUT = 0, 1, 2
OTHERS_ME, OTHERS_ROOM, OTHERS_THIRD = 0, 1, 2

# 效果 -> 风险（唯一映射表；新效果必须在这里登记）
EFFECT_RISK = {
    "read": RISK_READ,
    "write_local": RISK_LOCAL,
    "ui_control": RISK_LOCAL,
    "external_send": RISK_OUT,
}
RISK_HINT = {
    RISK_READ: "只是看一眼，不改动任何东西",
    RISK_LOCAL: "会改动本机上的东西，改完不能自动撤回",
    RISK_OUT: "会发出去，发出去就收不回来",
}


def risk_of(effects: object) -> int:
    """效果 -> 风险（多效果取最高；不认识的当最高，fail closed）。"""
    if isinstance(effects, str):
        items = (effects,)
    elif isinstance(effects, (list, tuple, set, frozenset)):
        items = tuple(effects)
    elif effects is None:
        return RISK_READ
    else:
        items = (str(effects),)
    worst = RISK_READ
    for item in items:
        key = str(getattr(item, "value", item) or "").strip()
        if not key:
            continue
        if key not in EFFECT_RISK:
            log.info("risk_unknown_effect effect=%s", key)
            return RISK_OUT
        worst = max(worst, EFFECT_RISK[key])
    return worst


def others_of(obs: Observation, risk: int = RISK_READ) -> int:
    """在场 -> 需要谁动手（0 只对我 / 1 在场可见 / 2 需第三方）。"""
    if risk == RISK_OUT:
        return OTHERS_THIRD
    return OTHERS_ROOM if obs.is_group() else OTHERS_ME


def risk_hint(risk: int) -> str:
    """一句人话，写进问句或回显。"""
    return RISK_HINT.get(int(risk), RISK_HINT[RISK_OUT])
