# -*- coding: utf-8 -*-
"""「这句是不是冲她来的」——字面判据（纯文本，无 I/O、无模型）。

2026-10-23 之后这里只剩一件事：给上层一个**字面**读数（私聊 / 显式 @ /
引用她 / 拍了拍她）。回不回、接不接，全部由模型在同一个 transcript 里自己定，
代码不再据此拦人。

历史：本函数曾住在 ``core/turn/verdict.py``，与 ``decide()`` 的四道格门放在
一起；格门整族删除后，只把这个字面判据搬出来单独留着。
"""
from __future__ import annotations

import re
from typing import Any

_AT_GAP = r"[\s\u2005\u200b\u00a0\u3000]"


def addressed_to_her(
    content: Any,
    bot_names: Any = (),
    *,
    is_direct: bool = False,
    explicit_address: bool = False,
) -> bool:
    # task-45 v2：本回合是否「被点名」（被点名一律不拦）。本地确定性判据：
    # 私聊、正文精确 @<名字>、微信引用她的消息、「拍了拍」她；仅出现名字不算。
    # task-43 的 utterance_edges 落库后改为读边，届时删除本函数（禁止两份解析）。
    if is_direct or explicit_address:
        return True
    text = str(content or "")
    if not text:
        return False
    for raw in bot_names or ():
        name = str(raw or "").strip()
        if not name:
            continue
        quoted = re.escape(name)
        if re.search("@" + _AT_GAP + "*" + quoted, text):
            return True
        if re.search(r"\[引用\s*" + quoted + r"\s*[:：]", text):
            return True
        if re.search(r"拍了拍\s*[\x22\x27\u201c\u201d]?" + quoted, text):
            return True
    return False

# ---- 呼唤形式的读数（原属 N1 ``core/context/intent.py``，2026-10-23 搬来） ----
# 四种呼唤形式 S1–S4 的**规范代号**（唯一一份字面量在 ``core.group_state``，这里只做
# 等价的**短名**映射；判据由路由的结构化解析器给，规格 §8 第 5 条红线）。
#: ``summons`` 里的取值 → 审计用的短名（顺序即规范顺序）。
SUMMONS_EVIDENCE_LABELS = {
    "S1": "mention",   # @她
    "S2": "name",      # 消息里含她的名字
    "S3": "pat",       # 拍她
    "S4": "quote",     # 引用她的消息
}

#: 判据里认得的呼唤形式（未知取值忽略，不进证据）。
SUMMONS_KINDS = frozenset(SUMMONS_EVIDENCE_LABELS)

#: 算作「显式指向她」的呼唤形式：S1 @她 / S3 拍她 / S4 引用她。
#: S2（消息里含她的名字）**不算**——名字出现可能是"说她"，不是"对她说"。
EXPLICIT_ADDRESS_SUMMONS = frozenset({"S1", "S3", "S4"})


def summons_evidence(summons: Any) -> tuple[str, ...]:
    """``summons`` 集合 → 去重、定序的证据标签元组（**集合语义**）。

    * **顺序无关**：输出按 :data:`SUMMONS_EVIDENCE_LABELS` 的规范顺序，不按传入顺序；
    * **重复无关**：同一形式出现 N 次只出现一次（``set`` 塌缩）；
    * **不叠加**：四种齐出也只得到 4 个**标签**，而"有没有被呼唤"这件事仍然是一个布尔
      （:func:`summons_present`）——消费方**不许**按标签个数加分；
    * **不落原文**：只回短名，不回消息文本。

    ``S3``（拍）与动作 ``A1`` 是**同一条事实的两种读法**：路由只产一条信号、同时填
    ``summons={S3}`` 与 ``actions={A1}``，这里**只**读 ``summons``。
    """
    raw = summons if isinstance(summons, (list, tuple, set, frozenset)) else ()
    present = {str(item or "").strip().upper() for item in raw}
    return tuple(
        label for kind, label in SUMMONS_EVIDENCE_LABELS.items() if kind in present
    )


def summons_present(summons: Any) -> bool:
    """本单元**有没有**呼唤形式（``evidence.summons ∈ {0,1}``）。

    刻意是布尔而不是计数：四种形式任意组合、任意重复次数，答案都只有这一个。
    """
    return bool(summons_evidence(summons))
