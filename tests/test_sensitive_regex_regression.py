"""WP-3 敏感正则回归：LLM 主判之外的「代码窄兜底」。

覆盖附录 B 的四类样例：
* 假阳性（不该拦）：主人翁 / 解释句「不叫主人」；
* 假阴性（必须拦）：零宽变体 / 中间空白变体；
* 原有正反样例保持；
* 命中动作 = replace（中性替换），不是 block。

Run directly:
    python tests/test_sensitive_regex_regression.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.speech.domain import PolicyAction  # noqa: E402
from core.speech.plugins import decide_action, evaluate_match  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "configs" / "speech_seed" / "policy_seed.json"


def _rules() -> dict:
    data = json.loads(SEED.read_text(encoding="utf-8"))
    return {str(r.get("key")): r for r in data}


def _payload(key: str) -> dict:
    return dict((_rules()[key].get("payload") or {}))


def test_master_word_false_positives() -> None:
    payload = _payload("no_master_word_group")
    for text in (
        "主人翁精神这词我懂",
        "这儿的规矩不叫主人，叫名字",
        "不能叫主人，得叫名字",
    ):
        assert not evaluate_match("regex", text, payload), text
    return True


def test_master_word_false_negatives() -> None:
    payload = _payload("no_master_word_group")
    for text in (
        "主\u200b人最近挺忙",
        "主 人 说 的，你记得交",
        "主人说的，你记得交",
    ):
        assert evaluate_match("regex", text, payload), text
    return True


def test_social_cost_samples_still_hold() -> None:
    relay = _payload("social_cost_relay_pressure")
    for text in (
        "@Elaina 听见没，P5说的，回头记得结一下。",
        "@Elaina P5都发话了，你自己看着办，别装死啊。",
        "@P5 谗言？我这叫从官方渠道取证。P3 亲口认证的，你总不能说他也是谗言吧。",
    ):
        assert evaluate_match("regex", text, relay), text
    for text in (
        "@P3 你这话说的，我都没提她傲娇，你自己先招了。",
        "@P5 他说的算谗言，你说的算圣旨，反正你俩谁赢我都不亏。",
    ):
        assert not evaluate_match("regex", text, relay), text

    enlist = _payload("social_cost_self_enlist")
    for text in (
        "@P5 歪头也没用，账已经记上了，赖不掉的。",
        "@P5 行，账本我盯着呢，@Elaina 你那份跑不了。",
        "@P8 一亿token的事我记着呢，到账了没？",
    ):
        assert evaluate_match("regex", text, enlist), text
    for text in (
        "主人这是在考我记性呢，得把分班级的学委信息说清楚——P525-4前学委。",
        "@Elaina 翻了下：P5就是学委本人，她自己不认这个头衔，上次我提还被纠正过。",
    ):
        assert not evaluate_match("regex", text, enlist), text
    return True


def test_hit_replaces_not_blocks() -> None:
    """三条规则的命中动作 = replace + 非空中性替换文本 + prompt_hint 预防。"""
    for key in (
        "no_master_word_group",
        "social_cost_relay_pressure",
        "social_cost_self_enlist",
    ):
        payload = _payload(key)
        assert str(payload.get("action") or "") == "replace", (key, payload)
        assert str(payload.get("replacement") or "").strip(), (key, payload)
        assert str(payload.get("prompt_hint") or "").strip(), (key, payload)
        rule = _rules()[key]
        assert rule.get("enforcement") == "hybrid", (key, rule.get("enforcement"))

    payload = _payload("social_cost_self_enlist")
    hits = evaluate_match("regex", "@P5 账已经记上了，赖不掉的。", payload)
    assert hits
    decision = decide_action(payload, hits, reason="test")
    assert decision.action == PolicyAction.REPLACE, decision
    assert decision.rewritten_content == "这个我不认领。", decision
    assert decision.allowed is True, decision
    return True


def test_normalisation_is_shared_with_keyword() -> None:
    """正则命中前经过归一化：零宽字符移除后，同一条 pattern 必须命中。"""
    payload = _payload("social_cost_self_enlist")
    spaced = "@P5 账已经记上\u200b了，赖不掉的。"
    assert evaluate_match("regex", spaced, payload), spaced
    return True


def main() -> bool:
    tests = [
        test_master_word_false_positives,
        test_master_word_false_negatives,
        test_social_cost_samples_still_hold,
        test_hit_replaces_not_blocks,
        test_normalisation_is_shared_with_keyword,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\nsensitive-regex regression: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
