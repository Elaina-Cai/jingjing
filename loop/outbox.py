"""队列作废时的"出声"：一句写好但没发出去的话，必须留下可查的痕迹。

2026-10-30：desk（发送工）没人拉起，一句话在 `outbound_queue` 里躺 10 分钟后
`state=expired, why=超过 600 秒没发出去`，没有任何日志、没有提醒——
她以为说过了（`act=say` 已落账），人以为没收到，中间没人报错。
"""
from __future__ import annotations

from safelog import warn


def note_expired(qid: int, chat: str, preview: str, waited: float) -> None:
    """作废一条：谁发给谁、原文开头、等了多久。"""
    warn("outbound_expired", qid=int(qid), chat=str(chat)[:40],
         preview=str(preview)[:30], waited_s=round(float(waited), 1))
