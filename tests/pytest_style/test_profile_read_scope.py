from dataclasses import replace
import asyncio
from types import SimpleNamespace

import pytest

from core.contracts.events import ChatType
from core.contracts.person_memory import PersonContextRequest, PersonObservation, PersonProfileSnapshot, ProfileScopeType
from core.people.context import PersonContextBuilder


class Repository:
    def __init__(self):
        self.seen = []
        self.rows = []
        self.profile = PersonProfileSnapshot("global", "target", "wechat", "bot", ProfileScopeType.GLOBAL, "", summary="跨场景全局摘要")

    def get_profile(self, person, **kwargs):
        self.seen.append(kwargs)
        return self.profile if self.profile.scope_type == kwargs["scope_type"] else None

    def list_for_person(self, person, **kwargs):
        self.viewer = kwargs["scope"].requester_person_id
        return self.rows


def request(**kwargs):
    values = dict(platform="wechat", account_id="bot", chat_type=ChatType.PRIVATE, chat_id="wxid_a", target_person_id="target")
    values.update(kwargs)
    return PersonContextRequest(**values)


@pytest.mark.parametrize("group,owner", [(False, False), (True, False), (True, True)])
def test_unscoped_summary_not_used_outside_owner_private(group, owner):
    repo = Repository()
    ctx = PersonContextBuilder(repo, repo).build(request(chat_type=ChatType.GROUP if group else ChatType.PRIVATE, viewer_is_owner=owner))
    assert ctx.profile is None and "跨场景全局摘要" not in ctx.prompt_block


def test_owner_private_can_use_global_profile():
    repo = Repository()
    ctx = PersonContextBuilder(repo, repo).build(request(viewer_is_owner=True))
    assert ctx.profile is not None and "跨场景全局摘要" in ctx.prompt_block


def test_unknown_viewer_does_not_become_the_subject():
    repo = Repository()
    repo.rows = [PersonObservation("obs", "target", "wechat", "bot", ChatType.GROUP, "other_group", content="只允许本人私聊查看的群经历")]
    ctx = PersonContextBuilder(repo, repo).build(request())
    assert repo.viewer == ""
    assert not ctx.observations
    assert "只允许本人" not in ctx.prompt_block


def test_scoped_profile_and_current_chat_observation_still_work():
    repo = Repository()
    repo.profile = replace(repo.profile, scope_type=ProfileScopeType.PRIVATE, scope_id="wxid_a", summary="本会话摘要")
    repo.rows = [PersonObservation("obs", "target", "wechat", "bot", ChatType.PRIVATE, "wxid_a", content="本会话记忆")]
    ctx = PersonContextBuilder(repo, repo).build(request())
    assert "本会话摘要" in ctx.prompt_block and "本会话记忆" in ctx.prompt_block


def test_profile_repository_cannot_return_another_tenant():
    repo = Repository()
    repo.profile = replace(repo.profile, scope_type=ProfileScopeType.PRIVATE, scope_id="wxid_a", account_id="other")
    ctx = PersonContextBuilder(repo, repo).build(request())
    assert ctx.profile is None


def test_missing_coordinates_do_not_query_profile():
    repo = Repository()
    ctx = PersonContextBuilder(repo, repo).build(request(account_id=""))
    assert not repo.seen and ctx.profile is None


def test_profile_tool_cannot_change_tenant_before_query(monkeypatch):
    from agents.tools.person_profile import GetPersonProfileTool
    tool = GetPersonProfileTool()
    tool.set_context(SimpleNamespace(extra={"platform": "wechat", "account_id": "bot", "chat_id": "wxid_a"}))
    monkeypatch.setattr(tool, "_meta", lambda: (_ for _ in ()).throw(AssertionError("不能查询其他租户")))
    result = asyncio.run(tool.execute(person="A", account_id="other"))
    assert result.error == "profile_scope_denied: 参数不能更换读取账号"


def test_profile_tool_viewer_resolution_is_read_only(monkeypatch):
    import agents.tools.person_profile as module
    repo = Repository()
    writes = []
    people = SimpleNamespace(
        find_persons=lambda *a, **kw: [SimpleNamespace(person_id="target", display_name="A", platform="wechat", account_id="bot")],
        get_identity=lambda *a, **kw: None,
        resolve_identity=lambda **kw: writes.append(kw))
    tool = module.GetPersonProfileTool(meta_repo=SimpleNamespace(engine=object(), people=people))
    tool.set_context(SimpleNamespace(extra={"platform": "wechat", "account_id": "bot", "chat_id": "wxid_a", "sender_id": "a"}))
    monkeypatch.setattr(module, "PersonMemoryRepository", lambda engine: repo)
    monkeypatch.setattr(tool, "authoritative_is_owner", lambda: False)
    result = asyncio.run(tool.execute(person="A"))
    assert not result.error and repo.viewer == ""
    assert not writes, "查询画像不得调用身份创建接口"


def test_owner_principal_from_another_chat_cannot_unlock_profile(monkeypatch):
    import agents.tools.person_profile as module
    from core.terminal.principal import PrincipalContext, RelationLevel
    repo = Repository()
    people = SimpleNamespace(
        find_persons=lambda *a, **kw: [SimpleNamespace(person_id="target", display_name="A", platform="wechat", account_id="bot")],
        get_identity=lambda *a, **kw: None)
    tool = module.GetPersonProfileTool(meta_repo=SimpleNamespace(engine=object(), people=people))
    principal = PrincipalContext("wechat", "bot", "wxid_owner", ChatType.PRIVATE, "owner", "主人", RelationLevel.OWNER)
    tool.set_context(SimpleNamespace(extra={"platform": "wechat", "account_id": "bot", "chat_id": "wxid_a"}, principal=principal))
    monkeypatch.setattr(module, "PersonMemoryRepository", lambda engine: repo)
    monkeypatch.setattr(tool, "authoritative_is_owner", lambda: True)
    result = asyncio.run(tool.execute(person="A"))
    assert "跨场景全局摘要" not in result.output
