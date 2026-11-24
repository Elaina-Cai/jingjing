from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable

from core.speech.domain import PolicyAction, PolicyDecision, Violation
from safelog import warn

MatchEvaluator = Callable[[str, dict[str, Any]], list[Violation]]
ActionHandler = Callable[[dict[str, Any], list[Violation], str], PolicyDecision]

_MATCH_EVALUATORS: dict[str, MatchEvaluator] = {}
_ACTION_HANDLERS: dict[str, ActionHandler] = {}


def register_match_evaluator(name: str):
    def deco(fn: MatchEvaluator) -> MatchEvaluator:
        _MATCH_EVALUATORS[name] = fn
        return fn
    return deco


def register_action_handler(name: str):
    def deco(fn: ActionHandler) -> ActionHandler:
        _ACTION_HANDLERS[name] = fn
        return fn
    return deco


def match_evaluator(name: str) -> MatchEvaluator | None:
    return _MATCH_EVALUATORS.get(name)


def action_handler(name: str) -> ActionHandler | None:
    return _ACTION_HANDLERS.get(name)


def _normalise(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    return text.replace("\u200b", "").replace("\ufeff", "").casefold()


@register_match_evaluator("always")
def _match_always(content: str, payload: dict) -> list[Violation]:
    return []


@register_match_evaluator("contains")
@register_match_evaluator("keyword")
def _match_keyword(content: str, payload: dict) -> list[Violation]:
    terms = [str(x) for x in (payload.get("terms") or payload.get("keywords") or [])]
    if payload.get("pattern"):
        terms.append(str(payload["pattern"]))
    haystack = _normalise(content)
    found = [term for term in terms if _normalise(term) in haystack]
    return [Violation(category="keyword", detail="forbidden_terms:" + ",".join(found[:5]))] if found else []


@register_match_evaluator("regex")
def _match_regex(content: str, payload: dict) -> list[Violation]:
    patterns = payload.get("patterns") or []
    if payload.get("pattern"):
        patterns = list(patterns) + [payload["pattern"]]
    # WP-3（2026-10-08）：正则匹配前先做与 keyword 同源的归一化（NFKC + 去零宽 + casefold），
    # 修「主\u200b人」这类零宽绕过；pattern 侧按需同时写全/半角（NFKC 会把 ！？， 变 !?,）。
    haystack = _normalise(content)
    for pattern in patterns:
        try:
            if re.search(str(pattern), haystack):
                return [Violation(category="regex", detail=f"pattern:{pattern}")]
        except re.error:
            warn("core/speech/plugins.py:73 re.error", err=repr(__import__("sys").exc_info()[1]))
            continue
    return []


@register_match_evaluator("semantic")
def _match_semantic(content: str, payload: dict) -> list[Violation]:
    # Semantic constraints no longer run an outbound LLM rewrite (Phase 3).
    # This evaluator stays as a marker so ContentGuard does not hard-block a
    # rule that currently has no deterministic player.
    return []


def _action_name(payload: dict, fallback: str = "regenerate") -> str:
    raw = str(payload.get("action") or fallback).lower()
    for key in ("replace", "rewrite", "drop", "block", "regenerate", "log_only", "allow"):
        if key in raw:
            return key
    if "replace" in raw:
        return "replace"
    return fallback


@register_action_handler("allow")
def _action_allow(payload: dict, violations: list[Violation], reason: str) -> PolicyDecision:
    return PolicyDecision(PolicyAction.ALLOW, violations=tuple(violations),
                          reason=reason,
                          rule_ids=tuple({v.rule_id for v in violations if v.rule_id}))


@register_action_handler("block")
def _action_block(payload: dict, violations: list[Violation], reason: str) -> PolicyDecision:
    return PolicyDecision(PolicyAction.BLOCK, violations=tuple(violations),
                          reason=reason,
                          rule_ids=tuple({v.rule_id for v in violations if v.rule_id}))


@register_action_handler("drop")
def _action_drop(payload: dict, violations: list[Violation], reason: str) -> PolicyDecision:
    return PolicyDecision(PolicyAction.DROP, violations=tuple(violations),
                          reason=reason,
                          rule_ids=tuple({v.rule_id for v in violations if v.rule_id}))


@register_action_handler("replace")
def _action_replace(payload: dict, violations: list[Violation], reason: str) -> PolicyDecision:
    return PolicyDecision(PolicyAction.REPLACE, violations=tuple(violations),
                          rewritten_content=str(payload.get("replacement") or ""),
                          reason=reason,
                          rule_ids=tuple({v.rule_id for v in violations if v.rule_id}))


@register_action_handler("rewrite")
def _action_rewrite(payload: dict, violations: list[Violation], reason: str) -> PolicyDecision:
    return PolicyDecision(PolicyAction.REWRITE, violations=tuple(violations),
                          rewritten_content=str(payload.get("replacement") or ""),
                          reason=reason,
                          rule_ids=tuple({v.rule_id for v in violations if v.rule_id}))


@register_action_handler("regenerate")
def _action_regenerate(payload: dict, violations: list[Violation], reason: str) -> PolicyDecision:
    return PolicyDecision(PolicyAction.REGENERATE, violations=tuple(violations),
                          reason=reason,
                          rule_ids=tuple({v.rule_id for v in violations if v.rule_id}))


@register_action_handler("log_only")
def _action_log_only(payload: dict, violations: list[Violation], reason: str) -> PolicyDecision:
    return PolicyDecision(PolicyAction.LOG_ONLY, violations=tuple(violations),
                          reason=reason,
                          rule_ids=tuple({v.rule_id for v in violations if v.rule_id}))


def evaluate_match(match_type: str, content: str,
                   payload: dict) -> list[Violation]:
    evaluator = match_evaluator(match_type or "always")
    if evaluator is None:
        return []
    return evaluator(content or "", payload or {})


def decide_action(payload: dict, violations: list[Violation],
                  reason: str = "") -> PolicyDecision:
    name = _action_name(payload)
    handler = action_handler(name) or action_handler("regenerate")
    return handler(payload or {}, violations, reason or name)
