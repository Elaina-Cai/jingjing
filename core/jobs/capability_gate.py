"""能力闸：动手之前先问一句「这件事我们到底有没有口」。

为什么需要它：动词表能判出「这句话是要办的事」，但判不出「我们办得了吗」。
真机 20 条对账的结果是漏 0、滥 4——「转 200 块 / 订高铁 / 打电话 / 发邮件」
全被判成要办，于是会开出一件**永远办不成**的事，等人批准，再报没有执行器。

两种错误里，这种更伤：它消耗了人的信任（人以为她要做，结果什么都没发生）。
所以这里只有一条规矩：**能力不存在 → 当场说「我没有这个口」，不开事**。

`ABSENT` 是「已知做不到」的负面清单——它不是穷举说法，而是穷举**我们家没有的能力域**
（每加一个能力就该从这里删一条）。判不出来（不在清单里）一律放行，交给后面的值校验与确认。
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

ABSENT: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("支付/转账", ("转账", "转给", "付款", "支付", "发红包", "充话费", "打钱"), "钱的事我不能碰，转出去就收不回来了"),
    ("订票/预订", ("订票", "订机票", "订高铁", "订酒店", "预订", "帮我订", "买票"), "我没有能下单的口，订票得你自己来"),
    ("打电话/语音", ("打电话", "拨号", "语音通话", "打个电话"), "我只能发文字，打不了电话"),
    ("发邮件", ("发邮件", "邮件发给", "邮件发出去", "写封邮件"), "我没有邮箱，发不了邮件"),
    ("改账号设置", ("改密码", "换绑定", "解绑", "改手机号"), "账号设置我不敢动，你自己来"),
)

SLOT_QUESTIONS: dict[str, str] = {
    "chat": "发到哪个群，或者发给谁？",
    "text": "要说什么？",
    "time": "什么时候？",
    "query": "查什么？",
    "url": "哪个网址？",
    "path": "哪个文件？",
    "element": "屏幕上的哪一块？",
    "amount": "多少钱？",
}


def slot_question(slot: str) -> str:
    """缺一个槽时该问的那一句（槽级模板，数量是槽数，不随说法增长）。"""
    return SLOT_QUESTIONS.get(str(slot or ""), f"{slot} 是什么？")


def absent_reason(text: str) -> str:
    """这句话指向一件我们没有口的事吗？返回一句人话，否则空串。"""
    body = str(text or "")
    if re.search(r"转\s*(?:给)?[^，。；]{0,6}?\s*(?:\d+|块|元)", body):
        log.info("capability_absent domain=money")
        return ABSENT[0][2]
    for name, words, reason in ABSENT:
        for word in words:
            if word in body:
                log.info(f"capability_absent domain={name} word={word}")
                return reason
    return ""


def gate(text: str, *, action: str = "") -> dict[str, Any]:
    """``{"ok": bool, "reason": str, "kind": "absent"|"ok"}``。不猜：判不出来就放行。"""
    reason = absent_reason(text) or absent_reason(action)
    if reason:
        return {"ok": False, "kind": "absent", "reason": reason}
    return {"ok": True, "kind": "ok", "reason": ""}


def refuse_line(reason: str) -> str:
    """拒绝时对外的说法：认账 + 给出人能走的路，不表演。"""
    tail = "我把这句记下了，你要我之后换个办法再说。" if reason else ""
    return f"{reason}。{tail}" if reason else "这件事我没有这个口。"
