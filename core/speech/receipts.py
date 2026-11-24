"""出口回执落库（`docs/decisions/出口.md` §5.3）。

旧路径（`core/speech/rhythm.py` → `core/speech/outbound.py` → provider）一直在发消息，
但「发出去的是什么、有没有回执」只写在日志里：`delivery_receipts` 表长期 0 行。
这个模块补的就是那一行——**只写，不改判定，不拦发送**。

三条纪律：
1. 开关 `DELIVERY_CONTRACT` 是 off 就一行不写（默认 off ⇒ 行为逐字不变）；
   `shadow` 也写——**"谁负责传输"与"有没有回执"是两件事**：shadow 档的含义是
   "新出口还没接管传输，但它在旁边看"，而回执记的是这次到底发成了什么
   （`ack` 只按 provider 给的证据写，弱证据就是 `unconfirmed`，不会把旁注写成强证据）。
2. 写失败只 log：回执是旁注，旁注坏了不许把已经发出去的消息改判成失败。
3. 真话优先：provider 给了平台消息号就写 `platform_id`，只拿到「输入框空了」就写
   `unconfirmed`，不许把弱证据写成强证据。
"""

from __future__ import annotations

import logging
from typing import Any

from core import features
from core.contracts.delivery import Receipt
from safelog import warn

log = logging.getLogger(__name__)

#: provider 没给 adapter 名时的兜底（宁愿空着，也不猜是哪个平台）。
ADAPTER_UNKNOWN = ""

#: 旧出口路径的固定说明，免得日后有人以为这条是「在手的事」写的。
DETAIL_LEGACY_EXIT = "旧出口路径（rhythm → outbound）"


def receipt_mode() -> str:
    """唯一读点：这一层只认 `DELIVERY_CONTRACT`。"""
    return features.mode(features.DELIVERY_CONTRACT, features.MODE_OFF)


def build_receipt(applied: Any, result: Any) -> Receipt:
    """把一次发送结果翻成一行回执。纯函数，不碰库。"""
    target = str(getattr(applied, "who", "") or "")
    chat_id = ""
    try:
        chat_id = str(applied.target.chat_id or "")
    except Exception:  # noqa: BLE001 - 形状不对就空着，不猜
        warn("core/speech/receipts.py:45 Exception", err=repr(__import__("sys").exc_info()[1]))
        chat_id = ""
    platform = ""
    try:
        platform = str(applied.target.platform or "")
    except Exception:  # noqa: BLE001
        warn("core/speech/receipts.py:50 Exception", err=repr(__import__("sys").exc_info()[1]))
        platform = ""
    server_id = str(getattr(result, "server_id", "") or "")
    local_id = str(getattr(result, "local_id", "") or "")
    ack = str(getattr(result, "ack", "") or "")
    if not ack:
        ack = "platform_id" if (server_id or local_id) else "unconfirmed"
    return Receipt(
        status="sent",
        kind=str(getattr(applied, "kind", "") or "text"),
        target=chat_id or target,
        ack=ack,
        platform_msg_id=server_id or local_id,
        client_msg_id=local_id,
        adapter=platform or ADAPTER_UNKNOWN,
        detail=DETAIL_LEGACY_EXIT,
    )


def record_delivery_receipt(
    applied: Any,
    result: Any,
    *,
    turn_id: str = "",
    chat_id: str = "",
    store: Any = None,
) -> bool:
    """落一行回执；off 档、写失败、形状不对都返 False（不抛）。"""
    if receipt_mode() == features.MODE_OFF:
        return False
    try:
        if store is None:
            from core.terminal import delivery_receipts as store  # type: ignore[no-redef]
        receipt = build_receipt(applied, result)
        # 会话 id 优先（`build_receipt` 已经按 chat_id → 显示名 的顺序挑过）
        target_chat = chat_id or receipt.target
        turn = turn_id or f"outbound:{target_chat}"
        return bool(store.record_receipt(receipt, turn_id=turn, chat_id=target_chat))
    except Exception:  # noqa: BLE001 - 回执是旁注
        log.warning("delivery_receipt_record_failed adapter=%s", str(getattr(applied, "kind", "")))
        return False
