"""P3 地基：世界模型自己的会话坐标（platform/account/chat/chat_type）。

没有这一步，其余关闭路径只能拿到 model、拿不到身份，写不了「属于哪个会话」的
完成事实。这里钉住四件事：
1. 坐标能落盘、能恢复；
2. 旧快照（无 scope）照常读，不因升级丢历史；
3. 首次绑定后不被第二套坐标覆盖（同名 chat_id 的历史限制，静默改写更坏）；
4. 坐标里的 chat_id 与模型键不一致时拒绝绑定。
"""
import json
import os

import pytest

from core.context import situation
from core.context import situation_store as store
from core.context.cognitive_commit import Scope


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    # 认知库指到不存在的临时路径：派生查询直接返回空，绝不碰生产库。
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    store.reset_world_models()
    yield
    store.reset_world_models()


def _scope(chat="c1"):
    return Scope("wechat", "bot", chat, "private", "")


def _scope_dict(chat="c1"):
    return {
        "platform": "wechat",
        "account_id": "bot",
        "chat_id": chat,
        "chat_type": "private",
    }


def test_scope_is_persisted_and_restored():
    model = store.get_world_model("c1", scope=_scope())
    assert model.scope == _scope_dict()
    assert store.persist_world_model("c1")

    store.reset_world_models()               # 模拟重启
    again = store.get_world_model("c1")
    assert again.scope == _scope_dict()


def test_legacy_snapshot_without_scope_still_loads():
    path = store._model_path("c1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [{"kind": "intention_opened", "at": "", "payload": {"key": "提醒:喝水"}}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    model = store.load_world_model("c1")

    assert model is not None
    assert model.scope == {}
    assert [item.key for item in situation.open_intentions(model)] == ["提醒:喝水"]


def test_legacy_model_persists_without_a_scope_key():
    store.get_world_model("c1")
    assert store.persist_world_model("c1")

    payload = json.loads(store._model_path("c1").read_text(encoding="utf-8"))
    assert "scope" not in payload, "没有坐标时不要凭空写一个空 scope"


def test_same_chat_in_other_scope_gets_a_separate_model():
    first = store.get_world_model("c1", scope=_scope())
    other = store.get_world_model("c1", scope=Scope("qq", "bot2", "c1", "group", "c1"))
    assert first is not other
    assert first.scope == _scope_dict()
    assert other.scope["platform"] == "qq"
    with pytest.raises(ValueError, match="ambiguous_world_scope"):
        store.get_world_model("c1")


def test_scope_for_another_chat_is_rejected():
    with pytest.raises(ValueError, match="world_scope_mismatch"):
        store.get_world_model("c1", scope=_scope("c2"))


def test_disk_enumeration_keeps_scope():
    store.get_world_model("c1", scope=_scope())
    assert store.persist_world_model("c1")

    found = store.world_models_on_disk()

    assert found["c1"].scope == _scope_dict()


def test_data_channel_binds_scope():
    from core.execution.data_channels import get_world

    model = get_world("c1", scope=_scope())

    assert model.scope == _scope_dict()


def _commit_close(scope, subject="提醒:喝水"):
    from core.context.cognitive_commit import (
        CommitContext,
        Proposal,
        commit_updates,
        connect,
        invalidate_closed_cache,
    )

    conn = connect(os.environ["YINYUE_COGNITIVE_DB"])
    try:
        result = commit_updates(
            conn,
            context=CommitContext(scope=scope, visible_event_ids=frozenset({"evt-1"})),
            proposals=[Proposal("p1", subject, "close", {}, ("evt-1",))],
            idempotency_key="k-%s-%s" % (scope.platform, scope.account_id),
        )
        assert result.ok, result.rejected
    finally:
        conn.close()
    invalidate_closed_cache()


def test_overlay_respects_bound_scope_not_just_chat_id():
    """同 chat_id、不同平台/账号：别处的关闭不能关掉这里的 goal。"""
    bound = Scope("qq", "bot2", "c1", "group", "c1")
    base = store.get_world_model("c1", scope=bound)
    situation.open_intention(base, "提醒:喝水", kind="remind", evidence="提醒我喝水")
    assert store.persist_world_model("c1")

    _commit_close(Scope("wechat", "bot", "c1", "private", ""))

    store.reset_world_models()
    reloaded = store.get_world_model("c1")
    assert [item.key for item in situation.open_intentions(reloaded)] == ["提醒:喝水"]


def test_overlay_closes_when_scope_matches():
    base = store.get_world_model("c1", scope=_scope())
    situation.open_intention(base, "提醒:喝水", kind="remind", evidence="提醒我喝水")
    assert store.persist_world_model("c1")

    _commit_close(_scope())

    store.reset_world_models()
    reloaded = store.get_world_model("c1")
    assert situation.open_intentions(reloaded) == ()
