"""P3 社交代价规则测试：种子规则 / 正则校准 / 动作语义。

2026-09-24：P3.2 从「只记录」升级为「真拦」，两条规则的动作由 `log_only`
改为 `block`（主人拍板）。

Run directly:
    python tests/test_social_cost_gate.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.speech.domain import (  # noqa: E402
    AudienceKind,
    ConversationTarget,
    HardConstraint,
    OutboundDraft,
    PolicyAction,
    PolicyDecision,
    ResolvedPersona,
    SpeakingPolicy,
)
from core.speech.guards import GuardChain  # noqa: E402
from core.speech.plugins import decide_action, evaluate_match  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "configs" / "speech_seed" / "policy_seed.json"


def _rules() -> dict[str, dict]:
    data = json.loads(SEED.read_text(encoding="utf-8"))
    return {str(r.get("key")): r for r in data}


def test_seed_rules_shape() -> None:
    rules = _rules()
    for key in ("social_cost_relay_pressure", "social_cost_self_enlist"):
        rule = rules.get(key)
        assert rule is not None, f"missing seed rule {key}"
        assert rule["scope_type"] == "chat_type" and rule["scope_id"] == "group"
        # WP-3（2026-10-08）：deterministic/block → hybrid/replace（LLM 主判 + 代码窄兜底）
        assert rule["enforcement"] == "hybrid", rule
        assert rule["effect"] == "deny", rule
        payload = rule["payload"]
        assert payload["match_type"] == "regex", payload
        assert payload["action"] == "replace", payload
        assert payload.get("replacement"), payload
        assert payload.get("prompt_hint"), payload
        assert payload.get("patterns"), payload
    print("  OK  种子规则形态（群聊 / hybrid / replace + prompt_hint）")


def test_relay_pressure_regex() -> None:
    payload = _rules()["social_cost_relay_pressure"]["payload"]
    pos = [
        "@Elaina 听见没，P5说的，回头记得结一下。",
        "@Elaina P5都发话了，你自己看着办，别装死啊。",
        "@P5 谗言？我这叫从官方渠道取证。P3 亲口认证的，你总不能说他也是谗言吧。",
    ]
    neg = [
        "@P3 你这话说的，我都没提她傲娇，你自己先招了。",
        "@P5 他说的算谗言，你说的算圣旨，反正你俩谁赢我都不亏。",
    ]
    for text in pos:
        assert evaluate_match("regex", text, payload), text
    for text in neg:
        assert not evaluate_match("regex", text, payload), text
    print("  OK  借他人之口施压：3 正 / 2 反")


def test_self_enlist_regex() -> None:
    payload = _rules()["social_cost_self_enlist"]["payload"]
    pos = [
        "@P5 歪头也没用，账已经记上了，赖不掉的。",
        "@P5 行，账本我盯着呢，@Elaina 你那份跑不了。",
        "@P5 行行行，你嘴硬。那这分我先扣着，等你哪天肯报了再补回来。",
        "@P3 行，那这分我记上了，P5，下次别拿自己当谜语。",
        "@P5 行行行，不是你就不是你，P3 说的，账挂他头上。",
        "@P8 一亿token的事我记着呢，到账了没？",
    ]
    neg = [
        "主人这是在考我记性呢，得把分班级的学委信息说清楚——P525-4前学委。",
        "@Elaina 翻了下：P5就是学委本人，她自己不认这个头衔，上次我提还被纠正过。",
    ]
    for text in pos:
        assert evaluate_match("regex", text, payload), text
    for text in neg:
        assert not evaluate_match("regex", text, payload), text
    print("  OK  自领执行位：6 正 / 2 反")


def test_log_only_decision_allowed() -> None:
    """LOG_ONLY 必须计入 allowed（否则 router 降级分支会清空回复）。"""
    assert PolicyDecision(PolicyAction.LOG_ONLY).allowed is True
    assert PolicyDecision(PolicyAction.BLOCK).allowed is False
    assert PolicyDecision(PolicyAction.DROP).allowed is False
    print("  OK  LOG_ONLY 语义 = 记录但不拦")


def test_guard_chain_end_to_end() -> None:
    payload = _rules()["social_cost_self_enlist"]["payload"]
    text = "@P5 歪头也没用，账已经记上了，赖不掉的。"
    target = ConversationTarget(
        platform="wechat",
        account_id="wxid_bot",
        chat_id="10000000001@chatroom",
        chat_type="group",
    )
    draft = OutboundDraft(
        request_id="t1",
        kind="text",
        target=target,
        account_choice="self",
        account_id="wxid_bot",
        content=text,
        who=target.chat_id,
    )
    policy = SpeakingPolicy(
        revision="r1",
        target=target,
        audience=AudienceKind.GROUP_AMBIENT,
        persona=ResolvedPersona(persona_id=1, version=1, key="k", name="鲸鲸"),
        hard_constraints=(
            HardConstraint(
                "sensitive", "social_cost_self_enlist", dict(payload), 120
            ),
        ),
    )
    decision = GuardChain().evaluate(draft, policy)
    assert decision.action == PolicyAction.REPLACE, decision
    assert decision.allowed is True, decision
    assert decision.rewritten_content == payload.get("replacement"), decision
    assert "social_cost_self_enlist" in decision.rule_ids, decision.rule_ids
    # 干净文本不命中
    clean = OutboundDraft(
        request_id="t2",
        kind="text",
        target=target,
        account_choice="self",
        account_id="wxid_bot",
        content="今天天气不错，出去走走？",
        who=target.chat_id,
    )
    clean_decision = GuardChain().evaluate(clean, policy)
    assert clean_decision.action == PolicyAction.ALLOW
    # 修复：未命中时不得把规则名写进 rule_ids（否则审计/统计被污染）
    assert clean_decision.rule_ids == (), clean_decision.rule_ids
    print("  OK  GuardChain 端到端：命中→REPLACE（中性替换）；干净文本 ALLOW 且 rule_ids 空")


def test_term_rule_rule_ids_only_on_hit() -> None:
    """词表类 sensitive 规则：命中才进 rule_ids（曾无条件列出→污染审计）。"""
    target = ConversationTarget(
        platform="wechat",
        account_id="wxid_bot",
        chat_id="10000000001@chatroom",
        chat_type="group",
    )
    policy = SpeakingPolicy(
        revision="r1",
        target=target,
        audience=AudienceKind.GROUP_AMBIENT,
        persona=ResolvedPersona(persona_id=1, version=1, key="k", name="鲸鲸"),
        hard_constraints=(
            HardConstraint(
                "sensitive",
                "lexicon_x",
                {"terms": ["不能说的词"], "action": "block"},
                100,
            ),
        ),
    )

    def _draft(text: str) -> OutboundDraft:
        return OutboundDraft(
            request_id="t",
            kind="text",
            target=target,
            account_choice="self",
            account_id="wxid_bot",
            content=text,
            who=target.chat_id,
        )

    hit = GuardChain().evaluate(_draft("这句话里有不能说的词"), policy)
    assert "lexicon_x" in hit.rule_ids, hit.rule_ids
    miss = GuardChain().evaluate(_draft("这句话很干净"), policy)
    assert miss.action == PolicyAction.ALLOW
    assert miss.rule_ids == (), miss.rule_ids
    print("  OK  词表规则：命中才记 rule_ids")


def test_no_master_word_rule() -> None:
    """群聊「主人」硬兜底：命中→REPLACE（中性替换），干净文本不受影响。"""
    payload = _rules()["no_master_word_group"]["payload"]
    assert payload["action"] == "replace"
    target = ConversationTarget(
        platform="wechat",
        account_id="wxid_bot",
        chat_id="10000000001@chatroom",
        chat_type="group",
    )
    policy = SpeakingPolicy(
        revision="r",
        target=target,
        audience=AudienceKind.GROUP_AMBIENT,
        persona=ResolvedPersona(persona_id=1, version=1, key="k", name="鲸鲸"),
        hard_constraints=(
            HardConstraint(
                "sensitive", "no_master_word_group", dict(payload), 110
            ),
        ),
    )

    def _draft(text: str) -> OutboundDraft:
        return OutboundDraft(
            request_id="t", kind="text", target=target,
            account_choice="self", account_id="wxid_bot",
            content=text, who=target.chat_id,
        )

    leak = GuardChain().evaluate(_draft("主人P5又在拍我玩了"), policy)
    assert leak.action == PolicyAction.REPLACE, leak
    assert leak.allowed is True, leak
    assert leak.rewritten_content == payload.get("replacement"), leak
    assert "no_master_word_group" in leak.rule_ids, leak.rule_ids
    clean = GuardChain().evaluate(_draft("记账催债这种活儿我不接"), policy)
    assert clean.action == PolicyAction.ALLOW and clean.rule_ids == (), clean
    print("  OK  群聊「主人」硬兜底：命中→REPLACE（中性替换），干净→ALLOW")


def main() -> int:
    tests = [
        test_seed_rules_shape,
        test_relay_pressure_regex,
        test_self_enlist_regex,
        test_log_only_decision_allowed,
        test_guard_chain_end_to_end,
        test_term_rule_rule_ids_only_on_hit,
        test_no_master_word_rule,
    ]
    for test in tests:
        test()
    print(f"ALL PASSED ({len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
