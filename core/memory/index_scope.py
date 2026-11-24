"""把已授权的索引快照绑定到生成它的读取身份；不重新解释来源权限。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
import json
from typing import Any, Callable

log = logging.getLogger(__name__)


def _coordinate_conflict(coordinates: dict, principal: Any) -> bool:
    """上下文里出现的坐标与已附身份矛盾时返回 True；缺失的坐标不算矛盾。"""
    for key, attr in (
        ("platform", "platform"),
        ("account_id", "bot_account_id"),
        ("chat_id", "chat_id"),
    ):
        given = str(coordinates.get(key) or "")
        actual = str(getattr(principal, attr, "") or "")
        if given and actual and given != actual:
            return True
    given_type = str(coordinates.get("chat_type") or "")
    actual_type = str(
        getattr(getattr(principal, "chat_type", ""), "value", getattr(principal, "chat_type", ""))
        or ""
    )
    if given_type and actual_type and given_type != actual_type:
        return True
    if getattr(principal, "is_group", False):
        given_group = str(coordinates.get("group_id") or "")
        actual_group = str(getattr(principal, "group_id", "") or "")
        if given_group and actual_group and given_group != actual_group:
            return True
    return False


def context_contradicts(context: Any, principal: Any) -> bool:
    """读记忆前的唯一坐标闸：上下文与已附身份矛盾 ⇒ 这次读不成立。"""
    if context is None or principal is None:
        return False
    from core.terminal.authority import context_coordinates

    return _coordinate_conflict(context_coordinates(context), principal)


def scope_key(context: Any, principal: Any = None) -> tuple | None:
    from core.terminal.authority import context_coordinates, context_principal

    if context is None:
        return None
    coordinates = context_coordinates(context)
    principal = principal if principal is not None else context_principal(context)
    from core.terminal.principal import PrincipalContext
    if not isinstance(principal, PrincipalContext):
        return None
    platform = str(coordinates.get("platform") or getattr(principal, "platform", "") or "")
    account = str(coordinates.get("account_id") or getattr(principal, "bot_account_id", "") or "")
    chat = str(coordinates.get("chat_id") or getattr(principal, "chat_id", "") or "")
    if not all((platform, account, chat)):
        return None
    # 已附身份与当前坐标矛盾时，不继续使用旧的身份授权。
    if _coordinate_conflict(coordinates, principal):
        return None
    if principal.is_group and not principal.group_id:
        return None
    # 固定完整授权快照；新增授权字段会自动参与绑定，而非扩充手工名单。
    return (platform, account, chat, coordinates.get("chat_type"), coordinates.get("sender_id"),
            coordinates.get("group_id"), replace(principal))


@dataclass(frozen=True)
class MemoryIndexSnapshot:
    binding: tuple | None
    payload: str
    validator: Callable[[dict], bool] | None = field(default=None, repr=False, compare=False)
    link_revision: int = -1

    def _link_is_current(self) -> bool:
        if self.link_revision < 0:
            return True
        try:
            from core.terminal import link_runtime

            current = link_runtime.current_link_revision()
        except Exception as exc:
            log.warning(
                "memory_index_link_revision_unavailable",
                extra={"err": repr(exc)[:160]},
            )
            return False
        return current == self.link_revision

    def matches(self, context: Any) -> bool:
        return (
            self._link_is_current()
            and self.binding is not None
            and self.binding == scope_key(context)
        )

    def entries_for(self, context: Any) -> list[dict]:
        if not self.matches(context):
            return []
        entries = json.loads(self.payload)
        # 只核对当前有效 ID，不用新正文替换已授权快照里的旧 key。
        from core.memory.beliefs import BeliefMemoryProvider
        from types import SimpleNamespace
        current = {}
        result = []
        for entry in entries:
            if self.validator is not None:
                try:
                    if not self.validator(entry):
                        continue
                except Exception as exc:
                    from safelog import warn
                    warn("memory_index_revalidation_failed", err=type(exc).__name__)
                    continue
            identifier = str(entry.get("memory_id") or "")
            if identifier.startswith("belief:"):
                at = str(entry.get("belief_as_of") or "")
                if at not in current:
                    active = BeliefMemoryProvider(verify_evidence=True).recall(self.binding[-1], limit=10**9, context=SimpleNamespace(extra={"belief_as_of": at}))
                    current[at] = {item.memory_id for item in active}
                if identifier not in current[at]:
                    continue
            result.append(entry)
        return result


    def is_current(self, context: Any) -> bool:
        return self.matches(context) and self.entries_for(context) == json.loads(self.payload)


def bind_index(context: Any, entries: list[dict], principal: Any = None, *, validator=None) -> None:
    revision = -1
    try:
        from core.terminal import link_runtime

        revision = link_runtime.current_link_revision()
    except Exception as exc:
        log.warning(
            "memory_index_link_revision_unavailable",
            extra={"err": repr(exc)[:160]},
        )
    context.memory_index_snapshot = MemoryIndexSnapshot(
        scope_key(context, principal),
        json.dumps(entries, ensure_ascii=False),
        validator,
        revision,
    )


def bound_index(context: Any) -> MemoryIndexSnapshot | None:
    snapshot = getattr(context, "memory_index_snapshot", None)
    return snapshot if isinstance(snapshot, MemoryIndexSnapshot) and snapshot.matches(context) else None
