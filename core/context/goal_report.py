# -*- coding: utf-8 -*-
"""把「已经办到哪一步」摊成几行事实，供复核回合照着说。

为什么单独一个模块：**引用存在 ≠ 已经送达**。过去一句话把两件事混着说，
台账里写得像办完了，用户那边什么都没收到（E-59 同一类事故）。这里只做两件事：

1. 把目标的完成条件和已登记的证据原样列出——不解释、不代签名；
2. 把还没拿到证据的那几项单独点名（产物保存 / 投递动作 / 本机观察 / 平台送达），
   让「还差什么」有字可说，而不是靠模型自己揣。

只读：不写世界模型、不提交目标、不发消息。
"""
from __future__ import annotations

from typing import Any, Iterable

#: 完成一份「找资料」类任务要分别拿到的证据；缺哪项就别说哪项办成了。
EVIDENCE_CLASSES: tuple[str, ...] = ("产物保存", "投递动作", "本机观察", "平台送达")

#: 证据引用前缀 → 它最多能证明什么。认不出的前缀一律当「未分类」，不许升级成送达。
_PROOF_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("artifact:", "产物保存"),
    ("file:", "产物保存"),
    ("queue:", "投递动作"),
    ("job:", "投递动作"),
    ("work_item:", "本机观察"),
    ("receipt:", "本机观察"),
    ("ack:", "平台送达"),
)


def proof_of(ref: str) -> str:
    """一条引用最多能证明什么；认不出返回空串（不猜）。"""
    value = str(ref or "").strip()
    for prefix, proof in _PROOF_BY_PREFIX:
        if value.startswith(prefix):
            return proof
    return ""


def _refs(goal: Any) -> list[str]:
    raw: Iterable[Any] = getattr(goal, "evidence_refs", ()) or ()
    return [str(item).strip() for item in raw if str(item).strip()]


def missing_classes(refs: Iterable[str]) -> tuple[str, ...]:
    proven = {proof_of(ref) for ref in refs}
    return tuple(item for item in EVIDENCE_CLASSES if item not in proven)


def status(goal: Any, *, remaining: str = "") -> str:
    """复核回合里那几行。没有完成条件也没有证据时返回空串（没目标可报）。"""
    if goal is None:
        return ""
    refs = _refs(goal)
    done_when = str(getattr(goal, "done_when", "") or "").strip()
    desired = str(getattr(goal, "desired_outcome", "") or "").strip()
    if not done_when and not desired and not refs:
        return ""

    lines = []
    condition = done_when or desired
    if condition:
        lines.append("完成条件：%s" % condition)
    if desired and desired != condition:
        lines.append("要交什么：%s" % desired)
    lines.append("已登记证据：%s" % ("、".join(refs) if refs else "还没有"))
    if str(remaining or "").strip():
        lines.append("还差：%s" % str(remaining).strip()[:120])
    missing = missing_classes(refs)
    if missing:
        lines.append("尚未取得证据：%s" % "、".join(missing))
    lines.append(
        "报告口径：引用「存在」只证明登记过，不证明已送达；"
        "「产物保存」「投递动作」「本机观察」「平台送达」逐项按证据说，缺的就说还差。"
    )
    return "\n".join(lines)


#: 终态目标的台账标签；只有这两个状态能出历史总结。
_CLOSED_LABELS: dict[str, str] = {"completed": "已结案", "abandoned": "已停止"}


def closed_summary(goal: Any) -> str:
    """已结案 / 已停止目标的历史台账：只读、有界，不能当作今天的指引。

    标签只来自目标状态；登记过的引用和仍缺的证据类别原样列出，不据此
    声称已送达或已全部办完，也不回放当时的行动倾向。
    """
    if getattr(goal, "status", "") != "closed":
        return ""
    label = _CLOSED_LABELS.get(goal.state)
    if not label:
        return ""
    refs = [ref for ref in _refs(goal) if proof_of(ref)]
    condition = str(getattr(goal, "done_when", "") or "").strip()
    if not condition:
        condition = str(getattr(goal, "desired_outcome", "") or "").strip()
    key = str(getattr(goal, "key", "") or "（未命名）")[:32]
    parts = ["目标 %s：%s" % (key, label)]
    if condition:
        parts.append("完成条件：" + condition[:48])
    registered = [proof for proof in EVIDENCE_CLASSES if any(proof_of(ref) == proof for ref in refs)]
    parts.append("已登记证据：" + ("、".join(registered) if registered else "还没有"))
    missing = missing_classes(refs)
    if missing:
        parts.append("尚未取得证据：" + "、".join(missing))
    if refs:
        parts.append("已登记引用：" + "、".join(refs)[:48])
    return ("[此前目标的历史台账]\n" + "｜".join(parts)
            + "\n按回执说明保存和送达情况。对方有新委托时再决定下一步。")


def line(goal: Any) -> str:
    """进提示词的一行（≤120 字）：完成条件 / 已登记 / 还缺哪类证据。

    只对**写了完成条件或已经有证据**的目标出现：没这两样的目标多说一句都是噪音。
    """
    if goal is None:
        return ""
    refs = _refs(goal)
    condition = str(getattr(goal, "done_when", "") or "").strip()
    if not condition and not refs:
        return ""
    parts = []
    if condition:
        parts.append("完成条件：" + condition[:60])
    parts.append("已登记证据：" + ("、".join(refs)[:60] if refs else "还没有"))
    missing = missing_classes(refs)
    if missing:
        parts.append("尚未取得证据：" + "、".join(missing))
    return "｜".join(parts)[:120]


__all__ = ["EVIDENCE_CLASSES", "closed_summary", "line", "missing_classes", "proof_of", "status"]
