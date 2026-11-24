"""Platform identity adapters.

Generic identity code must not know that WeChat groups end with ``@chatroom``
or that a WeChat sender id is called ``wxid``.  Those conventions live here,
one small adapter per platform.  Adding QQ/other platforms means adding an
entry (or an explicit adapter object), not editing ``repo_people`` or the
person-memory worker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

from core.contracts.events import ChatType

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlatformIdentityAdapter:
    platform: str
    identity_type: str
    group_suffixes: tuple[str, ...] = ()
    group_prefixes: tuple[str, ...] = ()
    group_nickname_type: str = "group_nickname"
    nickname_type: str = "nickname"


_BUILTIN_ADAPTERS: dict[str, PlatformIdentityAdapter] = {
    "wechat": PlatformIdentityAdapter(
        platform="wechat",
        identity_type="wxid",
        group_suffixes=("@chatroom",),
    ),
    "qq": PlatformIdentityAdapter(
        platform="qq",
        identity_type="qq_uin",
        group_prefixes=("group:",),
    ),
    "telegram": PlatformIdentityAdapter(
        platform="telegram",
        identity_type="telegram_user_id",
    ),
    "discord": PlatformIdentityAdapter(
        platform="discord",
        identity_type="discord_user_id",
    ),
    "dingtalk": PlatformIdentityAdapter(
        platform="dingtalk",
        identity_type="dingtalk_user_id",
    ),
    "feishu": PlatformIdentityAdapter(
        platform="feishu",
        identity_type="feishu_user_id",
    ),
}

_adapters: dict[str, PlatformIdentityAdapter] = dict(_BUILTIN_ADAPTERS)


def register_platform_adapter(adapter: PlatformIdentityAdapter) -> None:
    """Register or replace an adapter at runtime (useful for plugins/tests)."""

    name = str(adapter.platform or "").strip().lower()
    if not name:
        raise ValueError("platform adapter requires a platform name")
    _adapters[name] = adapter


def adapter_for(platform: str) -> PlatformIdentityAdapter:
    name = str(platform or "").strip().lower()
    if name in _adapters:
        return _adapters[name]
    fallback = name or "unknown"
    return PlatformIdentityAdapter(
        platform=fallback,
        identity_type=f"{fallback}_user_id",
    )


def identity_type_for(platform: str, raw_type: str = "") -> str:
    raw_type = str(raw_type or "").strip()
    if raw_type:
        return raw_type
    return adapter_for(platform).identity_type


def chat_type_for(
    platform: str,
    chat_type: str = "",
    chat_id: str = "",
) -> ChatType:
    """Normalize a chat type using canonical values first, adapter fallback second.

    Adapters should set canonical ``chat_type`` in their message payload.
    ``group_suffixes`` exists only as a compatibility fallback for legacy
    platforms like WeChat.
    """

    value = str(chat_type or "").strip().lower()
    if value in ("group", "chatroom", "group_chat"):
        return ChatType.GROUP
    if value in ("channel", "guild"):
        return ChatType.CHANNEL
    if value in ("private", "direct", "friend", "single", "dm"):
        return ChatType.PRIVATE

    chat_id = str(chat_id or "")
    adapter = adapter_for(platform)
    for suffix in adapter.group_suffixes:
        if suffix and chat_id.endswith(suffix):
            return ChatType.GROUP
    for prefix in adapter.group_prefixes:
        if prefix and chat_id.startswith(prefix):
            return ChatType.GROUP
    return ChatType.UNKNOWN


#: 「字段说的类型」与「id 形状推出来的类型」不一致的次数（进程内）。
#: 键是 ``(platform, declared, derived)``——同一个平台反复冲突只占一格。
_MISMATCHES: dict[tuple[str, str, str], int] = {}


def chat_type_checked(platform: str, chat_type: str = "", chat_id: str = "") -> ChatType:
    """字段优先、形状兜底；两者都说得清但不一致时**记一笔，不改判**。

    为什么不静默采信形状：``group_suffixes`` / ``group_prefixes`` 只是老平台的
    兼容兜底（见 ``adapter_for``），平台自己给的 ``chat_type`` 才是权威。反过来
    也不能静默采信字段——某个平台改了 id 形状而我们没更新 adapter 时，
    只有"记一笔"能让它可见（量具 ``chat_type_mismatch``）。
    """
    declared = chat_type_for(platform, chat_type, "")
    derived = chat_type_for(platform, "", chat_id)
    if (
        declared != ChatType.UNKNOWN
        and derived != ChatType.UNKNOWN
        and declared != derived
    ):
        key = (str(platform or ""), declared.value, derived.value)
        _MISMATCHES[key] = _MISMATCHES.get(key, 0) + 1
        log.warning(
            "chat_type_mismatch platform=%s chat_id=%s declared=%s shape=%s",
            platform, chat_id, declared.value, derived.value,
        )
    if declared != ChatType.UNKNOWN:
        return declared
    return derived


def mismatch_counts() -> dict[tuple[str, str, str], int]:
    """``chat_type_mismatch`` 的进程内计数（只读快照）。"""
    return dict(_MISMATCHES)


def reset_mismatches() -> None:
    """清空计数（测试用）。"""
    _MISMATCHES.clear()


def is_group_chat(platform: str, chat_type: str = "", chat_id: str = "") -> bool:
    return chat_type_for(platform, chat_type, chat_id) in (
        ChatType.GROUP,
        ChatType.CHANNEL,
    )
