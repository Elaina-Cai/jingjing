# -*- coding: utf-8 -*-
"""世界模型的进程内仓储（Step 9）。

N1 每轮读同一会话的 WorldModel、写回 scene_read 事务；跨重启快照留待后续接 DB。
纯内存 + 锁；不调用 LLM、不写生产库。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

from core.context.situation import WorldEvent, WorldModel, open_intentions
from safelog import warn
from core.infra.envflags import text
from core.infra.jsonio import atomic_write_json

__all__ = [
    "all_world_models",
    "get_world_model",
    "known_world_models",
    "load_world_model",
    "persist_world_model",
    "reset_world_models",
    "restore_world_model",
    "snapshot_world_model",
    "world_model_dir",
]

_MODELS: dict[Any, WorldModel] = {}
_LOCK = RLock()


def world_model_dir() -> Path | None:
    raw = text("YINYUE_WORLD_MODEL_DIR").strip()
    return Path(raw) if raw else None


def _coordinates(chat_id: Any, scope: Any = None) -> tuple[str, dict]:
    if isinstance(chat_id, tuple) and len(chat_id) == 3:
        platform, account, chat = chat_id
        bound = _scope_dict(scope) if scope is not None else {"platform": platform, "account_id": account, "chat_id": chat}
        if tuple(bound.get(k) for k in ("platform", "account_id", "chat_id")) != chat_id:
            raise ValueError("world_scope_mismatch")
        return str(chat), bound
    chat = str(chat_id or "")
    if scope is None:
        # 回合现场的坐标是隐式旧接口唯一可信的默认值。
        import sys
        ports = sys.modules.get("loop.ports")
        active = ports.current_read_scope() if ports and hasattr(ports, "current_read_scope") else None
        if active is not None and active.chat_id == chat:
            scope = active
    bound = _scope_dict(scope)
    if bound and bound.get("chat_id") != chat:
        raise ValueError("world_scope_mismatch")
    if bound and not all(bound.get(k) for k in ("platform", "account_id", "chat_id")):
        raise ValueError("scope_incomplete")
    return chat, bound


def _key(chat: str, scope: dict) -> Any:
    return tuple(scope[k] for k in ("platform", "account_id", "chat_id")) if scope else chat


def _model_path(chat_id: Any, *, scope: Any = None) -> Path | None:
    root = world_model_dir()
    if root is None:
        return None
    chat, bound = _coordinates(chat_id, scope)
    value = json.dumps(_key(chat, bound), ensure_ascii=False, separators=(",", ":")) if bound else chat
    digest = hashlib.sha256(value.encode()).hexdigest() if bound else hashlib.sha1(chat.encode()).hexdigest()[:16]
    return root / f"{digest}.json"


def _apply_cognitive_closures(model: WorldModel, chat_id: str) -> int:
    """把认知库里「已关闭」的意图叠到文件快照上（只读派生，不改盘）。

    关闭的唯一真源在认知事件日志；世界模型文件只保留开启与旧事件。加载时补一层
    派生事件，读侧（``open_intentions``）就与真源一致。派生事件带 ``_derived``
    标记，落盘时会被过滤，避免把同一条关闭又写回文件形成第二份记录。
    """
    try:
        from core.context.cognitive_commit import closed_states_for_scope
        bound = dict(getattr(model, "scope", {}) or {})
        states = closed_states_for_scope(bound)
        closed = frozenset(states)
        if not closed:
            return 0
        opened = {item.key for item in open_intentions(model)}
        applied = 0
        for key in sorted(closed & opened):
            model.add(
                "intention_closed",
                {"key": key, "result": "cognitive:close", "state": states[key], "_derived": True},
                at="",
            )
            applied += 1
        return applied
    except Exception as exc:  # noqa: BLE001 - 派生失败按「未关闭」处理
        warn("cognitive_overlay_failed", err=repr(exc)[:120])
        return 0


def get_world_model(chat_id: Any, *, scope: Any = None) -> WorldModel:
    chat, bound = _coordinates(chat_id, scope)
    with _LOCK:
        if not bound:
            candidates = {key: model for key, model in all_world_models().items() if (model.scope.get("chat_id") or (key if isinstance(key, str) else key[2])) == chat}
            if len(candidates) > 1:
                raise ValueError("ambiguous_world_scope")
            if candidates:
                model = next(iter(candidates.values()))
                bound = dict(model.scope)
        key = _key(chat, bound)
        if key in _MODELS:
            return _MODELS[key]
        model = load_world_model(chat, scope=bound) if bound else load_world_model(chat)
        if model is None:
            model = WorldModel(scope=bound)
        _apply_cognitive_closures(model, chat)
        _MODELS[key] = model
        return model


_SCOPE_FIELDS = ("platform", "account_id", "chat_id", "chat_type", "group_id")


def _scope_dict(scope: Any) -> dict[str, str]:
    """``Scope`` 对象或 dict → 只保留非空字段的纯 dict（可直接落盘）。"""
    if scope is None:
        return {}
    if isinstance(scope, dict):
        data = scope
    else:
        data = {name: getattr(scope, name, "") for name in _SCOPE_FIELDS}
    data = dict(data)
    if all(data.get(k) for k in ("platform", "account_id", "chat_id")) and not data.get("chat_type"):
        chat = str(data["chat_id"])
        data["chat_type"] = "group" if chat.endswith("@chatroom") or chat.startswith("group:") else "private"
    return {
        name: str(data.get(name) or "")
        for name in _SCOPE_FIELDS
        if str(data.get(name) or "")
    }


def _read_snapshot(path: Path) -> tuple[str, dict[str, str], list[dict[str, Any]]] | None:
    """读一份快照 → (chat_id, events)；坏文件/不认识的形状 → None。

    两种形状都认：
    - ``{"chat_id": ..., "scope": {...}, "events": [...]}``（scope 2026-11-07 起）；
    - ``[...]``（旧形状，读得出来但**反查不到 chat_id**——只有 caller 自己知道）。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 坏快照当没有
        warn("core/context/situation_store.py:71 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    if isinstance(payload, dict):
        events = payload.get("events")
        scope = payload.get("scope")
        return (
            str(payload.get("chat_id") or ""),
            dict(scope) if isinstance(scope, dict) else {},
            list(events or []),
        )
    if isinstance(payload, list):
        return "", {}, payload
    return None


def load_world_model(chat_id: Any, *, scope: Any = None) -> WorldModel | None:
    chat, bound = _coordinates(chat_id, scope)
    if not bound:
        candidates = [model for key, model in _disk_models().items() if (model.scope.get("chat_id") or key) == chat]
        if len(candidates) > 1:
            raise ValueError("ambiguous_world_scope")
        if candidates:
            bound = dict(candidates[0].scope)
    paths = [_model_path(chat, scope=bound)]
    if bound:
        # 仅继承明确归属于此坐标的旧快照；无归属的裸列表保持原样。
        root = world_model_dir()
        paths.append(root / (hashlib.sha1(chat.encode()).hexdigest()[:16] + ".json") if root else None)
    for path in paths:
        if path is None or not path.exists():
            continue
        read = _read_snapshot(path)
        if read is None:
            continue
        recorded, stored, events = read
        if recorded and recorded != chat:
            continue
        if bound and (not stored or _key(chat, stored) != _key(chat, bound)):
            continue
        return _build_model(events, scope=stored)
    return None


def persist_world_model(chat_id: Any, *, scope: Any = None) -> bool:
    model = get_world_model(chat_id, scope=scope)
    chat = str(model.scope.get("chat_id") or chat_id or "")
    path = _model_path(chat, scope=model.scope)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"chat_id": chat, "events": _snapshot(model)}
        if model.scope:
            payload["scope"] = dict(model.scope)
        atomic_write_json(path, payload)
        return True
    except Exception as exc:
        warn("world_snapshot_write_failed", chat=chat, err=repr(exc)[:120])
        return False


def _public_models(models: dict) -> dict:
    counts = {}
    for key, model in models.items():
        chat = model.scope.get("chat_id") or key
        counts[chat] = counts.get(chat, 0) + 1
    return {(model.scope.get("chat_id") or key) if counts[model.scope.get("chat_id") or key] == 1 else key: model for key, model in models.items()}


def known_world_models() -> dict:
    with _LOCK:
        return _public_models(dict(_MODELS))


def _disk_models() -> dict:
    root = world_model_dir()
    found = {}
    if root is None or not root.exists():
        return found
    # 新坐标文件比旧 16 位文件名后读取，优先保留新副本。
    for path in sorted(root.glob("*.json"), key=lambda p: (len(p.stem), p.name)):
        read = _read_snapshot(path)
        if read is None:
            continue
        chat, scope, events = read
        if not chat:
            continue
        found[_key(chat, scope)] = _build_model(events, scope=scope)
    return found


def world_models_on_disk() -> dict:
    return _public_models(_disk_models())


def all_world_models() -> dict:
    with _LOCK:
        merged = _disk_models()
        merged.update(_MODELS)
        return _public_models(merged)


def reset_world_models() -> None:
    with _LOCK:
        _MODELS.clear()


def _snapshot(model: WorldModel) -> list[dict]:
    return [{"kind": event.kind, "at": event.at, "payload": dict(event.payload or {})} for event in model.events if not (event.payload or {}).get("_derived")]


def snapshot_world_model(chat_id: Any, *, scope: Any = None) -> list[dict]:
    return _snapshot(get_world_model(chat_id, scope=scope))


def _build_model(
    events: list[dict[str, Any]] | None,
    *,
    scope: Any = None,
) -> WorldModel:
    """按快照造一个模型，**不注册进进程仓储**。

    单独抽出来的原因（2026-10-23 被守卫抓到）：`restore_world_model` 会写
    `_MODELS[key]`，如果盘上枚举也走它，就会把**活的进程内模型**（可能已经追加了
    新事件）换成盘上的旧快照——读一次"盘上有哪些会话"就把记忆回滚了。
    """
    model = WorldModel(scope=_scope_dict(scope))
    for item in events or ():
        try:
            kind = str(item.get("kind") or "")
            payload = item.get("payload") or {}
            if kind:
                model.add(kind, payload if isinstance(payload, dict) else {}, at=str(item.get("at") or ""))
        except Exception:  # noqa: BLE001 - 坏快照跳过，不阻断
            warn("core/context/situation_store.py:185 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
    return model


def restore_world_model(
    chat_id: Any,
    events: list[dict[str, Any]] | None,
    *,
    scope: Any = None,
) -> WorldModel:
    chat, bound = _coordinates(chat_id, scope)
    key = _key(chat, bound)
    model = _build_model(events, scope=bound)
    with _LOCK:
        _MODELS[key] = model
    return model
