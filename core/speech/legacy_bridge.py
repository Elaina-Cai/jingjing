"""旧出口的最后一段换成新出口（U-14，默认 off）。

`core/speech/outbound.py::_send_text_or_file()` 是真正唯一的对外口：策略、输出闸、
社会闸、@ 裁定、语气观察、知识边界、近重复闸……十几道关都跑完了，最后一步只有一句
`result = await provider.send(target_id, applied.content)`。

本模块只做那一步的**替换**：把「平台 provider」换成「新出口
（`core/speech/delivery.deliver`）」。于是这一次发送第一次有了：平台回执
（`platform_msg_id`）、六种状态、幂等键、`delivery_receipts` 落账。

三条纪律：

1. **只换传输，不换判定**：上面那些闸门、文案、audit 一行不动。
2. **默认 off**：`LEGACY_VIA_EXIT=off` 时本模块不参与，行为逐字节不变。
3. **不确认就不算发出去**：只有 `sent` / `sent_unconfirmed` 算 ok；`skipped` /
   `blocked` / `failed` / `partial` / `pending_approval` 都算失败并带回原因。
   **绝不重试**——重复发送率必须是 0。

发送前 fallback（`handled=False`，让旧路照常走，只写一条日志）：

- 平台不在 ``BRIDGE_PLATFORMS`` 里；
- 注册表里没有这个平台的适配器、或它没被启用（**没启用不等于发不出去**，旧路还在）；
- 投递种类不在适配器自报的 ``kinds`` 里（今天微信只报 ``say``，文件仍走旧路）；
投递调用开始后，异常或缺回执都保持 handled=True 与执行未知，禁止退回旧路。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from core.contracts.delivery import DeliverItem, Receipt, unknown_execution_receipt
from safelog import warn

log = logging.getLogger(__name__)

#: 今天只有微信有真适配器（`core/execution/wechat_adapter.py`）。
BRIDGE_PLATFORMS: tuple[str, ...] = ("wechat",)

#: 旧出口的 ``applied.kind`` → 新出口的 ``DeliverItem.kind``。
KIND_MAP: dict[str, str] = {"say": "say", "text": "say", "file": "file",
                            "image": "file"}


#: 出口作用域：只有名单内的会话才许让新出口接手传输。
#: 空名单 = 全部（向后兼容，也是"还没决定给谁开"时的默认）。见 docs/decisions/出口.md §5.10。
SCOPE_ENV = "YINYUE_EXIT_SCOPE_CHATS"


def exit_scope() -> tuple[str, ...]:
    """读 ``YINYUE_EXIT_SCOPE_CHATS``（逗号分隔）。空元组 = 不设限。"""
    import os

    raw = str(os.environ.get(SCOPE_ENV, "") or "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def in_exit_scope(chat_id: str) -> bool:
    """这个会话是否允许新出口接手。名单为空 = 允许（不设限）。"""
    scope = exit_scope()
    if not scope:
        return True
    return str(chat_id or "").strip() in scope


def legacy_via_exit_mode() -> str:
    """唯一读点：``LEGACY_VIA_EXIT`` 三态。"""
    from core import features

    try:
        return str(features.mode(features.LEGACY_VIA_EXIT, features.MODE_OFF))
    except Exception:  # noqa: BLE001 - 开关读不出来就按 off（永不改行为）
        warn("core/speech/legacy_bridge.py:73 Exception", err=repr(__import__("sys").exc_info()[1]))
        return features.MODE_OFF


@dataclass(frozen=True)
class SendOutcome:
    """旧出口只认三样：成没成、消息号、错在哪。"""

    ok: bool
    msg_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class BridgeResult:
    """``handled=False`` = 这次不归我管，调用方照旧走 provider。"""

    handled: bool
    outcome: SendOutcome = SendOutcome(False)
    #: 影子档要能说清「为什么没走下去」——空串 = 没走到能判的那一步。
    status: str = ""
    reason: str = ""

    def as_send_result(self) -> SendOutcome:
        return self.outcome




def unknown_bridge_result(*, kind: str, target: str, detail: str = "") -> BridgeResult:
    """进入投递调用后缺少确定结果，保留接管，禁止退回另一发送器。"""
    receipt = unknown_execution_receipt(kind=kind, target=target, detail=detail)
    return BridgeResult(True, receipt_outcome(receipt), status=receipt.status, reason=receipt.reason)


def receipt_outcome(receipt: Receipt) -> SendOutcome:
    """新出口的一张回执 → 旧出口要的三样。纯函数，测试直接打这里。"""
    status = str(getattr(receipt, "status", "") or "")
    if status in ("sent", "sent_unconfirmed"):
        return SendOutcome(True, msg_id=str(getattr(receipt, "platform_msg_id", "") or ""))
    reason = str(getattr(receipt, "reason", "") or "")
    detail = str(getattr(receipt, "detail", "") or "")
    tail = detail or reason or "没发出去"
    return SendOutcome(False, error=f"{status or 'failed'}:{tail}")


def deliver_via_exit(
    platform: str,
    target: str,
    text: str,
    *,
    kind: str = "say",
    filepath: str = "",
    turn_id: str = "",
    chat_id: str = "",
    registry: Any = None,
    ledger: Any = None,
    dry_run: bool = False,
) -> BridgeResult:
    """把一条消息交给新出口。**同步阻塞**（微信真发约 30 秒），调用方用线程跑。"""
    mode = legacy_via_exit_mode()
    if mode == "off":
        return BridgeResult(False, status="blocked", reason="mode_off")
    # shadow 只允许「dry_run 走一遍」——不许真发（`on` 才许真发）。
    if mode != "on" and not dry_run:
        return BridgeResult(False, status="blocked", reason="shadow_needs_dry_run")
    name = str(platform or "").strip().lower()
    if name not in BRIDGE_PLATFORMS:
        return BridgeResult(False, status="blocked", reason="platform_not_bridged")
    if not in_exit_scope(str(chat_id or "").strip() or str(target or "").strip()):
        log.info("legacy_bridge_out_of_scope platform=%s chat_id=%s", name, chat_id or target)
        return BridgeResult(False, status="skipped", reason="out_of_scope")
    mapped = KIND_MAP.get(str(kind or "").strip().lower(), "")
    if not mapped:
        log.info("legacy_bridge_kind_unknown platform=%s kind=%s", name, kind)
        return BridgeResult(False, status="blocked", reason="kind_unknown")
    if registry is None:
        # 必须是**适配器**注册表（出口的世界），不是工具注册表（她办事的世界）。
        # 2026-10-21 真机抓到的 bug：这里原来取的是 ``core.jobs.executor.default_registry()``
        # ——那是 ToolRegistry，里面没有 "wechat"，于是每一条影子档日志都报
        # ``no_adapter``（听起来像"微信没接"），把真正的 ``adapter_disabled`` 盖住了。
        try:
            from core.execution.wechat_adapter import default_registry as adapter_registry

            registry = adapter_registry()
        except Exception:  # noqa: BLE001 - 没有注册表就退回旧路
            warn("core/speech/legacy_bridge.py:152 Exception", err=repr(__import__("sys").exc_info()[1]))
            registry = None
    if registry is None:
        log.info("legacy_bridge_no_registry platform=%s", name)
        return BridgeResult(False, status="blocked", reason="no_registry")
    try:
        adapter = registry.get(name)
    except Exception:  # noqa: BLE001
        warn("core/speech/legacy_bridge.py:159 Exception", err=repr(__import__("sys").exc_info()[1]))
        adapter = None
    if adapter is None:
        log.info("legacy_bridge_no_adapter platform=%s", name)
        return BridgeResult(False, status="blocked", reason="no_adapter")
    try:
        if not registry.is_enabled(name):
            log.info("legacy_bridge_adapter_disabled platform=%s", name)
            return BridgeResult(False, status="blocked", reason="adapter_disabled")
        caps = adapter.capabilities()
        if mapped not in set(getattr(caps, "kinds", frozenset())):
            log.info("legacy_bridge_kind_unsupported platform=%s kind=%s", name, mapped)
            return BridgeResult(False, status="blocked", reason="kind_unsupported")
    except Exception:  # noqa: BLE001 - 自报能力失败就退回旧路
        log.warning("legacy_bridge_caps_failed platform=%s", name)
        return BridgeResult(False, status="blocked", reason="caps_failed")

    item = DeliverItem(kind=mapped, target=str(target or ""), text=str(text or ""),
                       file=(filepath or None))
    turn = str(turn_id or "") or time.strftime("legacy-%Y%m%d%H%M%S")
    hook = ledger
    if hook is None:
        try:
            from core.terminal.delivery_receipts import make_ledger

            hook = make_ledger(turn_id=turn, chat_id=str(chat_id or target or ""))
        except Exception:  # noqa: BLE001 - 落账是旁注，不阻断
            warn("core/speech/legacy_bridge.py:185 Exception", err=repr(__import__("sys").exc_info()[1]))
            hook = None
    try:
        from core.speech.delivery import deliver

        receipts = deliver([item], turn_id=turn, registry=registry, platform=name,
                           ledger=hook, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - 可能已发送，不能退回旧路
        log.warning("legacy_bridge_deliver_failed platform=%s target=%s", name, target)
        return unknown_bridge_result(kind=mapped, target=target, detail=f"{type(exc).__name__}: {exc}")
    if not receipts or not isinstance(receipts, (list, tuple)) or not isinstance(receipts[0], Receipt):
        return unknown_bridge_result(kind=mapped, target=target, detail="新出口未返回可核对回执")
    first = receipts[0]
    return BridgeResult(True, receipt_outcome(first),
                        status=str(getattr(first, "status", "") or ""),
                        reason=str(getattr(first, "reason", "") or ""))
