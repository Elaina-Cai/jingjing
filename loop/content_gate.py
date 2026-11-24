# -*- coding: utf-8 -*-
"""外发内容闸：这四类话不许她替主人自动发出去（2026-11-04，用户 m03054 的第 3 项）。

「全 C」之后外发不再等点头（见 ``loop/outbound_gate.py``），于是有四类话不能由她自己说：
**地址/联系方式、钱、替别人答应、时间承诺**。这不是"内容审查"，是**代理权限的边界**：
她可以转达、可以答复、可以说不知道；但不能替主人许诺、不能替主人掏钱、
不能把主人家的门牌号或手机号发出去。

判据只用正则与词表：**不读库、不看模型、不打分、不比语义相似度**。
每一类给稳定原因码（``content:money`` 等），被拦是可记账的裁决（``ack="skipped"``）。

三个"故意留的口子"（都是为了不把正常说话误伤，代价是少拦一些）：

1. **问句不拦**：``要不要我垫？`` 是在问，不是承诺（``_is_asking``）。
2. **拒绝/自陈不拦**：``转账这事我办不了`` ``下单要花钱，那也是主人的钱`` 是她在**拒绝**，
   拦掉反而让她没法说明自己做不到（``_is_refusing``）。
3. **系统消息不拦**：``<sysmsg …>`` 之类不是人话（``_is_noise``）。

这三条都记在 §25 的"已知误拦/漏拦"里；真机 3322 条她自己的消息回放，拦截率见文档。
"""

from __future__ import annotations

import re

#: 类目 -> 给日志/给主人看的一句话
LABELS: dict[str, str] = {
    "address": "涉及地址或联系方式（门牌/手机号/身份证/邮箱）",
    "money": "涉及钱（金额、付款、转账、二维码、垫付）",
    "others": "替主人答应了什么（代他决定/同意/说好）",
    "promise": "替主人做了时间上的承诺（保证 + 具体时间）",
}

#: 顺序＝优先级：越私密、越不可逆的越先判
ORDER: tuple[str, ...] = ("address", "money", "others", "promise")

_ASK_TAIL = ("?", "？", "吗", "呢", "好不好", "行不行", "要不要", "需不需要", "可以吗",
             "办不办", "走不走", "去不去", "来不来")

#: 拒绝/自陈/否定：她在说"我不做/我没有/办不了"，不是在承诺
_DENY_MARKERS = ("不能", "不会", "不敢", "不许", "别", "不了", "没法", "没办法", "没有",
                 "不做", "不拿", "不替", "不猜", "不主动", "拒", "办不了", "拿不到", "越界")

_MONEY_WORDS = (
    "转账", "付款", "收款", "付款码", "收款码", "二维码", "红包", "垫付", "报销", "打钱",
    "扫码", "充值", "下单", "借条", "还钱", "借钱", "预付", "定金", "尾款", "押金",
    "银行卡", "支付宝", "微信支付", "付款链接", "分期",
)
_MONEY_RE = re.compile(
    r"(?:[\u00a5\uffe5]\s*\d+)"                                 # ¥128 / ￥88
    r"|(?:\d+(?:\.\d+)?\s*(?:元|块钱|块|万|千|百|角|毛))"          # 128 元 / 5 块 / 3.5 万
    r"|(?:\b\d{12,19}\b)"                                        # 卡号一类长数字
    r"|(?:\b(?:RMB|CNY|USD)\b)"
)

_ADDRESS_WORDS = (
    "详细地址", "收货地址", "门牌号", "身份证号", "银行卡号", "手机号码", "手机号",
    "家庭住址", "寄到", "我家在", "家住",
)
_ADDRESS_RE = re.compile(
    r"(?:\b1[3-9]\d{9}\b)"                                      # 手机号
    r"|(?:\b\d{17}[\dXx]\b)"                                    # 身份证
    r"|(?:[\w.+-]+@[\w-]+\.[\w.]+)"                             # 邮箱
    r"|(?:[\u4e00-\u9fa5]{2,6}(?:省|市|区|县|镇)[^，。！？\n]{0,20}\d+\s*号)"
    r"|(?:\d+\s*号楼(?:\s*\d+\s*单元)?(?:\s*\d+\s*室)?)"          # 3 号楼 / 3 号楼 2 单元 502 室
    r"|(?:\d+\s*单元\s*\d+\s*室)"
    r"|(?:\d+\s*栋\s*\d+\s*室)"
)

_OTHERS_RE = re.compile(
    r"(?:我替|我代|替你|帮你|代你)[^。！？\n]{0,12}(?:答应|决定|同意|拒绝|说好|约好|报名|签字|签)"
)

_COMMIT_WORDS = ("我保证", "我一定", "我答应", "包在我身上", "我发誓", "我说定了", "我跟他约好",
                 "我跟他说好", "我这就去", "我马上办")
_TIME_RE = re.compile(
    r"(?:明早|明晚|明天|后天|今晚|今天|周末|月底|下个?月|下?周[一二三四五六日天]|\d+\s*(?:点|号|日))"
)


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _is_noise(text: str) -> bool:
    t = _norm(text)
    return t.startswith("<") or "sysmsg" in t[:40]


def _is_asking(text: str) -> bool:
    """疑问句里出现这些词是"在问"，不是"在许诺"。"""
    t = _norm(text).rstrip("。.!！")
    return any(t.endswith(x) for x in _ASK_TAIL)


def _is_refusing(text: str) -> bool:
    """她在拒绝或自陈边界（"转账这事我办不了"），不是承诺。"""
    return any(w in text for w in _DENY_MARKERS)


def _hit_address(text: str) -> bool:
    return bool(_ADDRESS_RE.search(text)) or any(w in text for w in _ADDRESS_WORDS)


def _hit_money(text: str) -> bool:
    return bool(_MONEY_RE.search(text)) or any(w in text for w in _MONEY_WORDS)


def _hit_others(text: str) -> bool:
    return bool(_OTHERS_RE.search(text))


def _hit_promise(text: str) -> bool:
    return any(w in text for w in _COMMIT_WORDS) and bool(_TIME_RE.search(text))


def check(text) -> tuple[bool, str]:
    """能不能自动发出去：``(True, "ok")`` 或 ``(False, "content:<类目>")``。纯判断，不读写任何东西。"""
    t = _norm(text)
    if not t:
        return True, "ok"
    if _is_noise(t) or _is_asking(t) or _is_refusing(t):
        return True, "ok"
    if _hit_address(t):
        return False, "content:address"
    if _hit_money(t):
        return False, "content:money"
    if _hit_others(t):
        return False, "content:others"
    if _hit_promise(t):
        return False, "content:promise"
    return True, "ok"


def explain(reason: str) -> str:
    """把原因码翻成人话（日志与主人抄送共用）。"""
    r = str(reason or "")
    if r.startswith("content:"):
        return LABELS.get(r.split(":", 1)[1], "内容里有不该自动发的东西")
    return r


__all__ = ["LABELS", "ORDER", "check", "explain"]
