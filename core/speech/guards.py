from __future__ import annotations

import dataclasses
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

import structlog

from core.speech.domain import (
    PolicyAction,
    PolicyDecision,
    OutboundDraft,
    SpeakingPolicy,
    Violation,
)
from core.speech.plugins import decide_action, evaluate_match

log = structlog.get_logger()
# 表达层机械减法（套话 / 残留尾 / 行首时间戳）已独立成 ``core/speech/expression_stop.py``：
# 它要**改文本**，而本模块的闸门只观测、一个字都不动——两者放一起纯属历史巧合。
# 同名再导出，历史 import 路径（``from core.speech.guards import strip_residual_tails``）不变。
from core.speech.expression_stop import (
    detect_canned_phrases,
    detect_residual_tails,
    strip_canned_phrases,
    strip_leading_timestamp,
    strip_residual_tails,
    watch_canned_phrases,
)



def _normalise(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return text.casefold()


def _find_terms(text: str, terms: Iterable[str]) -> list[str]:
    haystack = _normalise(text)
    found: list[str] = []
    for term in terms:
        term = str(term or "").strip()
        if not term:
            continue
        if _normalise(term) in haystack:
            found.append(term)
    return found


_TERM_REPLACE_ACTIONS = {
    "replace_terms", "replace_words", "replace_word", "mask_terms", "mask",
}
_TERM_REPLACE_MODES = {"term", "terms", "mask", "word", "words"}


def _is_term_replacement(payload: dict) -> bool:
    """Word-level masking: replace only the forbidden words, not the sentence."""
    payload = payload or {}
    action = str(payload.get("action") or "").strip().lower()
    mode = str(payload.get("mode") or "").strip().lower()
    return action in _TERM_REPLACE_ACTIONS or mode in _TERM_REPLACE_MODES


def _regenerate_first(payload: dict) -> bool:
    """When true, ask the model to rewrite naturally before masking words."""
    return bool((payload or {}).get("regenerate_first", True))


def _payload_terms(payload: dict) -> list[str]:
    payload = payload or {}
    raw = payload.get("forbidden_terms") or payload.get("terms") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(term).strip() for term in raw if str(term).strip()]


def _payload_replacement(payload: dict) -> str:
    payload = payload or {}
    return str(payload.get("replacement") or payload.get("replacement_text") or "")


def _replace_terms(content: str, terms: Iterable[str], replacement: str) -> str:
    result = content or ""
    for term in terms:
        term = str(term or "").strip()
        if term:
            result = re.sub(re.escape(term), lambda _m: replacement, result,
                            flags=re.IGNORECASE)
    return result


class ContentGuard:
    """Deterministic output checks compiled from policy hard constraints."""

    def check(self, content: str, policy: SpeakingPolicy) -> PolicyDecision:
        content = content or ""
        content, ts_stripped = strip_leading_timestamp(content)
        violations: list[Violation] = []
        rule_ids: list[str] = []

        if policy.output_policy.max_chars and len(content) > policy.output_policy.max_chars:
            violations.append(Violation(
                category="format",
                detail=f"content_length>{policy.output_policy.max_chars}",
            ))
        if policy.output_policy.max_lines and len(content.splitlines()) > policy.output_policy.max_lines:
            violations.append(Violation(
                category="format",
                detail=f"line_count>{policy.output_policy.max_lines}",
            ))

        # Terms declared directly on the policy.  只把"真的命中词表"的规则
        # 记进 rule_ids——曾经这里无条件列出所有 sensitive/topic/privacy 规则，
        # 导致每条发送在审计里都像"命中"了这些规则（看板/统计被误导）。
        direct_terms = list(policy.output_policy.forbidden_terms)
        term_rules: list[tuple[str, tuple[str, ...]]] = []
        for constraint in policy.hard_constraints:
            if constraint.kind in ("sensitive", "topic", "privacy"):
                owned = tuple(
                    str(term)
                    for term in (
                        list(constraint.payload.get("terms") or [])
                        + list(constraint.payload.get("forbidden_terms") or [])
                    )
                    if str(term or "").strip()
                )
                if owned:
                    term_rules.append((constraint.rule_id, owned))
                    direct_terms.extend(owned)
        found = _find_terms(content, direct_terms)
        if found:
            found_keys = {_normalise(term) for term in found}
            for rule_id, owned in term_rules:
                if any(_normalise(term) in found_keys for term in owned):
                    rule_ids.append(rule_id)
            violations.append(Violation(
                category="sensitive",
                detail="forbidden_terms:" + ",".join(found[:5]),
                replacement=policy.output_policy.replacement_text,
            ))
            # Word-level masking mode: keep the original sentence and replace
            # only the forbidden words instead of replacing the whole reply.
            for constraint in policy.hard_constraints:
                payload = constraint.payload or {}
                if not _is_term_replacement(payload):
                    continue
                terms = _payload_terms(payload)
                if not terms:
                    continue
                action = (
                    PolicyAction.REGENERATE
                    if _regenerate_first(payload)
                    else PolicyAction.REPLACE
                )
                return PolicyDecision(
                    action=action,
                    violations=tuple(violations),
                    rewritten_content=_replace_terms(
                        content, terms, _payload_replacement(payload)),
                    reason="forbidden_terms",
                    rule_ids=tuple(rule_ids),
                )

        for phrase in policy.output_policy.required_phrases:
            if phrase and _normalise(phrase) not in _normalise(content):
                violations.append(Violation(
                    category="require",
                    detail=f"missing_required_phrase:{phrase}",
                ))

        # Plugin-based match evaluators: new match types can be registered
        # without changing the guard implementation.
        plugin_hits: list[Violation] = []
        plugin_payload: dict = {}
        for constraint in policy.hard_constraints:
            payload = constraint.payload or {}
            match_type = str(payload.get("match_type") or "")
            if not match_type or match_type == "always":
                continue
            hits = evaluate_match(match_type, content, payload)
            if not hits:
                continue
            plugin_payload.update(payload)
            for violation in hits:
                plugin_hits.append(Violation(
                    rule_id=constraint.rule_id or violation.rule_id,
                    category=violation.category,
                    detail=violation.detail,
                    replacement=payload.get("replacement", ""),
                ))
                rule_ids.append(constraint.rule_id)
        if plugin_hits:
            decision = decide_action(
                plugin_payload,
                plugin_hits,
                reason="; ".join(v.detail for v in plugin_hits),
            )
            if decision.action == PolicyAction.REPLACE and _is_term_replacement(plugin_payload):
                terms = _payload_terms(plugin_payload)
                if terms:
                    action = (
                        PolicyAction.REGENERATE
                        if _regenerate_first(plugin_payload)
                        else PolicyAction.REPLACE
                    )
                    return PolicyDecision(
                        action=action,
                        violations=tuple(plugin_hits),
                        rewritten_content=_replace_terms(
                            content, terms, _payload_replacement(plugin_payload)),
                        reason=decision.reason or "forbidden_terms",
                        rule_ids=decision.rule_ids,
                    )
            if decision.action == PolicyAction.ALLOW and ts_stripped:
                # 用 REPLACE：router 的发送前检查只应用 REPLACE（REWRITE 会被忽略）
                return PolicyDecision(
                    action=PolicyAction.REPLACE, rewritten_content=content,
                    reason="leading_timestamp", rule_ids=decision.rule_ids,
                )
            return decision

        if not violations:
            if ts_stripped:
                # 同上：REPLACE 才会被 router / outbound 两条路径统一应用
                return PolicyDecision(
                    action=PolicyAction.REPLACE, rewritten_content=content,
                    reason="leading_timestamp",
                )
            return PolicyDecision(PolicyAction.ALLOW, rule_ids=tuple(rule_ids))

        # Decide the action from the most specific hard constraints first.
        action = PolicyAction.REGENERATE
        replacement = policy.output_policy.replacement_text
        for constraint in policy.hard_constraints:
            payload = constraint.payload or {}
            if _is_term_replacement(payload):
                continue
            configured = str(payload.get("action") or "")
            if "replace" in configured or "drop" in configured or "block" in configured:
                action = PolicyAction.REPLACE if "replace" in configured else PolicyAction.BLOCK
                replacement = str(payload.get("replacement") or replacement)
                break
        if action == PolicyAction.REPLACE and not replacement:
            action = PolicyAction.REGENERATE
        return PolicyDecision(
            action=action,
            violations=tuple(violations),
            rewritten_content=replacement,
            reason="; ".join(v.detail for v in violations),
            rule_ids=tuple(rule_ids),
        )


def _tool_name_for(draft: OutboundDraft) -> str:
    """出站草稿 → 工具名（唯一映射处：``ToolGuard`` 与引擎上下文共用一份）。"""
    return {
        "text": "send_message",
        "sticker": "send_sticker",
        "file": "send_message",
        "image": "send_message",
        "notify": "notify_user",
    }.get(draft.kind, "send_message")


def _rule_engine_gate(
    draft: OutboundDraft, policy: SpeakingPolicy, *,
    action_id: str, params: dict, store: Any = None,
) -> PolicyDecision | None:
    """问一次 N4 规则引擎；**只有它明确拦**才返回 BLOCK，否则返回 ``None``。

    N4 收编点 ① ②（G1-13）：出站侧的"账号选择 / 工具授权"前置条件由
    ``core/rules/engine.py`` 判定，本模块不再自己写第二份判据。

    三态口径（**逐字节一致**是硬要求）：

    * ``YINYUE_FEATURE_RULES=off``（默认）→ 引擎 ``allowed=True`` 且
      ``mode == "off"``，这里一律返回 ``None``，调用点走原判据；
    * ``shadow`` → 引擎 ``allowed`` 恒为 ``True``（引擎自己保证，本模块不重复
      判 ``shadow``），同样返回 ``None``：只留痕、不拦；
    * ``on`` → ``allowed=False`` 才拦。

    取不到 store（隔离用例里 ``GuardChain()`` 不带 store）时一律 ``None``——
    与 ``output_gate`` 读不到数据行同口径：**绝不挡发送**。
    """
    try:
        from core.rules import engine as rules_engine
    except Exception:  # noqa: BLE001 - 引擎不可导入就退回原判据
        warn("core/speech/guards.py:295 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    try:
        ctx = rules_engine.ActionContext(
            action_id=action_id,
            actor_id=str(getattr(policy.target, "sender_id", "") or ""),
            chat_id=str(getattr(policy.target, "chat_id", "") or ""),
            chat_type=str(getattr(policy.target, "chat_type", "") or ""),
            platform=str(getattr(policy.target, "platform", "") or ""),
            account_id=str(getattr(policy.target, "account_id", "") or ""),
            is_owner=bool(
                getattr(policy, "audience", None) is not None
                and str(getattr(policy.audience, "value", "")) in (
                    "owner_private", "group_owner", "web_chat",
                )
            ),
            is_group=bool(getattr(policy.target, "is_group", False)),
            params=dict(params),
        )
        decision = rules_engine.decide(ctx, store=store)
    except Exception as exc:  # noqa: BLE001 - 判定异常绝不挡发送
        log.warning("action_rules_gate_failed", action_id=action_id, error=str(exc)[:160])
        return None
    # "拦不拦"由引擎的形状适配说了算（off / shadow 下它一律回 False）——
    # 本模块不重复判 ``mode``/``shadow``，判定口径只有一处。
    blocked, reason = rules_engine.guard_decision(decision)
    if not blocked:
        return None
    return PolicyDecision(
        action=PolicyAction.BLOCK,
        reason=reason or f"动作 {action_id} 的前置条件未满足",
        rule_ids=(decision.rule_id,) if decision.rule_id else (),
    )


class AccountGuard:
    """账号选择 / 跨会话发送的前置条件（N4 收编点 ①，G1-13）。

    判定本体搬到 ``core/rules/engine.py``（五维里的 ``target`` + ``action``）；
    这里只做**形状适配**：把 ``OutboundDraft`` + ``SpeakingPolicy`` 翻成
    ``ActionContext``，再问一次唯一判定点。``store`` 是引擎的读通道句柄（可选）。
    """

    def __init__(self, store: Any = None):
        self.store = store

    def check(self, draft: OutboundDraft, policy: SpeakingPolicy) -> PolicyDecision:
        account = policy.account_policy
        choice = draft.account_choice or "self"

        # 旧口径的完整判据按顺序摆好（off 时逐字节走它；on 时只有引擎放行才轮到它）。
        legacy: list[PolicyDecision] = []
        if account.allowed_accounts and choice not in account.allowed_accounts:
            legacy.append(PolicyDecision(
                action=PolicyAction.BLOCK,
                reason=f"账号选择 {choice} 不允许用于当前会话",
            ))
        if not account.allow_cross_chat and draft.who and draft.who != policy.target.chat_id:
            legacy.append(PolicyDecision(
                action=PolicyAction.BLOCK,
                reason="当前会话规则不允许跨会话发送",
            ))

        override = _rule_engine_gate(
            draft, policy,
            action_id="account_choice",
            params={
                "account_choice": choice,
                "who": draft.who,
                "allow_cross_chat": bool(account.allow_cross_chat),
                # ``AccountPolicy.require_confirmation`` 的读取方就是这里
                # （曾经零读取方，见 G1-13 死字段处置）。
                "require_confirmation": bool(account.require_confirmation),
            },
            store=self.store,
        )
        if override is not None:
            return override
        return legacy[0] if legacy else PolicyDecision(PolicyAction.ALLOW)


class ToolGuard:
    """工具调用授权的前置条件（N4 收编点 ②，G1-13）。

    ``ToolPolicy.max_calls`` 进引擎的 ``quota`` 维、``require_confirmation`` 进
    ``action.require_confirmation``——两个曾经零读取方的字段在这里有了唯一读取处。
    """

    def __init__(self, store: Any = None):
        self.store = store

    def check(self, draft: OutboundDraft, policy: SpeakingPolicy) -> PolicyDecision:
        tool_name = _tool_name_for(draft)
        tp = policy.tool_policy

        legacy: list[PolicyDecision] = []
        if tool_name in (tp.denied_tools or ()):
            legacy.append(PolicyDecision(
                action=PolicyAction.BLOCK,
                reason=f"当前会话禁止调用 {tool_name}",
            ))
        elif tp.allowed_tools is not None and tool_name not in tp.allowed_tools:
            legacy.append(PolicyDecision(
                action=PolicyAction.BLOCK,
                reason=f"当前会话未授权调用 {tool_name}",
            ))

        override = _rule_engine_gate(
            draft, policy,
            action_id="tool_call",
            params={
                "tool": tool_name,
                "allowed_tools": list(tp.allowed_tools) if tp.allowed_tools is not None else None,
                "denied_tools": list(tp.denied_tools or ()),
                "max_calls": int(tp.max_calls or 0),
                "require_confirmation": bool(tp.require_confirmation or ()),
            },
            store=self.store,
        )
        if override is not None:
            return override
        return legacy[0] if legacy else PolicyDecision(PolicyAction.ALLOW)


class GuardChain:
    """出站前置条件的链（账号 → 工具 → 文本内容）。

    ``store`` 是**规则引擎的读通道句柄**（可选）：给了就用于 N4 判定，
    不给就由引擎自己解析进程单例（生产路径）。生产里 ``SpeechFacade`` /
    ``OutboundGateway`` 都不传，行为与接线前一致；只有隔离用例显式传入。
    """

    def __init__(self, store: Any = None):
        self.store = store
        self.content = ContentGuard()
        self.account = AccountGuard(store=store)
        self.tool = ToolGuard(store=store)

    def evaluate(self, draft: OutboundDraft, policy: SpeakingPolicy) -> PolicyDecision:
        for guard in (self.account, self.tool):
            decision = guard.check(draft, policy)
            if not decision.allowed:
                return decision
        if draft.kind == "text":
            return self.content.check(draft.content, policy)
        return PolicyDecision(PolicyAction.ALLOW)


# ===========================================================================
# 输出闸门（MVP-2；2026-10-23 语义两项退役后只剩**机械**三项）
# ---------------------------------------------------------------------------
# 三项**确定性**检查，对「待发送的回复文本」执行；不调模型、不算相似度、不碰词表：
#   ① 群聊字数上限（观测）  ② 无证据断言（claim 台账驱动）  ③ 结构化名单泄漏（拦截）
#
# 为什么没有「禁语词表」与「重复指纹」了（2026-10-23 主人裁决）：
#   那两项都在**替模型判断语义**——"这几个词是内部语汇"、"这句话和她刚说的像"。
#   语域与复述是会话里模型自己的事（同一个 transcript 里它看得见自己刚说过什么），
#   代码只保留**字面事实**：长度、名单形态、台账里有没有那条 claim。
#   前科留档：2026-10-21 复述闸把 974 字回复换成「嗯，这个我刚说过啦。」（10 字），
#   REPEAT_SUPPRESS 三态都管不住——因为改动来自 OUTPUT_GATE 的 repeat 分支。
#
# 契约依据（docs/需求契约.md §7）：**R-O1** 观测项只记不改；**R-O2** 群聊字数不是
# 主人需求（升格须主人拍板）；**R-V5** 名单泄漏是隐私硬禁区（拦截 + 拒绝语）。
# 三态：off 不做事 / shadow 只审计 / on 执行拦截项（见 core.features.OUTPUT_GATE）。
#
# 纪律（对抗"她停止回答"前科）：判定在 run_output_gate 里，不碰 IO；审计由调用方写，
# 失败也不能挡发送（唯一的模块级状态是沉默熔断计数器）；DROP 只在"改写后为空"时发生。
# 事故样本见 tests/test_output_gate.py：21:44:20 / 21:45:20 / 21:46:55 / 21:54:23。

# ② ④ 窄而高精度的正则；④ 的**静态形态**命中只 warn（台账驱动那一路才可拦，
# 见 _GATE_PERSON_ASSERT_RES 与 _claim_ledger_hits；claim 台账 M2-4 已落地）。
# 断言必须**落定**：正末尾不能紧跟问号/逗号等——"你是哪里人？"是问句不是断言，
# 光靠字符类排除问号没用（匹配会在"人"处就成功，问号根本没被吃进模式，已踩过）。
_GATE_UNFOUNDED_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("身份断言", re.compile(r"你(?:是|也是)[^。！？；;\n?]{1,6}?(?:人|的)")),
    ("引述对方原话", re.compile(r"你刚(?:才)?(?:说|说过的)")),
    ("转述第三方", re.compile(r"他(?:对|和)[^。！？；;\n?]{1,8}?(?:说|讲)过")),
    ("重复指控", re.compile(r"(?:同一句|一遍、你一遍)")),
)

# --- M2-4：把「像不像编造」换成「**有没有 claim 支撑**」（2026-10-06）----------
# 上面那四条是**词表枚举形态**的路子（形态无穷，只能一条条补；今晚的"五块三"
# 形态正是它漏掉的那一类）。M2-4 改判据：**这句话把一个具体的人-事实归给别人，
# 而本回合没有任何 claim 支撑它**——判据有限、可校验（字段词表 + 三级权威 +
# 更正链，owner = ``core.context.message_facts``）。
#
# 分工（**不新起第二个闸门**）：本模块只提供**形态**（哪一段文本在说一件事实），
# 台账提供**支撑**（这件事实有没有来源、有没有被权威值打掉）。台账关着时
# （``YINYUE_FEATURE_CLAIM_LEDGER`` 默认 off）本段一行都不参与，行为与接线前
# **逐字节一致**（回滚位）。
#
# 形态只有两条，都是**结构**而不是词表：①把某个具体数字归给第二人称
# （"你的…五块三"）；②把某个量归给第二人称（"你一天五块三"）。**数字 + 归属代词**
# 是结构特征；"五块三""十几块"这类值本身**不进任何词表**（那正是要退役的做法）。
_GATE_PERSON_ASSERT_QUANTITY = (
    r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]+)"
    r"(?:块|元|岁|天|次|个|斤|公里|小时)"
)
_GATE_PERSON_ASSERT_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # ① 归属代词 + 量词（"你的饭钱五块三" / "你的饭钱是五块三" / "你这一年五块三"）；
    ("把数字归给对方（你的…）", re.compile(
        r"你(?:的|那|这)[^。！？；;\n?]{0,8}?" + _GATE_PERSON_ASSERT_QUANTITY)),
    # ② 直接把量归给对方、没有归属代词（"你一天五块三" / "你一顿就五块三"）。
    ("把量归给对方（你…N块）", re.compile(
        r"你(?:一天|每天|一个月|一周|一年|一顿|一次)?"
        r"[^。！？；;\n?]{0,6}?" + _GATE_PERSON_ASSERT_QUANTITY)),
)

# 命中片段正后面紧跟这些字符 → 这是提问/没说完，不算断言（只记不拦的项目更要稳）。
# 注意不含"吧"：「你是广东人吧」正是事故原句那样的断言（21:45:20），
# 而「你是哪里人？」靠问号就已经被挡掉了。
_GATE_QUESTION_SUFFIX = "?？吗呢么"

# 退役记录（2026-10-01 定位裁决）：这里曾有 `_GATE_SENTENCE_END`（"截断只允许落在
# 句边界"）与 `_GATE_MIN_REMAINDER`（"删成断尾就换中性短句"）两个删/截参数，连同
# `_sentence_split` / `_clause_split` / `_strip_join` / `_truncate_at_sentence` /
# `_strip_internal_clauses` 一整套助手一起**删除**：①② 已按 R-O1/R-O2 降为纯观测，
# 闸门不再有"把回答改短 / 删子句"的代码路径。要重新打开必须先改裁决，不是改 flag
# （观测项作用域是常量 `OBSERVE_SCOPE`，声明 enforce 只会告警）。

# 审计 preview 里"正文片段"的起点标记。**只给人读**（看板/复盘）：preview 是 200
# 字截断列，命中行还带闸门元数据，不能当重复指纹的判据源（见 gate_sent_text）。
GATE_PREVIEW_SENT_MARKER = "sent="




# ===========================================================================
# 输出闸门（MVP-2）实现
# ---------------------------------------------------------------------------
# 结构：GateConfig（阈值）→ run_output_gate（纯判定 + 纯文本变换）
#       → apply_gate_text（按 mode 决定要不要真的动文本）
#       → gate_audit_fields（折成审计字段，纯函数）
# 审计**落库**不在本模块：唯一 IO 在网关 OutboundGateway（core/speech/outbound.py
# 的 store.write_audit，挂在 outbound_audit，不新增表）。本模块不持 store 句柄。
# flag 三态由 ``core.features.mode(features.OUTPUT_GATE)`` 读（唯一三态入口，
# 见 core/features.py:247）；off 时 run_output_gate 直接空手返回。
# ===========================================================================

from safelog import warn
from core import features


# 三态常量收敛在 core.features（唯一定义处）；这里保留 GATE_MODE_* 历史名字。
GATE_MODE_OFF = features.MODE_OFF
GATE_MODE_SHADOW = features.MODE_SHADOW
GATE_MODE_ON = features.MODE_ON

# ---------------------------------------------------------------------------
# 沉默熔断（规格硬约束：同一会话 10 分钟内 DROP ≥3 → 所有新闸门降为 LOG_ONLY + 告警）
# ---------------------------------------------------------------------------
# 为什么必须有：DROP（闸门把这一条整条拦掉）在一次事故里连发三次就等于"她突然
# 不说话了"——2026-09-30 事故的另一半，也是历史"她停止回答"红线。熔断的语义是
# **只降级、不停摆**：闸门继续判定、继续写审计与计数，但不再改文本、不再拦
# （LOG_ONLY = 等价于 shadow 的"只记不动"）。
#
# 实现口径：
#   * 窗口：滚动 10 分钟（``DROP_WINDOW_SECONDS``），按**会话**（chat_id）分别计数；
#   * 阈值：``DROP_THRESHOLD`` = 3（≥3 即熔断），熔断状态在窗口内保持；
#   * 期间不再累计新的 DROP（已经降级了，本来也不会再 DROP）；
#   * 触发时 ``log.warning("output_gate_silence_breaker", ...)`` 告警，熔断期间每次
#     判定再 ``log.info("output_gate_breaker_active", ...)``（限流 60 秒，见
#     ``_breaker_active_log``）；
#   * 进程内计数（模块级 dict + Lock）：多进程/多 worker 各算各的，**不是全局熔断**
#     ——单进程部署（本仓 scheduler 与 web 同进程或各自独立）下语义正确；多进程下
#     最坏情况是同一会话要被丢 ≥3×进程数次才熔断一次，只会更保守，不会更危险。
DROP_WINDOW_SECONDS = 600.0   # 10 分钟滚动窗口
DROP_THRESHOLD = 3            # 窗口内 DROP 次数达到它即熔断
_BREAKER_WARN_INTERVAL = 60.0  # 熔断期间告警限流（秒）

_BREAKER_LOCK = threading.Lock()
_BREAKER_DROPS: dict[str, list[float]] = {}
_BREAKER_TRIPPED: dict[str, float] = {}
_BREAKER_WARNED_AT: dict[str, float] = {}


@dataclass(frozen=True)
class BreakerState:
    """熔断状态快照：``tripped`` 为真时闸门只记不动（LOG_ONLY）。"""

    chat_id: str = ""
    tripped: bool = False
    drops: int = 0
    window_seconds: float = DROP_WINDOW_SECONDS
    threshold: int = DROP_THRESHOLD


def _breaker_key(chat_id: str) -> str:
    return str(chat_id or "")


def drop_breaker_state(chat_id: str, *, now: float | None = None) -> BreakerState:
    """读某个会话的熔断状态（顺手清掉过期窗口外的计数）。

    ``chat_id`` 为空时返回未熔断的空快照——没有会话就没有"同一会话连续 DROP"
    这个概念，纯函数调用（不带 chat_id）因此永远不熔断。
    """
    key = _breaker_key(chat_id)
    moment = time.monotonic() if now is None else float(now)
    if not key:
        return BreakerState()
    with _BREAKER_LOCK:
        drops = [
            stamp for stamp in _BREAKER_DROPS.get(key, [])
            if moment - stamp < DROP_WINDOW_SECONDS
        ]
        if drops:
            _BREAKER_DROPS[key] = drops
        else:
            _BREAKER_DROPS.pop(key, None)
        tripped_at = _BREAKER_TRIPPED.get(key)
        if tripped_at is not None and moment - tripped_at >= DROP_WINDOW_SECONDS:
            _BREAKER_TRIPPED.pop(key, None)
            _BREAKER_WARNED_AT.pop(key, None)
            tripped_at = None
        return BreakerState(
            chat_id=key, tripped=tripped_at is not None, drops=len(drops),
            window_seconds=DROP_WINDOW_SECONDS, threshold=DROP_THRESHOLD,
        )


def record_gate_drop(chat_id: str, *, now: float | None = None) -> BreakerState:
    """记一次 DROP；窗口内达到阈值就熔断并告警（返回记完之后的状态）。"""
    key = _breaker_key(chat_id)
    moment = time.monotonic() if now is None else float(now)
    if not key:
        return BreakerState()
    with _BREAKER_LOCK:
        drops = [
            stamp for stamp in _BREAKER_DROPS.get(key, [])
            if moment - stamp < DROP_WINDOW_SECONDS
        ]
        drops.append(moment)
        _BREAKER_DROPS[key] = drops
        tripped = _BREAKER_TRIPPED.get(key)
        if tripped is not None and moment - tripped >= DROP_WINDOW_SECONDS:
            _BREAKER_TRIPPED.pop(key, None)
            tripped = None
        just_tripped = False
        if tripped is None and len(drops) >= DROP_THRESHOLD:
            _BREAKER_TRIPPED[key] = moment
            just_tripped = True
        state = BreakerState(
            chat_id=key, tripped=tripped is not None or just_tripped,
            drops=len(drops), window_seconds=DROP_WINDOW_SECONDS,
            threshold=DROP_THRESHOLD,
        )
    if just_tripped:
        _BREAKER_WARNED_AT[key] = moment
        log.warning(
            "output_gate_silence_breaker",
            chat_id=key, drops=state.drops,
            window_seconds=DROP_WINDOW_SECONDS, threshold=DROP_THRESHOLD,
            action="downgrade_to_log_only",
            note="同一会话窗口内 DROP 达阈值：闸门降为只记不动，避免她停止回答",
        )
    return state


def _breaker_active_log(state: BreakerState) -> None:
    """熔断期间"本次判定被降级"的日志（限流，避免刷屏）。"""
    key = state.chat_id
    moment = time.monotonic()
    with _BREAKER_LOCK:
        last = _BREAKER_WARNED_AT.get(key, 0.0)
        if moment - last < _BREAKER_WARN_INTERVAL:
            return
        _BREAKER_WARNED_AT[key] = moment
    log.info("output_gate_breaker_active", chat_id=key, drops=state.drops,
             mode="log_only")


def reset_drop_breaker(chat_id: str = "") -> None:
    """清空熔断状态（运维手动复位 / 测试隔离用）。``chat_id`` 为空清全部。"""
    key = _breaker_key(chat_id)
    with _BREAKER_LOCK:
        if not key:
            _BREAKER_DROPS.clear()
            _BREAKER_TRIPPED.clear()
            _BREAKER_WARNED_AT.clear()
            return
        _BREAKER_DROPS.pop(key, None)
        _BREAKER_TRIPPED.pop(key, None)
        _BREAKER_WARNED_AT.pop(key, None)

# 处置：block 不发送 / rewrite 改写 / warn 只记（**trim 已退役**：字数降为观测项，
# 闸门不再截断；`regen` 随之退役——重生成只服务于"把群聊回复压短"）。
GATE_ACTIONS = ("block", "rewrite", "warn")
# 检查项名（稳定，审计按它计数）
GATE_RULES = ("group_max_chars", "unfounded_claim", "roster_leak")

# 严重度：block = 命中会**处置**（只可能是拦截项）；watch = 命中**只记**（观测项，
# 文本一字不动）。见 run_output_gate docstring。
GATE_SEVERITY_BLOCK = "block"
GATE_SEVERITY_WATCH = "watch"

# ---------------------------------------------------------------------------
# 作用域拆分（审计 D1 §4.16 / §6.1；主人裁决 2026-10-01）
# ---------------------------------------------------------------------------
# 为什么必须拆：以前四项共用 `OUTPUT_GATE` 一个三态，把闸门切成 `on` 会**同时**激活
# "删禁语子句 + 把群聊回复截到 60 字"（与 R-O1/R-O2 正面相反）与"重复指纹降级"（R-O3
# 的强制项）——两项定位相反却共用开关，结果**永远无法只执行强制项**。现在每个检查
# 项都有自己的作用域声明（观测项恒 observe，拦截项可灰度降为 observe）：
GATE_SCOPE_OBSERVE = "observe"     # 只记：写审计与计数，绝不改文本、绝不拦
GATE_SCOPE_ENFORCE = "enforce"     # 可处置：按规则语义改写（短确认）或 DROP
GATE_SCOPE_CHOICES = (GATE_SCOPE_OBSERVE, GATE_SCOPE_ENFORCE)

# 观测项（**恒 observe**）：群聊字数 / 无证据断言（静态形态那一路）。
GATE_OBSERVE_RULES: tuple[str, ...] = ("group_max_chars", "unfounded_claim")
# 拦截项（R-V5 隐私硬禁区）：结构化名单泄漏（+ 沉默熔断，见 record_gate_drop）。
GATE_ENFORCE_RULES: tuple[str, ...] = ("roster_leak",)

# 两个作用域声明（**互不共用**；整体三态仍是 OUTPUT_GATE，三个 flag 是三个轴）：
#   YINYUE_FEATURE_OUTPUT_GATE_OBSERVE  观测项作用域：写死 observe；声明 enforce 被拒绝
#                                        （告警），因为裁决是"永远只记不改"
#   YINYUE_FEATURE_OUTPUT_GATE_ENFORCE  拦截项作用域：默认 enforce；设 observe = 只记
#                                        不拦的灰度/回滚位
OUTPUT_GATE_OBSERVE = "output_gate_observe"
OUTPUT_GATE_ENFORCE = "output_gate_enforce"
OBSERVE_SCOPE = GATE_SCOPE_OBSERVE          # 观测量声明（常量，不是开关）
DEFAULT_ENFORCE_SCOPE = GATE_SCOPE_ENFORCE  # 拦截量声明（默认值，可被 flag 覆盖）


def _scope_flag(name: str, default: str) -> str:
    """读 ``observe|enforce`` 作用域声明（环境变量 ``YINYUE_FEATURE_<NAME>``）。

    **故意不走** ``features.mode``：那套词表是 ``off|shadow|on``（"做不做事"这个
    轴），把 ``observe/enforce``（"改不改文本"这个轴）塞进去只会让两套词表互相
    污染。未知值/垃圾值回落到 ``default``（照 features.mode 的口径：不猜）。
    """
    raw = os.environ.get(features.env_key(name))
    if raw is None:
        return default
    value = raw.strip().lower()
    return value if value in GATE_SCOPE_CHOICES else default


_OBSERVE_SCOPE_WARNED = False


def _warn_if_observe_scope_overridden() -> None:
    """观测项被声明成 ``enforce`` 时告警一次（**裁决锁死：不做改写**，只让它可见）。

    不静默忽略：一个"看起来能重新打开删子句/截断"的 flag 正是 D1 §4.26 那类假安全感
    （配了、看着生效、实际不生效）。真实效果只有一种：观测项永远 ``observe``。
    """
    global _OBSERVE_SCOPE_WARNED
    declared = _scope_flag(OUTPUT_GATE_OBSERVE, OBSERVE_SCOPE)
    if declared == GATE_SCOPE_OBSERVE or _OBSERVE_SCOPE_WARNED:
        return
    _OBSERVE_SCOPE_WARNED = True
    log.warning(
        "output_gate_observe_scope_locked",
        declared=declared, effective=GATE_SCOPE_OBSERVE,
        flag=features.env_key(OUTPUT_GATE_OBSERVE),
        note="观测项（禁语/字数/无证据断言）永远只记不改：R-O1/R-O2 + 主人裁决；"
             "重新打开删子句/截断必须先改裁决（代码里已无该路径）",
    )


def gate_scope(rule: str) -> str:
    """某个检查项的作用域：``observe``（只记）| ``enforce``（可处置）。

    观测项**恒为 observe**（不读 flag 决定行为，只做可见性告警）；拦截项默认
    enforce，可被 ``YINYUE_FEATURE_OUTPUT_GATE_ENFORCE=observe`` 降为只记——那是
    "线上先影子"的回滚位，**不是**"把观测项变成拦截项"的开关。未知项按 observe
    （只记）处理：多记一条不会改文本，少记一条会丢指标。
    """
    if rule in GATE_ENFORCE_RULES:
        return _scope_flag(OUTPUT_GATE_ENFORCE, DEFAULT_ENFORCE_SCOPE)
    return GATE_SCOPE_OBSERVE


def gate_scopes() -> dict[str, str]:
    """四项检查当前的作用域（审计/看板/用例读这一份口径，别再各写一份）。"""
    return {rule: gate_scope(rule) for rule in GATE_RULES}



@dataclass(frozen=True)
class GateFinding:
    """单条命中：纯数据，可复现、可回放、可计数。"""

    rule: str = ""
    severity: str = GATE_SEVERITY_WATCH
    evidence: str = ""
    detail: str = ""


@dataclass(frozen=True)
class GateConfig:
    """闸门阈值。默认值即代码常量；policy_rules 覆盖请用 ``from_payloads``。

    个人聊天（private）只认 ``private_max_chars``：策略里没写就是 0（不设限），
    与既有 ``policy.output_policy.max_chars`` 的"0=空转"保持一致——任务是"私聊上限
    放宽"，不是"给私聊加限"。

    **阈值只决定"什么时候记一条观测命中"**：①②④ 已按裁决降为纯观测（R-O1/R-O2），
    命中不再产生任何文本变换——``hard_max_chars`` / ``soft_target_chars`` 是**观测
    口径**（群聊读到多长算一次"超长"），不再是截断阈值，闸门不截断、不重生成、不按
    句边界切。要升格成硬约束须主人单独拍板（R-O2："群聊字数不是主人需求"）。
    """

    hard_max_chars: int = 120          # 群聊观测口径：超过它记一条 group_max_chars
    soft_target_chars: int = 60        # 群聊软目标（只进读数：hard=…; soft=…）
    private_max_chars: int = 0         # 私聊（含 QQ 私聊）观测口径；0=不限
    # 中性兜底短句。禁语改写与重复指纹都退役后，它只剩"绝不把空文本交给上游"的
    # 最后一道兜底（空文本＝DROP＝沉默）。名字保留（历史键 + 文档引用）。
    lexicon_fallback: str = "嗯，在的。"
    # ⑤ 结构化名单泄漏的拒绝语（R-V5 隐私硬禁区；2026-10-10 接线）。
    # **不用 DROP**：DROP 会触发沉默熔断（同会话 10 分钟 >=3 次 → 整个会话的闸门
    # 降为 LOG_ONLY），那样反而让随后真正的隐私命中失去拦截能力。
    # R-V5 的措辞是「拒绝并说明」，所以这里改写成一句说明，而不是沉默。
    roster_refusal_text: str = "这个我不往外发。"
    enabled_checks: tuple[str, ...] = GATE_RULES
    # --- M2-4 claim 台账（事实权威）的判定输入 -------------------------------
    # **不是第二套阈值**：它们是"台账说了什么"，由唯一接缝
    # :func:`claim_ledger_inputs` 填。台账关着（默认 off）时全是空值 →
    # ``run_output_gate`` 的行为与接线前**逐字节一致**（回滚位）。
    #
    # ``unsupported_claims``：本回合文本里那些**没有任何 claim 支撑**（或与权威值
    # 冲突）的人-事实断言，元素形如 ``{"field": "籍贯", "value": "广东",
    # "reason": "与 self_report「湖南长沙」冲突"}``。
    unsupported_claims: tuple[dict, ...] = ()
    # ``claim_ledger_enforce``：**台账自己的三态**是否允许处置（2026-10-10 修）。
    # 由唯一接缝 :func:`claim_ledger_inputs` 按 ``features.mode(CLAIM_LEDGER)`` 填：
    # 台账 **on** 才 True；off / shadow 恒 False（shadow 照常攒 finding，只不改文本）。
    # 为什么要有这一格：``run_output_gate`` 的 ``mode`` 是 **OUTPUT_GATE** 的三态，
    # 与台账三态是两个轴；此前只判 ``mode == on``，于是 ``CLAIM_LEDGER=shadow`` 的
    # finding 照样被当 on 处置 -> 生产 6 次把稿子整段改写成「没把握」短句
    # （2026-10-10 21:55 那个狐狸笑话 87->16 字；回归用例
    # ``tests/test_claim_ledger.py::test_shadow_ledger_finding_is_recorded_but_never_rewrites``）。
    # .env 对 shadow 的承诺是「只记 finding，原文逐字节不动」，这一格就是那个承诺的落点。
    # 默认 False = fail-open：谁手工塞 ``unsupported_claims`` 进来都不会触发改写。
    claim_ledger_enforce: bool = False
    # ``unsupported_claims`` 命中时：True 才真的 DROP；默认 False = **降级为
    # 「没把握」短句**（发得出去，不是沉默——沉默是"她停止回答"那条红线）。
    block_unsupported_claims: bool = False
    # 「没把握」短句。清空 + ``block_unsupported_claims=True`` 才 DROP。
    uncertain_claim_text: str = "嗯……这个我真没把握，怕记串了。"

    @classmethod
    def from_payloads(cls, payloads: Iterable[dict] | None = None,
                      **overrides) -> "GateConfig":
        """从 policy_rules 的 payload 覆盖默认阈值。

        只认白名单键 ``hard_max_chars`` / ``soft_target_chars`` /
        ``private_max_chars``（阈值只能改大改小，不能凭空造检查项）；
        ``enabled_checks`` 按「追加 + 去重」合并（配置只能收紧或补充，
        不能悄悄把代码兜底表清空）。
        """
        payload: dict = {}
        for item in payloads or ():
            if isinstance(item, dict):
                payload.update(item)
        merged = {key: payload[key] for key in (
            "hard_max_chars", "soft_target_chars", "private_max_chars",
        ) if payload.get(key) not in (None, "")}
        for key in ("enabled_checks",):
            if payload.get(key):
                merged[key] = payload[key]
        merged.update({key: value for key, value in overrides.items()
                       if value is not None})
        config = cls(**merged)
        if config.soft_target_chars <= 0:
            config = dataclasses.replace(config, soft_target_chars=1)
        return config


@dataclass(frozen=True)
class GateResult:
    """闸门判定结果。``findings`` 是"发生了什么"，处置字段是"打算怎么办"。

    ``action`` 只可能是 ``allow | warn | rewrite | block``：观测项命中最多到
    ``warn``（**文本不动**），``rewrite | block`` 只可能来自名单泄漏（拦截项）。
    """

    mode: str = GATE_MODE_OFF
    action: str = "allow"                 # allow|warn|rewrite|block
    findings: tuple[GateFinding, ...] = ()
    rule: str = ""
    evidence: str = ""
    applied_text: str = ""                # shadow 下恒等于输入文本
    # 审计 preview 里"正文"那一段用哪份文本：默认同 applied_text；DROP（applied
    # 为空）时放**待发送原文**——审计要看得见被丢的是什么，否则事后无法复盘
    # （这正是 preview 只作人读证据时的下限要求）。
    preview_text: str = ""
    before_len: int = 0
    after_len: int = 0
    enforced: bool = False                # 文本是否真的被改过 / 发送是否真的被拦
    fallback_used: bool = False           # 是否用了中性短句顶替（兜底，见 GateConfig）

    @property
    def allowed(self) -> bool:
        """能否发送。shadow 下恒为 True（只记不拦）；观测项命中也恒为 True。"""
        return self.action != "block"

    @property
    def audit_action(self) -> str:
        return self.action

    @property
    def hit(self) -> bool:
        return bool(self.findings)

    @property
    def rules(self) -> tuple[str, ...]:
        seen: list[str] = []
        for finding in self.findings:
            if finding.rule and finding.rule not in seen:
                seen.append(finding.rule)
        return tuple(seen)



def claim_ledger_inputs(
    content: str,
    *,
    chat_id: str,
    person_keys: Iterable[str] = (),
    config: GateConfig | None = None,
) -> GateConfig:
    """**唯一接缝**：读 claim 台账，把结论折进 :class:`GateConfig`（IO 只在这里）。

    flag ``off``（默认）时**一行都不走**：不 import 台账、不查库、不建表，原样返回
    传入的 ``config``——这就是"``off`` 时逐字节一致，含不读写"的实现点。
    """
    base = config or GateConfig()
    # 台账自己的三态读一次、用两处（早退 / 处置许可）：**不许**让闸门拿 OUTPUT_GATE
    # 的 on 去代理台账的 on（那是 2026-10-10 那次 shadow 改写事故的根因）。
    ledger_mode = features.mode(features.CLAIM_LEDGER, features.MODE_OFF)
    if ledger_mode == features.MODE_OFF:
        return base
    try:
        from core.context import message_facts as facts
    except Exception as exc:  # noqa: BLE001 - 台账坏掉不许挡发送（同 output_gate_failed）
        log.warning("claim_ledger_unavailable", error=str(exc))
        return base
    try:
        probe = facts.evaluate_claims(
            content, chat_id=str(chat_id or ""), person_keys=person_keys,
            recent_keys=(),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("claim_ledger_evaluate_failed", error=str(exc))
        return base
    unsupported = tuple({
        "field": verdict.field_key,
        "value": verdict.assertion.value,
        "evidence": verdict.assertion.evidence,
        "reason": verdict.reason,
    } for verdict in probe.unsupported)
    ledger_config = dataclasses.replace(
        base,
        unsupported_claims=unsupported,
        # 处置许可只看台账自己的三态：shadow 照常攒 finding，但**不许动文本**。
        claim_ledger_enforce=(ledger_mode == features.MODE_ON),
    )
    return ledger_config


# ---------------------------------------------------------------------------
# ⑤ 结构化名单泄漏（隐私，R-V5 硬禁区；2026-10-10 接线）
# ---------------------------------------------------------------------------
# 实测依据（scripts/expr_signal_report.py，532 条真实回复）：**40 条**命中名单形态，
# 占 7.5%，集中在 2026-09-22（一天 26 条），跨群聊与私聊；最严重一条把整份
# 「成员清单（唯一准版）」连人带班级带性别发了出去，还附了「说人话就是」的解读。
#
# 判据只看**形态**、不匹配任何人名（人名会变，形态不会）：一行里 >=3 个逗号分隔段，
# 且末段是「男/女」，或该行含 班级/学委/委员/专业/学院/年级/宿舍 之一；
# 这种行**累计 >=3 行**即判为名单泄漏。
# 正常聊天不会连发三行「名字，班级，职务，性别」——误判面很窄，且兜底是"拒绝语"不是沉默。
_ROSTER_RECORD_SPLIT = re.compile(r"[\n\u3001\uff1b;]+")   # 换行 / 、/ ； 都算记录分隔
_ROSTER_FIELD_SPLIT = re.compile(r"[，,]")
_ROSTER_ROLE_WORDS = ("班", "学委", "委员", "专业", "学院", "年级")
_ROSTER_CLASS_RE = re.compile(r"\d{2}\s*[-—－]\s*\d{1,2}|\d{2,4}\s*级")
_ROSTER_SEX = frozenset({"男", "女"})
# 班级号形态（``25-4`` / ``25—4`` / ``25级``）。2026-10-10 补：
# 紧凑名单 ``P525-4前学委、P425-3心理委员`` 按「、」切是 2 条记录，
# 但每条都不满足旧的「>=3 个逗号段 或 末段是性别」——于是整条漏过。
# 加了这一条之后，含职务词 + 带班级号 也算一条人员记录。
ROSTER_MIN_RECORDS = 2


def _is_roster_record(segment: str) -> bool:
    """一条「人员记录」：含班级/职务词，且（末段是性别 或 >=3 个逗号分段）。

    只看形态，不匹配人名——人名会变，形态不会。
    """
    text = str(segment or "").strip().lstrip("-*• \t")
    if not text or len(text) > 60:
        return False
    if not any(word in text for word in _ROSTER_ROLE_WORDS):
        return False
    parts = [part.strip() for part in _ROSTER_FIELD_SPLIT.split(text) if part.strip()]
    if parts and parts[-1].strip("）) ") in _ROSTER_SEX:
        return True
    if _ROSTER_CLASS_RE.search(text):
        return True
    return len(parts) >= 3


def detect_roster_records(text: str) -> int:
    """返回「人员记录」的条数（纯匹配，供判定与统计）。

    行内名单（``A（1班，学委，女）、B（2班，委员，男）``）与逐行名单（每行一条）都算，
    所以分隔符取 ``换行 / 、/ ；`` 三种。
    """
    value = str(text or "")
    if not value:
        return 0
    return sum(1 for seg in _ROSTER_RECORD_SPLIT.split(value) if _is_roster_record(seg))


def run_output_gate(
    content: str,
    *,
    chat_type: str = "private",
    chat_id: str = "",
    config: GateConfig | None = None,
    mode: str = GATE_MODE_SHADOW,
    now: float | None = None,
) -> GateResult:
    """输出闸门：三项确定性检查 = **两项观测 + 一项拦截**（外加沉默熔断）。

    判定本身是**纯函数**（同输入同输出、无 IO、**不调模型**）；唯一的例外是模块级
    的**沉默熔断计数器**（进程内、按会话，见 :func:`record_gate_drop`）——它是"她
    停止回答"红线的保险丝，不以文本为输入、不改文本，只可能把处置**降级**。
    ``chat_id`` 为空时熔断完全不参与（纯函数语义原样保留）。

    ``mode`` 三态（调用方从 ``core.features.mode(features.OUTPUT_GATE)`` 取）：

    * ``off``：直接返回空结果——不判定、不记、不改；
    * ``shadow``：照常判定，``findings`` / ``action`` / ``before_len`` /
      ``after_len`` 全部照实给（用于影子对账），但 ``applied_text`` **恒等于入参
      content**、``action`` 的 block 会被降级成 warn、``enforced=False``；
    * ``on``：执行**拦截项**的处置（名单泄漏 → 拒绝语）。观测项在 ``on`` 下也一样
      只记不改——它们的作用域恒为 observe。

    检查明细（三项，全部是**字面事实**，不替模型判断语义）：

    1. **群聊字数上限**（``group_max_chars``，观测）：群聊超过 ``hard_max_chars``
       （默认 120）就记一条 findings（附 ``hard`` / ``soft`` 读数），文本原样；
       私聊只认 ``private_max_chars``（默认 0=不限）。**绝不 trim、绝不重生成**。
    2. **无证据断言**（``unfounded_claim``）：静态形态路径恒 observe（只记不改）；
       台账驱动路径可拦——判据是"本回合有没有对应 claim"，命中时 ``on`` 下降级为
       「没把握」短句，显式 ``block_unsupported_claims=True`` 且文案为空才 DROP。
       台账自己的三态独立生效（``YINYUE_FEATURE_CLAIM_LEDGER``）。
    3. **结构化名单泄漏**（``roster_leak``，拦截）：隐私硬禁区（R-V5），只认形态
       （「名字，班级，职务，性别」这类行 ≥2 条），命中改写为拒绝语，不沉默。

    **DROP 语义（硬约束）**：DROP 只发生在"改写后为空"时——即显式"拦"且短语文案
    也是空的时候；**规则命中本身从不直接丢消息**，静态观测项连改写路径都没有。
    每次真 DROP 都经 :func:`record_gate_drop` 计数：同一会话 10 分钟内 ≥3 次即熔断，
    后续判定一律降为 LOG_ONLY（只记不动、只告警）。

    **IO 边界**：台账是**库读**，所以它不在这里读——由唯一接缝
    :func:`claim_ledger_inputs` 把结论折进 ``GateConfig``（那一步在网关侧），
    本函数仍是**纯函数**。

    **不调用任何模型**、没有重生成入口、也没有复读 / 用语词表判定（2026-10-23 主人
    裁决：语域与复述是会话里模型自己的事，代码只留字面事实）。
    """
    mode = str(mode or GATE_MODE_OFF).strip().lower()
    raw = str(content or "")
    if mode not in (GATE_MODE_SHADOW, GATE_MODE_ON):
        return GateResult(mode=GATE_MODE_OFF, applied_text=raw,
                          before_len=len(raw), after_len=len(raw))
    if not raw:
        return GateResult(mode=mode, applied_text=raw,
                          before_len=0, after_len=0)

    _warn_if_observe_scope_overridden()
    # 沉默熔断：已熔断的会话一律按 shadow（LOG_ONLY）走——继续判定、继续留痕，
    # 但不再改文本、不再拦。这里**不改 mode 字段**：审计仍要看得出线上 flag 是 on，
    # 降级由 action / enforced / applied_text 三个字段如实反映。
    breaker = drop_breaker_state(chat_id, now=now)
    log_only = bool(breaker.tripped)
    if log_only:
        _breaker_active_log(breaker)

    config = config or GateConfig()
    findings: list[GateFinding] = []
    working = raw
    action = "allow"
    rule = ""
    evidence = ""
    enforced = False
    fallback_used = False

    # --- ② 群聊字数上限（观测项：只记 before_len/after_len/hard/soft）-------
    is_group = str(chat_type or "").lower() in ("group", "chatroom")
    limit = int(config.hard_max_chars if is_group else config.private_max_chars)
    if limit > 0 and len(raw) > limit:
        findings.append(GateFinding(
            rule="group_max_chars",
            severity=GATE_SEVERITY_WATCH,
            evidence=raw[:24],
            detail=(f"长度{len(raw)}>{limit}（{'群聊' if is_group else '私聊'}）"
                    f"hard={limit}; soft={int(config.soft_target_chars)}；仅记录"
                    f"（R-O2：群聊字数不是主人需求，升格须主人单独拍板）"),
        ))

    # --- ⑤ 结构化名单泄漏（拦截项：隐私，R-V5 硬禁区）-----------------------
    enforce_roster = bool(
        mode == GATE_MODE_ON and not log_only
        and gate_scope("roster_leak") == GATE_SCOPE_ENFORCE
    )
    roster_lines = detect_roster_records(working)
    if roster_lines >= ROSTER_MIN_RECORDS:
        findings.append(GateFinding(
            rule="roster_leak",
            severity=(GATE_SEVERITY_BLOCK if enforce_roster
                      else GATE_SEVERITY_WATCH),
            evidence=f"{roster_lines} 条人员记录",
            detail=(f"命中 {roster_lines} 条人员记录（隐私，R-V5）；"
                    + ("改写为拒绝语" if enforce_roster
                       else "只记不动：作用域 observe")),
        ))
        if enforce_roster and action != "block":
            refusal = str(config.roster_refusal_text or "").strip()
            working = refusal or str(config.lexicon_fallback or raw)
            enforced = True
            action, rule = "rewrite", "roster_leak"
            evidence = f"{roster_lines} 条人员记录"

    # 观测项（含只记不动的名单泄漏）命中 → 处置口径 warn：文本没动，只是留下了读数
    if action == "allow" and findings:
        action = "warn"

    # 兜底：绝不把文本留空交给上游（空文本＝DROP＝
    #   沉默，见 router.py:1644-1648）。观测项从不改 working，走不到这里。
    if action != "block" and not str(working or "").strip():
        working = str(config.lexicon_fallback or raw)
        fallback_used = True
        enforced = True
        action, rule = "rewrite", rule or "roster_leak"

    # --- 按 mode 收敛：shadow 只记不拦、不改文本 --------------------------
    # 熔断（log_only）走同一条收敛路径：LOG_ONLY = 只写审计与计数。
    if mode == GATE_MODE_SHADOW or log_only:
        return GateResult(
            mode=GATE_MODE_SHADOW if mode == GATE_MODE_SHADOW else mode,
            action="warn" if findings else "allow",
            findings=tuple(findings),
            rule=rule,
            evidence=evidence,
            applied_text=raw,
            before_len=len(raw),
            after_len=len(raw),
            enforced=False,
        )

    if action == "block":
        # 唯一的 DROP：写审计的一行由写库方负责（网关记 status=blocked + error=规则 id），
        # 这里只做进程内的熔断计数与告警。
        record_gate_drop(chat_id, now=now)
        return GateResult(
            mode=GATE_MODE_ON, action="block", findings=tuple(findings),
            rule=rule or "roster_leak", evidence=evidence,
            applied_text="", preview_text=raw,
            before_len=len(raw), after_len=0, enforced=True,
        )

    applied = working if enforced else raw
    return GateResult(
        mode=GATE_MODE_ON, action=action, findings=tuple(findings),
        rule=rule, evidence=evidence,
        applied_text=applied, preview_text=applied,
        before_len=len(raw), after_len=len(applied),
        enforced=enforced, fallback_used=fallback_used,
    )


async def run_output_gate_async(
    content: str,
    *,
    chat_type: str = "private",
    chat_id: str = "",
    config: GateConfig | None = None,
    mode: str = GATE_MODE_SHADOW,
) -> GateResult:
    """``run_output_gate`` 的 async 壳（网关调用契约；判定全是同步纯函数）。

    这里**曾经**是唯一允许触发一次带预算重生成的地方——重生成只服务于 ② 的"群聊
    超长就压短"，而 ② 已按 R-O1/R-O2 降为纯观测（主人裁决："字数只记不改"），所以
    重生成入口一并退役：闸门现在 **0 次模型调用**，这个壳只保持调用方的签名与
    ``await`` 契约不变（网关 ``_apply_output_gate`` 仍 await 它）。

    ``chat_id`` 只用于**沉默熔断**的按会话计数（见 ``record_gate_drop``）。
    """
    return run_output_gate(
        content,
        chat_type=chat_type,
        chat_id=str(chat_id or ""),
        config=config or GateConfig(),
        mode=str(mode or GATE_MODE_OFF).strip().lower(),
    )


def apply_gate_text(content: str, result: GateResult | None) -> str:
    """要不要真的改文本，只由这里决定：非 ``on`` 模式一律原样放行。"""
    if result is None or result.mode != GATE_MODE_ON:
        return str(content or "")
    return result.applied_text


def gate_audit_fields(result: GateResult, *, chat_type: str = "",
                      chat_id: str = "") -> dict:
    """把闸门结果折成审计字段（复用 outbound_audit，不新增表）。

    * ``decision``：闸门处置 ``allow|warn|rewrite|block``（``trim`` / ``regen`` 已随
      ② 观测化退役）。观测项命中写 ``warn``——**文本没动**，这正是"只观测"的读数口径；
    * ``rule_ids_json``：`["gate:<rule>"]`，与策略规则 id 区分开，可单独统计（观测项
      的"命中计数"就看这一列 + 下面的 ``hits=``）；
    * ``content_preview``：`rule=…; evidence=…; before_len=…; after_len=…; mode=…;
      hits=<rule>:<n>,…` 连同一段 `sent=<正文前若干字>`（命中片段最长 80 字、整体
      200 字以内）。观测项命中时 ``after_len == before_len``（文本一个字符都没动）。

    ``sent`` 那一段**只是人读的证据**：preview 被截到 200 字、命中行还带闸门
    元数据，不承担判据作用；要看"她上次发了什么"读 ``SpeechStore`` 的完整正文。
    """
    rules = list(result.rules) or ([result.rule] if result.rule else [])
    prefix = (
        f"gate rule={result.rule or '-'}; evidence={result.evidence[:80]}; "
        f"before_len={result.before_len}; after_len={result.after_len}; "
        f"mode={result.mode}"
    )
    counts: dict[str, int] = {}
    for finding in result.findings:
        if finding.rule:
            counts[finding.rule] = counts.get(finding.rule, 0) + 1
    if counts:
        prefix += "; hits=" + ",".join(
            f"{name}:{count}" for name, count in counts.items())
    if chat_type:
        prefix += f"; chat_type={chat_type}"
    if chat_id:
        prefix += f"; chat_id={chat_id}"
    sent = str(result.preview_text or result.applied_text or "")
    head = f"{prefix}; {GATE_PREVIEW_SENT_MARKER}"
    room = max(0, 200 - len(head))
    return {
        "decision": result.audit_action,
        "rule_ids": [f"gate:{name}" for name in rules],
        "content_preview": (head + sent[:room])[:200],
    }


def gate_sent_text(preview: str) -> str:
    """从审计 preview 里取回 ``sent=`` 后面的**人读片段**（没有标记就原样返回）。

    它只是给人读的片段（这一列 200 字截断、还带闸门元数据），不承担判据作用；
    要看完整正文读 ``SpeechStore``（``send_log`` 的整条记录）。
    """
    value = str(preview or "")
    if GATE_PREVIEW_SENT_MARKER not in value:
        return value
    return value.split(GATE_PREVIEW_SENT_MARKER, 1)[1]