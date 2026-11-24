# -*- coding: utf-8 -*-
"""出口的契约（**只放类型，不放实现**）。

出处：`docs/decisions/出口.md` §2 / §5 / §14，`docs/decisions/软件适配器.md` §2。
实现落 `core/speech/delivery.py`（U-04）。本文件不许出现 I/O、不许 import 适配器。

三种词汇的关系（出口.md §5 与 §14-2 的裁决口径）：
- ``Status``  = **投递结果**：她想发的东西最后怎么了（**六态**，不许有第七种）；
  ``pending_approval`` 是 v0.4 新增的第六态：动作需要人批准（花钱 / 不可逆 / 涉隐私），
  出口**一个字节都不发**，挂起等 `approval`（见 `docs/decisions/办_在手的事.md` §6）。
- ``Ack``     = **平台回执**：对面到底回没回一个号（四值，与 Status 正交）。
「没发出去」（skipped / blocked）与「发出去了但平台没回号」（sent + unconfirmed）
是两种完全不同的故障，不能挤进同一列。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal, Mapping, Protocol, Sequence, runtime_checkable

# 出口.md §5（v0.4）：六种结果，不许有第七种；**没有「沉默地丢掉」这个选项**
Status = Literal["sent", "skipped", "blocked", "failed", "partial", "pending_approval"]
STATUSES: tuple[Status, ...] = (
    "sent", "skipped", "blocked", "failed", "partial", "pending_approval",
)
# 需要人批准、且尚未批准的状态（出口不许把它当失败，也不许重投）
PENDING_APPROVAL: Status = "pending_approval"

# 出口.md §14-2：平台无回执怎么计（三态 + 未调用）
Ack = Literal["platform_id", "unconfirmed", "error", "none"]
ACKS: tuple[Ack, ...] = ("platform_id", "unconfirmed", "error", "none")

# 出口.md §5：没发出去必须带 reason（名字，不许自由文本）
SKIP_REASONS: tuple[str, ...] = (
    "dry_run",       # DRY_RUN_SEND 打开 / 影子模式
    "no_target",     # 判没给 target
    "empty_content", # 正文与文件都空
    "rate_limited",  # 限流器不放行
    "duplicate",     # 幂等键命中：这一回合已经发过同一条
)
BLOCK_REASONS: tuple[str, ...] = (
    "adapter_disabled",        # 适配器未人工启用（软件适配器.md §6）
    "kind_not_registered",     # kind 不在注册表
    "target_not_found",        # 目标会话/联系人不存在
    "not_in_whitelist",        # 白名单外
    "external_send_via_exit",  # 工具包想直发 → 一律走出口（工具包.md §4）
    "over_limit",              # 单回合超过两条（出口.md §9）
)

#: 零宽字符：算 content_hash 时要剥掉，否则同一句话能被当成两条（出口.md §14-5）
ZERO_WIDTH: tuple[str, ...] = ("\u200b", "\u200c", "\u200d", "\ufeff")

# 出口.md §5：failed 只在 retryable 时重试，最多 1 次
FAIL_RETRYABLE: tuple[str, ...] = ("network", "rate_limited", "platform_error")


@dataclass(frozen=True)
class DeliverItem:
    """判交下来的一条投递（`deliver[]` 的元素）。"""

    kind: str
    target: str = ""
    text: str = ""
    file: str | None = None
    basis: tuple[str, ...] = ()

    def content_hash(self) -> str:
        """幂等键的一半：**只算正文**，剥掉前导 `@` 与零宽字符（出口.md §14-5）。

        故意不含 ``target`` / ``basis``：同一句话换个收件人仍是「另一条投递」，
        由幂等键的其它列区分；这里只回答「这句话是不是上次那句」。
        """
        text = str(self.text or "").lstrip()
        while text.startswith("@"):
            text = text[1:].lstrip()
        for ch in ZERO_WIDTH:
            text = text.replace(ch, "")
        payload = text + "|" + str(self.file or "")
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Receipt:
    """一次投递的结果。成功失败都要有一条（出口.md §5 / §7）。"""

    status: Status
    kind: str
    target: str = ""
    ack: Ack = "none"
    reason: str = ""
    platform_msg_id: str = ""
    client_msg_id: str = ""
    adapter: str = ""
    detail: str = ""
    at: str = ""
    #: 这条回执指向的对象（如 `job-abc#2`）。出口不改写它，只透传——没有它，
    #: 「这条挂起的是哪一步」只能从 `detail` 里抠字。
    ref: str = ""

    #: 仅由发送前程序校验提供；异常或错误码不能证明未执行。
    not_executed: bool = False

    def is_delivered(self) -> bool:
        """重复发送率的口径：`sent`（含 unconfirmed）与 `partial` 都算「已发出」。"""
        return self.status in ("sent", "partial")


def unknown_execution_receipt(*, kind: str, target: str, detail: str = "") -> Receipt:
    """调用可能已产生外部副作用；不宣称失败、送达或可重试。"""
    return Receipt(status="partial", kind=kind, target=target, ack="unconfirmed",
                   reason="execution_unknown", detail=("执行结果未知，可能已发送；不重发。" + str(detail))[:200])


@runtime_checkable
class DeliveryPort(Protocol):
    """出口的实现形状（U-04：`core/speech/delivery.py::deliver`）。"""

    def deliver(
        self,
        items: Sequence[DeliverItem],
        *,
        turn_id: str,
        dry_run: bool = False,
    ) -> list[Receipt]:
        """返回条数必须与 ``items`` 等长：**不许少返回一条**（没有静默丢弃）。"""
        ...
