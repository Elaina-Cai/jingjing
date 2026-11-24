"""短事实召回门（长句改写问法）与更正信念的可读标签。

两个真实缺口（2026-11-18 B 回放）：
1. ``_match`` 用 query 侧的 token 数当分母，整句问法会把唯一实体词稀释到 0.0x，
   「晚上想煮个汤，香菜还放吗？」永远召不回「香菜 = 不吃」。
2. 更正走 ``belief-correct:<hash>`` 操作号，渲染成 ``belief-correct:... = 吃``：
   模型读不懂，关键词也召不回。
"""
from types import SimpleNamespace

import pytest

from core.context import situation, situation_store as store
from core.context.cognitive_commit import Scope
from core.context.situation import Belief
from core.memory.belief_commit import fingerprint
from core.memory.beliefs import BeliefMemoryProvider, belief_label, effective_beliefs
from core.memory.hub import MemoryHub
from core.memory.providers import _match, _tokens
from core.terminal.principal import ChatType, PrincipalContext, RelationLevel

SCOPE = Scope("wechat", "a", "same")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    store.reset_world_models()
    yield
    store.reset_world_models()


def principal():
    return PrincipalContext("wechat", "a", "same", ChatType.PRIVATE, "person-a", "A", RelationLevel.UNKNOWN)


def context(at=""):
    return SimpleNamespace(principal=principal(), extra={"platform": "wechat", "account_id": "a", "chat_id": "same", "belief_as_of": at})


def model():
    return store.get_world_model("same", scope=SCOPE)


def recall(query="", at=""):
    return MemoryHub([BeliefMemoryProvider()]).recall(principal(), query, kinds=["fact"], limit=20, context=context(at))


@pytest.mark.parametrize("query", [
    "香菜",
    "香菜还放吗",
    "我不吃香菜",
    "晚上想煮个汤，香菜还放吗？",
])
def test_reworded_question_still_matches_two_token_fact(query):
    assert _match(query, "香菜 = 不吃") is True


def test_corrected_two_token_fact_matches_after_paraphrase():
    assert _match("上次说错了，我其实吃香菜的。", "香菜 = 吃") is True


@pytest.mark.parametrize("query", ["帮我找一部纪录片看看。", "晚上吃什么", "部署说明在哪"])
def test_unrelated_question_does_not_match_short_fact(query):
    assert _match(query, "香菜 = 不吃") is False


def test_long_content_is_not_loosened_by_short_fact_rule():
    long_content = "项目资料 文档 索引：" + "部署说明在 docs/deploy.md；" * 12
    assert len(_tokens(long_content)) > 8
    assert _match("香菜还放吗", long_content) is False


def test_corrected_belief_keeps_human_label_and_is_recallable():
    situation.assert_fact(model(), "香菜", value="不吃", at="2026-11-17", subject_key="person-a", predicate="dislikes", evidence_refs=("event-1",))
    old = [belief for belief in effective_beliefs(model()) if belief.key == "香菜"][0]
    operation_key = "belief-correct:687ccfe746932df3"
    model().add("fact_asserted", {
        "key": operation_key, "value": "吃", "source": "chat_observed", "who": "person-a",
        "subject_key": "person-a", "predicate": "dislikes", "scope": ":".join(SCOPE.key),
        "supersedes": "香菜", "supersedes_fingerprint": fingerprint(old), "change_kind": "correction",
        "evidence_refs": ["event-2"],
    }, at="2026-11-19")

    live = effective_beliefs(model())
    assert [belief.value for belief in live] == ["吃"]
    assert belief_label(live[0]) == "香菜"

    hits = recall("晚上想煮个汤，香菜还放吗？")
    assert len(hits) == 1
    assert "belief-correct:" not in hits[0].content
    assert hits[0].content == "香菜 = 吃"
    assert hits[0].metadata["belief_key"] == operation_key


def test_all_opaque_labels_fall_back_to_key():
    belief = Belief(key="belief-correct:687ccfe746932df3", value="吃", subject_key="person-a")
    assert belief_label(belief) == "belief-correct:687ccfe746932df3"
