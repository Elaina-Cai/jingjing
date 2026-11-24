# -*- coding: utf-8 -*-
"""出口的实现（U-04）：一次投递 = 一次 adapter 调用 + 一条回执。

出处：`docs/decisions/出口.md` §2 / §4 / §5 / §7 / §8 / §9 / §14。

出口的三条纪律（本文件就是它们）：
1. **只放行、不判**：不改写正文、不替判挑话、不因失败改内容重发；
2. **六种结果，不许有第七种**：每一件进得来的东西都必须回一条 ``Receipt``，
   条数与输入**等长**——没有「沉默地丢掉」这个选项。第六态 ``pending_approval``
   由 ``deliver_pending_approvals()`` 产出：**只落回执、一个字节都不发**；
3. **幂等**：同一回合里同一条话只发一次（重复发送率必须 0，出过真实事故）。

本文件不碰数据库：落账通过 ``ledger`` 钩子交给调用方（迁移 64 的表由
``core/speech/outbound.py`` / `db/migrations.py` 在开关打开那一步接上）。
"""

from __future__ import annotations

from dataclasses import replace
import logging
import time
from typing import Any, Callable, Mapping, Sequence

from core.constants import DELIVER_MAX_PER_TURN, RETRY_BACKOFF_S, RETRY_MAX
from core.contracts.delivery import (
    unknown_execution_receipt,
    FAIL_RETRYABLE,
    PENDING_APPROVAL,
    DeliverItem,
    Receipt,
)
from safelog import warn

log = logging.getLogger(__name__)

#: 进程内幂等键：(turn_id, kind, target, content_hash)。生产由账本行承接。
_SENT: dict[tuple[str, str, str, str], str] = {}
#: 每回合已发条数（出口.md §9：单回合 ≤2）
_SLOTS_PER_TURN: dict[str, set[tuple[str, str]]] = {}
_SENT_PER_TURN: dict[str, int] = {}

LedgerHook = Callable[[Receipt], None]


def reset_idempotency() -> None:
    """清空进程内幂等状态（只给测试用；生产靠账本行，不靠内存）。"""
    _SENT.clear()
    _SENT_PER_TURN.clear()


def _resolve(registry: Any, platform: str | None) -> tuple[Any, Any, str]:
    """取适配器 + 能力。返回 ``(adapter, caps, block_reason)``。"""
    if registry is None:
        return (None, None, "adapter_disabled")
    name = str(platform or "").strip()
    adapter = None
    getter = getattr(registry, "get", None)
    if callable(getter):
        if name:
            adapter = getter(name)
        elif isinstance(registry, Mapping) and registry:
            adapter = next(iter(registry.values()))
    if adapter is None:
        return (None, None, "adapter_disabled")

    caps = None
    try:
        caps = adapter.capabilities()
    except Exception:  # noqa: BLE001 - 自报失败就当「没有能力」，不许放行
        warn("core/speech/delivery.py:66 Exception", err=repr(__import__("sys").exc_info()[1]))
        caps = None

    enabled = False
    is_enabled = getattr(registry, "is_enabled", None)
    if callable(is_enabled) and name:
        enabled = bool(is_enabled(name))
    if caps is not None and getattr(caps, "enabled", False):
        enabled = True
    if not enabled:
        return (adapter, caps, "adapter_disabled")
    return (adapter, caps, "")


def _guard(
    item: DeliverItem,
    *,
    turn_id: str,
    caps: Any,
    adapter: Any,
    dry_run: bool,
    max_items: int,
) -> Receipt | None:
    """校验①：能不能发。返回 ``None`` 表示放行。"""
    kind = str(item.kind or "").strip()
    target = str(item.target or "").strip()

    if not target:
        return Receipt(status="blocked", kind=kind, target="", reason="no_target")
    if not str(item.text or "").strip() and not item.file:
        return Receipt(status="skipped", kind=kind, target=target, reason="empty_content")
    if adapter is None:
        return Receipt(status="blocked", kind=kind, target=target, reason="adapter_disabled")

    kinds = frozenset(getattr(caps, "kinds", ()) or ())
    targets = frozenset(getattr(caps, "targets", ()) or ())
    if kinds and kind not in kinds:
        return Receipt(status="blocked", kind=kind, target=target, reason="kind_not_registered")
    if targets and target not in targets:
        return Receipt(status="blocked", kind=kind, target=target, reason="target_not_found")

    if dry_run:
        # dry-run 也要留一行（出口.md §15-4），否则「她说过什么」会缺行
        return Receipt(status="skipped", kind=kind, target=target, reason="dry_run")

    # 顺序有讲究：**先判重复，再判额度**。同一句话再交一次，答案是「这条已经发过」，
    # 不是「这回合额度用完了」——后者会让人以为换个回合就能再发，那是错的方向。
    key = (turn_id, kind, target, item.content_hash())
    if key in _SENT:
        return Receipt(status="skipped", kind=kind, target=target, reason="duplicate")

    # 2026-10-21 真机：一条长回复会被拆成多个气泡，每个气泡都是一次 deliver。
    # 原来按「次数」记账 → 第 3 个气泡就 over_limit，用户看到的是「她没回」。
    # 额度的本意是「一个回合别刷多个会话」（出口.md §9），所以按
    # (kind, target) 这个**目的地槽位**计数：同一会话拆成 10 个气泡只占 1 格。
    slots = _SLOTS_PER_TURN.setdefault(turn_id, set())
    slot = (str(kind), str(target))
    if slot not in slots:
        if len(slots) >= max(0, int(max_items)):
            return Receipt(status="blocked", kind=kind, target=target, reason="over_limit")
        slots.add(slot)
    return None


def _send_once(adapter: Any, item: DeliverItem) -> Receipt:
    payload: dict[str, Any] = {"text": item.text}
    if item.file:
        payload["file"] = item.file
    try:
        receipt = adapter.send(item.kind, item.target, payload)
    except Exception as exc:  # noqa: BLE001 - 适配器炸了也要有一条回执，不许静默
        warn("core/speech/delivery.py:136 Exception", err=repr(exc))
        return unknown_execution_receipt(kind=item.kind, target=item.target, detail=f"{type(exc).__name__}: {exc}")
    if not isinstance(receipt, Receipt):
        return unknown_execution_receipt(kind=item.kind, target=item.target,
                                         detail=f"适配器返回了 {type(receipt).__name__}，不是 Receipt")
    return receipt


def _best_effort_ledger(ledger: LedgerHook | None, *, turn_id: str) -> LedgerHook | None:
    """旁注失败只留痕，不能覆盖已取得的投递结果。"""
    if ledger is None:
        return None

    def record(receipt: Receipt) -> None:
        try:
            ledger(receipt)
        except Exception as exc:
            warn("delivery_ledger_write_failed", turn_id=turn_id, target=receipt.target,
                 kind=receipt.kind, status=receipt.status, ack=receipt.ack, error=repr(exc)[:200])

    return record


def deliver(
    items: Sequence[DeliverItem],
    *,
    turn_id: str,
    dry_run: bool = False,
    registry: Any = None,
    platform: str | None = None,
    backoff_s: float = RETRY_BACKOFF_S,
    max_items: int = DELIVER_MAX_PER_TURN,
    ledger: LedgerHook | None = None,
) -> list[Receipt]:
    """把判交下来的投递清单发出去。**返回条数恒等于输入条数。**

    重试要求程序侧明确未执行证据，且属于 ``FAIL_RETRYABLE``；最多 1 次、退避 2s；**不许**因为失败改写内容重发。
    """
    ledger = _best_effort_ledger(ledger, turn_id=turn_id)
    adapter, caps, why = _resolve(registry, platform)
    platform_name = str(getattr(adapter, "platform", "") or platform or "")
    receipts: list[Receipt] = []

    for item in items:
        if why:
            # `_resolve` 说了不能发（注册表里没有 / 适配器没启用 / 自报能力失败）——
            # 这一条必须到此为止。**不许**因为「反正后面还有 dry_run 闸」就漏过去：
            # 一旦 dry_run=False，漏过去就是往一个没启用的适配器真发消息。
            blocked = Receipt(status="blocked", kind=str(item.kind or ""),
                              target=str(item.target or ""), ack="none", reason=why)
            receipts.append(_with_context(blocked, platform_name))
            if ledger is not None:
                ledger(receipts[-1])
            continue

        guard = _guard(item, turn_id=turn_id, caps=caps, adapter=adapter,
                       dry_run=dry_run, max_items=max_items)
        if guard is not None:
            receipt = _with_context(guard, platform_name)
            receipts.append(receipt)
            if ledger is not None:
                ledger(receipt)
            continue

        attempt = 0
        while True:
            attempt += 1
            receipt = _with_context(_send_once(adapter, item), platform_name)
            retryable = (receipt.status == "failed" and receipt.reason in FAIL_RETRYABLE and receipt.not_executed is True)
            if not retryable or attempt > RETRY_MAX:
                break
            time.sleep(max(0.0, float(backoff_s)))

        # 真的打到平台上了：记幂等键与回合计数
        key = (turn_id, str(item.kind or ""), str(item.target or ""), item.content_hash())
        _SENT[key] = receipt.status
        _SENT_PER_TURN[turn_id] = _SENT_PER_TURN.get(turn_id, 0) + 1
        receipts.append(receipt)
        if ledger is not None:
            ledger(receipt)

    return receipts


def _with_context(receipt: Receipt, platform_name: str) -> Receipt:
    """补上两样上下文：哪个适配器、什么时候。其余字段一律原样透传。"""
    if not receipt.adapter and platform_name:
        receipt = replace(receipt, adapter=platform_name)
    return receipt


#: 「在等批准」那条回执的 ``kind``：与 ``core/jobs/runner.approval_request()`` 同名同义。
APPROVAL_KIND = "plan"


def _delivery_mode() -> str:
    """唯一读点：``DELIVERY_CONTRACT`` 三态（off / shadow / on）。"""
    from core import features

    return features.mode(features.DELIVERY_CONTRACT, features.MODE_OFF)


def _ref_of(approval: Mapping[str, Any]) -> str:
    """`job-abc#2`：这条挂起的是哪件、哪一步（出口只透传，不解释）。"""
    job_id = str(approval.get("job_id", "") or "")
    if not job_id:
        return ""
    step_n = approval.get("step_n")
    return f"{job_id}#{step_n}" if step_n not in (None, "") else job_id


def pending_approval_receipt(approval: Mapping[str, Any]) -> Receipt:
    """把 ``core/jobs/runner.approval_request()`` 那条 dict 包成 ``Receipt``。

    **永不发送**：不经过 ``_resolve`` / ``_guard`` / ``_send_once``，函数签名里连
    registry 都没有——结构上就不可能把它发出去。状态恒为第六态 ``pending_approval``，
    ``ack="none"``、两个消息号都为空（"没发出去"与"发出去了"必须一眼可分）。
    """
    return Receipt(
        status=PENDING_APPROVAL,
        kind=str(approval.get("kind", "") or APPROVAL_KIND),
        target=str(approval.get("target", "") or ""),
        ack="none",
        reason="",
        platform_msg_id="",
        client_msg_id="",
        detail=str(approval.get("detail", "") or "")[:200],
        ref=_ref_of(approval),
    )


def deliver_pending_approvals(
    approvals: Sequence[Mapping[str, Any]],
    *,
    turn_id: str,
    ledger: LedgerHook | None = None,
) -> list[Receipt]:
    """出口消费「在等批准」：**只落回执，一个字节都不发**。

    * ``DELIVERY_CONTRACT=off``（默认）→ ``[]``，一行日志都不写（逐字节不变）；
    * ``shadow`` → 造回执 + 一条日志，**不调** ``ledger``（影子期不写任何账）；
    * ``on`` → 造回执 + 每条调一次 ``ledger``（回执表的唯一入口）。

    真正的「要不要我帮你订」不在这里：那是**判**在下一回合用普通 ``deliver`` 说的话
    （可以先 dry-run）。出口只把「挂起了、等谁批哪一步」记下来——第六态不许被当成已发出。
    """
    if not approvals:
        return []
    mode = _delivery_mode()
    if mode == "off":
        return []
    receipts = [pending_approval_receipt(a) for a in approvals]
    if mode == "shadow":
        log.info("pending_approval_shadow turn_id=%s count=%d", turn_id, len(receipts))
        return receipts
    for receipt in receipts:
        if ledger is not None:
            ledger(receipt)
    log.info("pending_approval_recorded turn_id=%s count=%d", turn_id, len(receipts))
    return receipts
