from core.terminal.event_model import Event, EventType
from core.terminal.principal import (
    ChatType,
    PrincipalContext,
    RelationLevel,
    VisibilityScope,
)
from core.terminal.visibility import event_visible


def _group_event(platform: str, account: str, content: str, *, visibility=None):
    return Event.new(
        event_type=EventType.MESSAGE,
        platform=platform,
        bot_account_id=account,
        chat_id="room",
        chat_type="group",
        content=content,
        visibility_scope=visibility
        or frozenset({VisibilityScope.GROUP_MEMBERS}),
        metadata={"group_id": "room"},
    )


def _group_principal(platform: str, account: str) -> PrincipalContext:
    return PrincipalContext(
        platform,
        account,
        "room",
        ChatType.GROUP,
        "person-a",
        "A",
        RelationLevel.ORDINARY,
        group_id="room",
    )


def test_group_members_do_not_cross_platform():
    event = _group_event("qq", "bot_b", "qq 群正文")
    principal = _group_principal("wechat", "bot_a")
    assert event_visible(event, principal) is False


def test_group_members_do_not_cross_bot_account():
    event = _group_event("wechat", "bot_b", "另一个账号的群正文")
    principal = _group_principal("wechat", "bot_a")
    assert event_visible(event, principal) is False


def test_same_platform_and_account_group_member_still_visible():
    event = _group_event("wechat", "bot_a", "本群正文")
    principal = _group_principal("wechat", "bot_a")
    assert event_visible(event, principal) is True


def test_public_event_is_still_visible_across_platforms():
    event = _group_event(
        "qq",
        "bot_b",
        "公开正文",
        visibility=frozenset({VisibilityScope.PUBLIC}),
    )
    principal = _group_principal("wechat", "bot_a")
    assert event_visible(event, principal) is True


def test_owner_does_not_cross_platform_for_group_members():
    event = _group_event("qq", "bot_b", "qq 群正文")
    principal = PrincipalContext(
        "wechat",
        "bot_a",
        "room",
        ChatType.GROUP,
        "owner-a",
        "Owner",
        RelationLevel.OWNER,
        group_id="room",
    )
    assert event_visible(event, principal) is False
