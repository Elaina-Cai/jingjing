# -*- coding: utf-8 -*-
"""⑩ 数据通道（批 D Step D1）：状态落盘/恢复、记忆检索、人设读取的统一出口。

设计图：② ──状态落盘/恢复──▶ ⑩；③ ──记忆召回/检索──▶ ⑩；⑥ ──读人设──▶ ⑩。
⑩ 是**纯通道**：只做 1:1 转发，不缓存、不判断、不写业务逻辑；状态仍归②
（``core.context.situation_store``）、记忆仍归⑨（``core.memory``）、人设仍归
⑩ 的 persona 通道（``core.execution.persona_channel``）。

* 行为不变：调用点统一走本模块，语义与直接调底层函数逐字节相同（纯代理）；
* 不设新 flag（D1 无行为变化）：迁移回滚 = revert 对应提交；
* 转发不吞异常：底层怎么表现，通道就怎么表现（调用方原有 try/except 语义不变）。
"""
from __future__ import annotations

from typing import Any
from safelog import warn

__all__ = [
    "CHANNEL_OWNERS",
    "CHANNELS",
    "channel_digest",
    "get_world",
    "known_worlds",
    "load_world",
    "persona_channel_mode",
    "persona_digest",
    "persona_note",
    "principal_for_context",
    "read_persona",
    "recall_for_context",
    "recall_hub_items",
    "recall_items_for_context",
    "render_hits",
    "save_world",
    "snapshot_world",
]

#: 本通道负责的三个数据面（供审计/文档/静态测试读，不参与运行判断）。
CHANNELS: dict[str, tuple[str, ...]] = {
    "state": (
        "get_world",
        "known_worlds",
        "save_world",
        "load_world",
        "snapshot_world",
    ),
    "retrieval": (
        "recall_items_for_context",
        "recall_for_context",
        "render_hits",
    ),
    "persona": (
        "read_persona",
        "persona_note",
        "persona_digest",
        "persona_channel_mode",
    ),
}

#: 每个数据面背后的真实 owner（⑩ 只是入口）。
CHANNEL_OWNERS: dict[str, str] = {
    "state": "core.context.situation_store",
    "retrieval": "core.memory.recall_router",
    "persona": "core.execution.persona_channel",
}


def channel_digest() -> dict[str, Any]:
    """通道结构摘要（名字/owner/入口函数），给文档、审计与静态测试用。"""
    return {
        "channels": {
            name: {
                "owner": CHANNEL_OWNERS.get(name, ""),
                "entries": list(entries),
            }
            for name, entries in CHANNELS.items()
        },
        "entries": sum(len(entries) for entries in CHANNELS.values()),
    }


# ---------------------------------------------------------------------------
# ② 状态落盘/恢复（owner: core.context.situation_store）
# ---------------------------------------------------------------------------


def get_world(chat_id: Any, *, scope: Any = None) -> Any:
    """取（不存在则建）会话世界模型；语义与 store.get_world_model 一致。"""
    from core.context.situation_store import get_world_model

    return get_world_model(chat_id, scope=scope)


def known_worlds() -> dict[str, Any]:
    """进程内已知世界模型浅拷贝；语义与 store.known_world_models 一致。"""
    from core.context.situation_store import known_world_models

    return known_world_models()


def save_world(chat_id: Any, *, scope: Any = None) -> bool:
    """把会话世界模型落盘（原子写）；语义与 store.persist_world_model 一致。"""
    from core.context.situation_store import persist_world_model

    return persist_world_model(chat_id, scope=scope)


def load_world(chat_id: Any, *, scope: Any = None) -> Any:
    """从盘上恢复会话世界模型（没有/读不动返回 None）；语义与 store 同名函数一致。"""
    from core.context.situation_store import load_world_model

    return load_world_model(chat_id, scope=scope)


def snapshot_world(chat_id: Any, *, scope: Any = None) -> list[dict[str, Any]]:
    """世界模型事件快照（kind/at/payload）；语义与 store 同名函数一致。"""
    from core.context.situation_store import snapshot_world_model

    return snapshot_world_model(chat_id, scope=scope)


# ---------------------------------------------------------------------------
# ⑨ 记忆检索（owner: core.memory.recall_router；只读，缺 principal 一律空）
# ---------------------------------------------------------------------------


def recall_items_for_context(
    context: Any,
    query: str = "",
    *,
    kinds: Any = None,
    limit: int = 8,
    hub: Any = None,
) -> list[Any]:
    """多类记忆召回；语义与 recall_router.recall_items_for_context 一致。"""
    from core.memory.recall_router import recall_items_for_context as _recall

    return _recall(context, query, kinds=kinds, limit=limit, hub=hub)


def recall_for_context(
    context: Any,
    query: str = "",
    *,
    limit: int = 5,
    gateway: Any = None,
) -> list[Any]:
    """自动长时记忆召回；语义与 recall_router.recall_for_context 一致。"""
    from core.memory.recall_router import recall_for_context as _recall

    return _recall(context, query, limit=limit, gateway=gateway)


def render_hits(
    hits: Any,
    *,
    header: str = "[事件记忆·仅作当前对话参考]",
    max_chars: int = 1200,
) -> str:
    """把召回结果渲染成提示词片段；语义与 recall_router.render_hits 一致。"""
    from core.memory.recall_router import render_hits as _render

    return _render(hits, header=header, max_chars=max_chars)


def principal_for_context(
    context: Any,
    *,
    platform: str = "",
    account_id: str = "",
    chat_id: str = "",
    chat_type: str = "",
    sender_id: str = "",
    sender_name: str = "",
) -> Any:
    """解出当前回合的身份（已 attach 的优先，其次用给定坐标现算；解不出返回 None）。

    语义与 recall_router._principal_for_context 一致，但**只用本次坐标**：
    绝不 attach（attach 会写 context.principal 与 extra 身份键，破坏 ③ 的 shadow
    零 diff，2026-10-10 实测打红三个 ownership 用例）。身份判定仍归
    ``core.terminal.authority``，本函数只是它的惰加载壳。
    """
    try:
        from core.terminal.authority import active_context_principal

        principal = active_context_principal(context)
    except Exception:  # noqa: BLE001 - 解析失败按"没挂身份"处理
        warn("core/execution/data_channels.py:186 Exception", err=repr(__import__("sys").exc_info()[1]))
        principal = None
    if principal is not None:
        return principal
    coords = {
        "platform": str(platform or ""),
        "account_id": str(account_id or ""),
        "chat_id": str(chat_id or ""),
        "chat_type": str(chat_type or ""),
        "sender_id": str(sender_id or ""),
        "sender_name": str(sender_name or ""),
    }
    coords = {key: value for key, value in coords.items() if value}
    if not coords.get("platform"):
        return None
    try:
        from core.terminal.authority import active_principal

        return active_principal(**coords)
    except Exception:  # noqa: BLE001 - authority 失败按"没挂身份"处理
        warn("core/execution/data_channels.py:205 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def recall_hub_items(
    principal: Any,
    query: str = "",
    *,
    kinds: Any = None,
    limit: int = 8,
    per_kind: Any = None,
    context: Any = None,
    detail: Any = None,
) -> list[Any]:
    """按需记忆召回：直连 MemoryHub（owner: core.memory.hub；只读）。

    ``recall_items_for_context`` 是"给了 context 自己解身份"的便利面，
    本函数是"身份已经解好了"的 explicit 面——⑥ 的 memory_recall 工具、
    ③ 的 needs_memory 召回都用它。principal 为空一律空手而归。
    """
    if principal is None:
        return []
    if context is not None:
        from core.memory.index_scope import context_contradicts

        # 同一次读不能借旧身份越界：坐标与已附身份矛盾就不取数。
        if context_contradicts(context, principal):
            return []
    from core.memory.hub import get_memory_hub

    return get_memory_hub().recall(
        principal,
        str(query or ""),
        kinds=kinds,
        limit=max(1, int(limit or 1)),
        per_kind=per_kind,
        context=context,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# ⑩ 人设读取（owner: core.execution.persona_channel；纯只读）
# ---------------------------------------------------------------------------


def read_persona(repo: Any = None, *, persona_id: Any = None) -> Any:
    """读持久人设视图；语义与 persona_channel.read_persona 一致。"""
    from core.execution.persona_channel import read_persona as _read

    return _read(repo, persona_id=persona_id)


def persona_note(view: Any, *, limit: int = 80) -> str:
    """规划上下文用的人设摘要；语义与 persona_channel.persona_note 一致。"""
    from core.execution.persona_channel import persona_note as _note

    return _note(view, limit=limit)


def persona_digest(view: Any, *, limit: int = 80) -> dict[str, Any]:
    """审计/回放用的人设载荷；语义与 persona_channel.persona_digest 一致。"""
    from core.execution.persona_channel import persona_digest as _digest

    return _digest(view, limit=limit)


def persona_channel_mode() -> str:
    """人设通道三态 flag；语义与 persona_channel.persona_channel_mode 一致。"""
    from core.execution.persona_channel import persona_channel_mode as _mode

    return _mode()
