# -*- coding: utf-8 -*-
"""平台适配器的契约（类型 + 注册表实现）。

出处：`docs/decisions/软件适配器.md` §2 / §5 / §6 / §7。
本文件**不做 I/O**：真适配器在 `core/execution/<platform>_adapter.py`（U-05 后续）。

一次投递 = 调一个 adapter（软件适配器.md §2）。出口不猜平台：
能不能发，先问 ``registry.check(platform, kind, target)``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from core.contracts.delivery import Receipt


class ErrorCode(str, Enum):
    """软件适配器.md §5：六类错误码，不许有第七种。"""

    AUTH_EXPIRED = "auth_expired"
    RATE_LIMITED = "rate_limited"
    TARGET_NOT_FOUND = "target_not_found"
    UNSUPPORTED_KIND = "unsupported_kind"
    PLATFORM_ERROR = "platform_error"
    NETWORK = "network"


#: 只有这三类值得重试（且最多 1 次、退避 2s）；auth 过期与「能力没有」重试没有意义
RETRYABLE: frozenset[ErrorCode] = frozenset(
    {ErrorCode.RATE_LIMITED, ErrorCode.PLATFORM_ERROR, ErrorCode.NETWORK}
)


@dataclass(frozen=True)
class AdapterCapabilities:
    """适配器自报「能发什么、发给谁、要什么权限」（软件适配器.md §2）。

    ``targets`` 不是装饰：出口校验①要按 target 判定「发得出去吗」。
    ``rate_limits`` 必须是结构化数字（不是 ``rate_hint: str`` 这种人话），
    否则出口拿它做不了判定。
    """

    platform: str
    kinds: frozenset[str] = frozenset()
    targets: frozenset[str] = frozenset()
    needs_auth: bool = True
    rate_limits: Mapping[str, int] = field(default_factory=dict)
    enabled: bool = False  # 软件适配器.md §6：新 adapter 默认 disabled，由人打开


@runtime_checkable
class Adapter(Protocol):
    """适配器的形状。``send`` 只做一次平台调用，不重试、不改写内容。"""

    platform: str

    def capabilities(self) -> AdapterCapabilities:
        ...

    def send(self, kind: str, target: str, payload: Mapping[str, Any]) -> Receipt:
        ...


class AdapterRegistry:
    """声明式注册表 + 人工启用（软件适配器.md §6）。

    两条纪律：
    - 注册**不等于**启用：新适配器一律 disabled，由人 ``enable()``；
    - 注册时留一份能力快照，之后自报的 ``kinds``/``targets`` 变了要能看见
      （``capability_drift()``），不许悄悄漂移。
    """

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}
        self._enabled: set[str] = set()
        self._snapshot: dict[str, tuple[frozenset[str], frozenset[str]]] = {}

    # ── 注册 / 取 ────────────────────────────────────────────────
    def register(self, adapter: Adapter) -> None:
        """注册即入表，但**不启用**：``enabled`` 保持 False，由人打开。"""
        name = str(getattr(adapter, "platform", "") or "").strip()
        if not name:
            raise ValueError("adapter.platform 不能为空")
        caps = adapter.capabilities()
        self._adapters[name] = adapter
        self._snapshot[name] = (
            frozenset(caps.kinds or ()),
            frozenset(caps.targets or ()),
        )

    def get(self, platform: str) -> Adapter | None:
        return self._adapters.get(str(platform or "").strip())

    def platforms(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    # ── 启用 / 停用（只有人能改）──────────────────────────────────
    def enable(self, platform: str) -> tuple[bool, str]:
        name = str(platform or "").strip()
        if name not in self._adapters:
            return (False, "adapter_disabled")
        self._enabled.add(name)
        return (True, "")

    def disable(self, platform: str) -> tuple[bool, str]:
        name = str(platform or "").strip()
        self._enabled.discard(name)
        return (True, "")

    def is_enabled(self, platform: str) -> bool:
        return str(platform or "").strip() in self._enabled

    # ── 出口校验① ────────────────────────────────────────────────
    def check(self, platform: str, kind: str, target: str) -> tuple[bool, str]:
        """返回 ``(ok, reason)``，reason 取自 ``core.contracts.delivery.BLOCK_REASONS``。"""
        name = str(platform or "").strip()
        adapter = self._adapters.get(name)
        if adapter is None:
            return (False, "adapter_disabled")
        if name not in self._enabled:
            return (False, "adapter_disabled")
        caps = adapter.capabilities()
        kinds = frozenset(caps.kinds or ())
        targets = frozenset(caps.targets or ())
        if kinds and str(kind) not in kinds:
            return (False, "kind_not_registered")
        if targets and str(target) not in targets:
            return (False, "target_not_found")
        return (True, "")

    # ── 能力漂移 ────────────────────────────────────────────────
    def capability_drift(self) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
        """自报 ``kinds``/``targets`` 与注册时不一致的适配器（需人确认）。"""
        out: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        for name, adapter in self._adapters.items():
            base_kinds, base_targets = self._snapshot.get(name, (frozenset(), frozenset()))
            caps = adapter.capabilities()
            kinds = frozenset(caps.kinds or ())
            targets = frozenset(caps.targets or ())
            if kinds != base_kinds or targets != base_targets:
                out[name] = (kinds, targets)
        return out
