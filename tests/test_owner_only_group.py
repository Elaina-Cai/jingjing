"""owner_only 记忆不泄漏进群上下文（2026-09-22 修复的回归）。

根因：`is_owner` 被当成"主人私聊上下文"，主人在群里发言时 owner_only 记忆
被召回/注入/复述，泄漏给群成员。修复后：owner_only/issuer_only/self 三类私密
scope 在 `chat_type=group` 时一律不可见，即使说话的是主人。
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.terminal.principal import (
    ChatType,
    PrincipalContext,
    RelationLevel,
    VisibilityScope,
)


def _owner(chat_type: ChatType, group_id: str = "") -> PrincipalContext:
    memory_scopes = (
        frozenset({VisibilityScope.GROUP_MEMBERS})
        if chat_type == ChatType.GROUP
        else frozenset({VisibilityScope.OWNER_ONLY, VisibilityScope.COMPONENT})
    )
    return PrincipalContext(
        platform="wechat",
        bot_account_id="wxid_seed02",
        chat_id=group_id or ("owner" if chat_type == ChatType.PRIVATE else group_id),
        chat_type=chat_type,
        person_id="owner",
        display_name="主人",
        relation_to_bot=RelationLevel.OWNER,
        group_id=group_id,
        memory_scopes=memory_scopes,
    )


def _item(scope: str) -> SimpleNamespace:
    return SimpleNamespace(
        visibility_scope=(scope,),
        metadata={},
        platform="wechat",
        bot_account_id="wxid_seed02",
        linked_component_id="",
        subject_person_ids=(),
    )


def test_owner_only_not_visible_in_group() -> None:
    from core.terminal.memory_gateway import MemoryGateway

    gw = MemoryGateway()
    assert gw._projection_allowed(_item("owner_only"), _owner(ChatType.GROUP, "g1")) is False


def test_owner_only_visible_in_private() -> None:
    from core.terminal.memory_gateway import MemoryGateway

    gw = MemoryGateway()
    assert gw._projection_allowed(_item("owner_only"), _owner(ChatType.PRIVATE)) is True


def test_owner_still_sees_group_members_in_group() -> None:
    from core.terminal.memory_gateway import MemoryGateway

    gw = MemoryGateway()
    item = _item("group_members")
    item.metadata = {"chat_id": "g1"}
    assert gw._projection_allowed(item, _owner(ChatType.GROUP, "g1")) is True


def test_event_visible_blocks_owner_only_in_group() -> None:
    from datetime import datetime

    from core.terminal.event_model import Event, EventType
    from core.terminal.visibility import event_visible

    ev = Event(
        event_id="e1",
        occurred_at=datetime(2026, 9, 22, 17, 0),
        event_type=EventType.MEMORY_WRITE,
        visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
    )
    assert event_visible(ev, _owner(ChatType.GROUP, "g1")) is False
    assert event_visible(ev, _owner(ChatType.PRIVATE)) is True


def test_group_scope_does_not_cross_groups() -> None:
    """本群的事只能在本群用：另一个群的 group_members 记忆不许出现在这里。"""
    from core.terminal.memory_gateway import MemoryGateway

    gw = MemoryGateway()
    item = _item("group_members")
    item.metadata = {"chat_id": "g2"}  # 记忆是在 g2 产生的
    assert gw._projection_allowed(item, _owner(ChatType.GROUP, "g1")) is False


def test_group_scope_needs_a_group_id() -> None:
    """群内但 principal 没有 group_id → 无从比对，判不可见（不猜）。"""
    from core.terminal.memory_gateway import MemoryGateway

    gw = MemoryGateway()
    item = _item("group_members")
    item.metadata = {"chat_id": ""}
    # PrincipalContext 是 frozen dataclass：改字段必须 replace，不能赋值
    principal = replace(_owner(ChatType.GROUP, "g1"), group_id="")
    assert gw._projection_allowed(item, principal) is False


def test_public_visible_everywhere() -> None:
    """public 是唯一"哪都能用"的档，群与私聊都放行。"""
    from core.terminal.memory_gateway import MemoryGateway

    gw = MemoryGateway()
    assert gw._projection_allowed(_item("public"), _owner(ChatType.GROUP, "g1")) is True
    assert gw._projection_allowed(_item("public"), _owner(ChatType.PRIVATE)) is True


def test_projection_chat_scope_only_guards_groups() -> None:
    """私聊不按群口径过滤（否则私聊里替主人记的事会全被挡掉）。"""
    from core.terminal.memory_gateway import MemoryGateway

    gw = MemoryGateway()
    item = _item("owner_only")
    assert gw._projection_chat_scope_ok(item, _owner(ChatType.PRIVATE)) is True


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
