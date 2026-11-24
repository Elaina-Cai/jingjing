"""新出口的影子档：把判交下来的 `deliver` 清单过一遍出口，**不真的发**。

## 为什么先影子

旧路径（`core/speech/rhythm.py` → `outbound.py` → provider）还在生产发消息。
如果这一层也发，同一条消息就会发两次——那是本仓库最硬的一条指标（重复发送率必须 0）。
所以影子档只做三件事：**校验、走一遍出口的判定分支、把六态回执记进日志**。

## 三条纪律

1. `off` 档一行不做：不 import 出口、不读载荷（默认档，行为与接线前逐字一致）。
2. 影子档恒 `dry_run=True`——这一层**没有能力**真发，参数写死，不给入口
   （要开闸得改代码 + 换 adapter，不是改开关）。适配器表用
   `core.execution.wechat_adapter.default_registry()`（登记了、未启用）。
3. 任何异常吞掉只 log：出口的影子不许影响这一回合真正说出去的话。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Sequence

from core import features
from core.contracts.delivery import DeliverItem, Receipt
from safelog import warn

log = logging.getLogger(__name__)

#: 影子档每一步都带这个理由，方便日后从日志里把它们和真投递区分开。
SHADOW_REASON = "dry_run"

#: 出口要按平台名取适配器（空名字会让 `_resolve` 直接判 `adapter_disabled`）。
PLATFORM_HINT = "wechat"


def exit_shadow_mode() -> str:
    """唯一读点：这一层只认 `DELIVERY_CONTRACT`。"""
    return features.mode(features.DELIVERY_CONTRACT, features.MODE_OFF)


def items_from_payload(payload: Any, *, chat_id: str = "") -> list[DeliverItem]:
    """把判决载荷里的 `deliver` 清单翻成出口的投递项；形状不对的项直接丢。"""
    if not isinstance(payload, Mapping):
        return []
    raw = payload.get("deliver")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    items: list[DeliverItem] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        if not kind:
            continue
        target = str(entry.get("target") or chat_id or "")
        text = entry.get("text")
        items.append(
            DeliverItem(
                kind=kind,
                target=target,
                text=str(text) if isinstance(text, str) else "",
                file=str(entry.get("file") or ""),
                basis=str(entry.get("basis") or ""),
            )
        )
    return items


def _registry() -> Any:
    """出口默认用的适配器表：微信适配器**已登记、未启用**（人要显式 `enable`）。

    未启用时出口拿到的回执是 `blocked(adapter_disabled)` —— 这是真实的现状，
    不是「没接」。人一旦 `enable("wechat")`，影子档立刻变成 `skipped(dry_run)`。
    """
    try:
        from core.execution.wechat_adapter import default_registry

        return default_registry()
    except Exception:  # noqa: BLE001 - 拿不到适配器表就当没有，退回 blocked
        warn("core/speech/exit_shadow.py:78 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def shadow_deliver(
    payload: Any,
    *,
    turn_id: str,
    chat_id: str = "",
    deliver_fn: Callable[..., list[Receipt]] | None = None,
    registry: Any = None,
) -> list[Receipt]:
    """影子投递：`off` 返空；其余档恒 `dry_run=True`，异常吞掉返空。"""
    if exit_shadow_mode() == features.MODE_OFF:
        return []
    items = items_from_payload(payload, chat_id=chat_id)
    if not items:
        return []
    try:
        if deliver_fn is None:
            from core.speech.delivery import deliver as deliver_fn  # type: ignore[no-redef]
        receipts = deliver_fn(
            items,
            turn_id=turn_id,
            dry_run=True,
            registry=_registry() if registry is None else registry,
            platform=PLATFORM_HINT,
        )
    except Exception:  # noqa: BLE001 - 影子不许影响这一回合
        log.warning("exit_shadow_failed turn_id=%s items=%d", turn_id, len(items))
        return []
    try:
        log.info(
            "exit_shadow turn_id=%s items=%d statuses=%s",
            turn_id,
            len(items),
            ",".join(str(r.status) for r in receipts),
        )
    except Exception:  # noqa: BLE001 - 日志坏了不算失败
        warn("core/speech/exit_shadow.py:116 Exception", err=repr(__import__("sys").exc_info()[1]))
    return list(receipts)
