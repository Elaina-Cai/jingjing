"""N4 规则引擎（**动作前置条件**的唯一判定点）。

唯一 owner 是 :mod:`core.rules.engine`：五维规则（``condition`` / ``target`` /
``quota`` / ``action`` / ``priority``）→ :class:`core.rules.engine.RuleDecision`，
数据面复用 ``policy_rules`` + ``core.speech.store`` 的 ``get_revision()`` 缓存通道
（改一行数据即生效、不用重启）。调用点一律走模块级 :func:`decide` / :func:`record`，
**不许**自己再写一份前置判定。
"""

from __future__ import annotations

from core.rules.engine import (
    HARD_WALL_ACTIONS,
    RULES_RULE_KEY,
    ActionContext,
    Condition,
    Quota,
    QuotaCounts,
    Rule,
    RuleAction,
    RuleDecision,
    RuleEngine,
    Target,
    action_ids,
    authority,
    bump_revision,
    decide,
    decide_with_rows,
    delete_rule,
    describe_rules,
    get_engine,
    guard_decision,
    has_capability,
    is_owner,
    quota_blocked,
    record,
    rules,
    rules_mode,
    save_rule,
    use_count,
)

__all__ = [
    "HARD_WALL_ACTIONS",
    "RULES_RULE_KEY",
    "ActionContext",
    "Condition",
    "Quota",
    "QuotaCounts",
    "Rule",
    "RuleAction",
    "RuleDecision",
    "RuleEngine",
    "Target",
    "action_ids",
    "authority",
    "bump_revision",
    "decide",
    "decide_with_rows",
    "delete_rule",
    "describe_rules",
    "get_engine",
    "guard_decision",
    "has_capability",
    "is_owner",
    "quota_blocked",
    "record",
    "rules",
    "rules_mode",
    "save_rule",
    "use_count",
]
