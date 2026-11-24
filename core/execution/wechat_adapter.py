# -*- coding: utf-8 -*-
"""微信适配器：把「真发一条」包成出口认识的形状（`软件适配器.md` §2 / §7）。

一条投递 = 一次平台调用：`send(kind, target, payload) -> Receipt`。
这里**不重试、不改写内容**——重试与内容归出口管，适配器只做一次。

## 为什么它只是薄薄一层

真发的那套（切会话按顶栏验收、输入框验字、按回车、落盘等待、回读 server_id）
已经在 `core/execution/gui/wechat_send.py` 里真机跑通过了。这一层只做翻译：

| `send_and_verify` 的 status | 出口的 `status` × `ack` |
| --- | --- |
| `sent` | `sent` × `platform_id`（带回 `server_id`） |
| `unconfirmed` | `sent` × `unconfirmed`（发出去了，但库里还没行） |
| `failed` | `failed` × `error`（reason 从 detail 前缀翻成六类错误码之一） |
| `blocked` | `blocked` × `none`（开关关着 / 影子档，一次都没碰平台） |

## 三条纪律

1. **注册不等于启用**：`capabilities().enabled` 恒 False，只有人能 `registry.enable("wechat")`。
2. **不发就别报 sent**：拿不到 `server_id` 时 `ack` 只能是 `unconfirmed`，不许冒充平台号。
3. **只有 `say` 一种 kind**：文件/图片/表情还没有能真发的实现，一律 `unsupported_kind`。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core.contracts.delivery import Receipt, unknown_execution_receipt
from core.execution.adapters import AdapterCapabilities, AdapterRegistry
from safelog import warn
from core.infra.envflags import flag

PLATFORM = "wechat"
KIND_SAY = "say"
DETAIL_MAX_CHARS = 200

#: 结构化限流（不是人话）：一回合最多 2 条、一分钟最多 6 条。
RATE_LIMITS: dict[str, int] = {"per_turn": 2, "per_minute": 6}

#: `send_and_verify` 的 detail 前缀 -> 软件适配器.md §5 的六类错误码。
_REASON_BY_PREFIX: dict[str, str] = {
    "no_target": "target_not_found",
    "timeout": "network",
    "not_connected": "network",
    "fill_failed": "platform_error",
    "fill_unverified": "platform_error",
    "no_row_in_db": "platform_error",
    "press_failed": "platform_error",
}


def _reason_of(detail: str) -> str:
    """从 detail 里读首个 token，翻成六类错误码之一；认不出就是 platform_error。"""
    head = str(detail or "").split(":", 1)[0].strip().lower()
    return _REASON_BY_PREFIX.get(head, "platform_error")


class WechatAdapter:
    """微信 PC 的出口适配器（UI 通道 + 数据库回读）。"""

    platform = PLATFORM

    def __init__(self, send_fn: Callable[..., Any] | None = None) -> None:
        self._send_fn = send_fn

    # ── 能力自报 ──────────────────────────────────────────────────
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            platform=PLATFORM,
            kinds=frozenset({KIND_SAY}),
            targets=frozenset(),  # 空 = 不限制：任何会话都发得出去
            needs_auth=True,      # 账号态在客户端里，不在我们手上
            rate_limits=dict(RATE_LIMITS),
            enabled=False,        # 注册不等于启用
        )

    # ── 一次平台调用 ──────────────────────────────────────────────
    def send(self, kind: str, target: str, payload: Mapping[str, Any]) -> Receipt:
        kind_s = str(kind or "").strip().lower()
        target_s = str(target or "").strip()
        if kind_s != KIND_SAY:
            return Receipt(status="failed", kind=kind_s, target=target_s,
                           ack="error", reason="unsupported_kind",
                           detail=f"微信适配器只会发 {KIND_SAY}，收到 {kind_s or '(空)'}")
        if not target_s:
            return Receipt(status="failed", kind=kind_s, target="",
                           ack="error", reason="target_not_found", detail="没有目标会话")
        text = str(payload.get("text") or "")
        if not text.strip():
            return Receipt(status="skipped", kind=kind_s, target=target_s,
                           ack="none", reason="empty_content", detail="正文是空的")
        if payload.get("file"):
            return Receipt(status="failed", kind=kind_s, target=target_s,
                           ack="error", reason="unsupported_kind",
                           detail="文件投递还没有能真发的实现")

        try:
            result = self._send(target_s, text)
            return self._to_receipt(result, kind_s, target_s)
        except Exception as exc:  # noqa: BLE001 - 适配器炸了也要有一条回执
            warn("wechat_adapter_send_failed", err=repr(exc)[:160])
            return unknown_execution_receipt(kind=kind_s, target=target_s,
                                             detail=f"{type(exc).__name__}: {exc}")

    # ── 真发（唯一一条会按回车走到底的路径）───────────────────────
    def _send(self, target: str, text: str) -> Any:
        if self._send_fn is not None:
            return self._send_fn(target, text)
        return self._default_send(target, text)

    # ── 翻译 ─────────────────────────────────────────────────────
    def _default_send(self, target: str, text: str) -> Any:
        from core.execution.gui.wechat_send import send_and_verify

        return send_and_verify(target, text)

    def _to_receipt(self, result: Any, kind: str, target: str) -> Receipt:
        status = str(getattr(result, "status", "") or "").strip().lower()
        if status not in {"sent", "unconfirmed", "blocked", "failed"}:
            return unknown_execution_receipt(kind=kind, target=target, detail="适配器返回了未知状态")
        detail = str(getattr(result, "detail", "") or "")[:DETAIL_MAX_CHARS]
        local_id = getattr(result, "local_id", 0) or 0
        server_id = getattr(result, "server_id", 0) or 0
        base = dict(kind=kind, target=target, adapter=PLATFORM, detail=detail,
                    at=str(getattr(result, "at", "") or ""),
                    client_msg_id=f"local:{local_id}" if local_id else "",
                    ref=f"wechat#{local_id}" if local_id else "")
        if status == "sent":
            return Receipt(status="sent", ack="platform_id",
                           platform_msg_id=str(server_id or ""),
                           reason="" if server_id else "no_server_id", **base)
        if status == "unconfirmed":
            return Receipt(status="sent", ack="unconfirmed", reason="", **base)
        if status == "blocked":
            return Receipt(status="blocked", ack="none", reason="adapter_disabled", **base)
        return Receipt(status="failed", ack="error", reason=_reason_of(detail), **base)


_DEFAULT: AdapterRegistry | None = None


def build_registry(*, adapter: WechatAdapter | None = None, enable: bool = False) -> AdapterRegistry:
    """登记一个微信适配器；``enable=False`` 时**注册但不启用**（人才能启用）。"""
    registry = AdapterRegistry()
    registry.register(adapter or WechatAdapter())
    if enable:
        registry.enable(PLATFORM)
    return registry


#: 人才能启用出口适配器。刻意**不做**三态开关：这是"允许她真的替你发消息"，
#: 是一次运行期授权，不是一个实验档。默认关；设为 1/true/on/yes 才启用。
ADAPTER_ENABLE_ENV = "YINYUE_WECHAT_ADAPTER_ENABLE"


def adapter_enable_env() -> bool:
    """读 ``YINYUE_WECHAT_ADAPTER_ENABLE``（默认关）。"""
    import os

    return flag(ADAPTER_ENABLE_ENV)


def default_registry() -> AdapterRegistry:
    """进程内单例：出口影子档默认用它（未启用时出口一律 ``adapter_disabled``）。

    ``YINYUE_WECHAT_ADAPTER_ENABLE=1`` 时**直接以启用态**构建——这是"只给某些会话
    开新出口"能成立的最后一块：出口作用域（``YINYUE_EXIT_SCOPE_CHATS``）管"给谁"，
    这里管"准不准"。
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = build_registry(enable=adapter_enable_env())
    return _DEFAULT


def reset_default_registry() -> None:
    """测试用：丢掉进程内单例（下次调用重新按环境变量构建）。"""
    global _DEFAULT
    _DEFAULT = None
