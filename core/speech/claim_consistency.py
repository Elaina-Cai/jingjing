"""Deterministic checks for memory-backed visible claims."""
from __future__ import annotations

import re
from typing import Any, Iterable

_HEDGES = (
    "可能",
    "也许",
    "好像",
    "似乎",
    "不确定",
    "没把握",
    "没核实",
    "未核实",
    "没法核实",
    "无法核实",
    "查不到",
    "找不到",
    "记不清",
    "怕记串",
    "印象里",
    "如果没记错",
    "是不是",
    "对吗",
    "对吧",
    "你再提醒",
    "再说一下原话",
)

_PROMISE_CLAUSES = re.compile(r"[^，,。；;！!？?、\r\n但]+")
_CLAUSE_QUESTION_TAIL = re.compile(r"(?:吗|么)\s*$")
_PROMISE_QUALIFIERS = _HEDGES + (
    "记不准",
    "记不得",
    "不记得",
    "拿不准",
)
_PROMISE_VERBS = r"(?:答应|承诺|保证|许诺|允诺)"
_ACTION_VERBS = r"(?:找|查|核对|确认|处理|准备|整理|跟进|联系|安排|做|弄|写|改|修)"
_DELIVER_VERBS = (
    r"(?:完成|做完|弄好|处理好|核对好|整理好|交给你|发给你|办妥|办好)"
)
_FUTURE_MARKERS = (
    r"(?:会|一定|肯定|保证|马上|立刻|尽快|待会|等下|稍后|明早|明天|今晚|下午|回头|晚点)"
)
_PROMISED_RE = re.compile(
    rf"^(?:我|俺|咱)(?:确实|的确|记得|之前|以前|当时|曾经|曾|已经|早就|是){{0,3}}"
    rf"{_PROMISE_VERBS}(?:过|了|你|您|给你|帮你)"
)
_NEVER_PROMISED_RE = re.compile(
    rf"^(?:我|俺|咱)?(?:确实|之前|以前|当时)?"
    rf"(?:从没|从未|从来没有|从来没|从来未|未曾|没有|没)"
    rf"{_PROMISE_VERBS}(?:过|了|你|您)?"
)
_ONGOING_RE = re.compile(
    rf"^(?:(?:我|俺|咱)(?:现在|这会儿)?(?:还在|一直在|正在|在)|还在|正在)"
    rf"(?:帮你|给你|试着)?{_ACTION_VERBS}"
)
_COMPLETED_RE = re.compile(
    rf"(?:我|俺|咱)[^，。；！!？?]{{0,6}}(?:已经|早就|刚|刚刚|才)"
    rf"[^，。；！!？?]{{0,4}}{_DELIVER_VERBS}"
)
_COMPLETED_BARE_RE = re.compile(
    rf"(?:我|俺|咱)[^，。；！!？?]{{0,4}}(?:完成|做完|弄好|处理好|核对好|整理好)了"
)
_COMMITMENT_RE = re.compile(
    rf"^(?:但|不过|那|所以|就|并且|而且|然后|另外|我|俺|咱)*"
    rf"[^，。；！!？?]{{0,2}}{_FUTURE_MARKERS}[^，。；！!？?]{{0,10}}{_DELIVER_VERBS}"
)
_CORRECTION_SENTENCE_BREAK = re.compile(r"(?<=[。！？!?；;])|(?<=\.)[ \t]+|[\r\n]+")


def _is_question(text: str) -> bool:
    value = str(text or "").strip()
    return value.endswith(("?", "？")) or value.endswith(("吗", "么", "呢"))


def claims_unverified_promise(text: str) -> bool:
    """Flag an unqualified promise claim from a turn with no scoped record."""
    body = str(text or "")
    if not body.strip():
        return False
    for match in _PROMISE_CLAUSES.finditer(body):
        clause = match.group(0).strip()
        if not clause:
            continue
        tail = body[match.end():match.end() + 1]
        if tail in ("？", "?") or _CLAUSE_QUESTION_TAIL.search(clause):
            continue
        if any(marker in clause for marker in _PROMISE_QUALIFIERS):
            continue
        if (
            _PROMISED_RE.search(clause)
            or _NEVER_PROMISED_RE.search(clause)
            or _ONGOING_RE.search(clause)
            or _COMPLETED_RE.search(clause)
            or _COMPLETED_BARE_RE.search(clause)
            or _COMMITMENT_RE.search(clause)
        ):
            return True
    return False


def _fact_value(content: str) -> str:
    text = " ".join(str(content or "").split())
    for separator in (" = ", "=", "：", ":"):
        if separator in text:
            text = text.rsplit(separator, 1)[1].strip()
            break
    return text.strip("。！？!?，,；;「」\"'“”‘’ ")


def claim_value(claim: dict[str, Any]) -> str:
    return _fact_value(str(claim.get("content") or ""))


def replace_offending_clause(
    text: str,
    values: Iterable[str],
    replacement: str,
) -> str:
    """Replace clauses that repeat an offending value, keeping the rest."""
    body = str(text or "").strip()
    replacement = str(replacement or "").strip()
    cleaned_values: list[str] = []
    for value in values or ():
        value_text = str(value or "").strip()
        if value_text:
            cleaned_values.append(value_text)
    if not body or not cleaned_values or not replacement:
        return replacement
    clauses = [
        clause
        for clause in re.split(r"(?<=[。！？!?；;，,\n])", body)
        if clause
    ]
    output: list[str] = []
    replaced = False
    for clause in clauses:
        if any(value.casefold() in clause.casefold() for value in cleaned_values):
            if not replaced:
                output.append(replacement)
                replaced = True
            continue
        output.append(clause)
    if not replaced:
        return replacement
    result = "".join(output).strip()
    return result or replacement


def claims_from_memory_data(data: Any) -> list[dict[str, Any]]:
    """Extract uncertain claims from memory tool payloads."""
    direct_claims: list[dict[str, Any]] = []
    if isinstance(data, dict) and isinstance(data.get("memory_claims"), list):
        rows = []
        for item in data["memory_claims"]:
            if not isinstance(item, dict):
                continue
            if str(item.get("state") or "") == "conflict":
                direct_claims.append(dict(item))
            else:
                rows.append(item)
    else:
        rows = data if isinstance(data, list) else []
    claims: list[dict[str, Any]] = []
    conflict_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        metadata = row.get("metadata") or {}
        state = str(metadata.get("evidence_state") or row.get("evidence_state") or "")
        if state in {"unknown", "unverified"}:
            claims.append({
                "content": str(row.get("content") or ""),
                "state": state,
                "source_event_ids": list(row.get("source_event_ids") or ()),
            })
        if metadata.get("conflict"):
            key = (
                str(metadata.get("subject_key") or metadata.get("belief_key") or "__conflict__"),
                str(metadata.get("predicate") or ""),
            )
            group = conflict_groups.setdefault(key, {
                "content": str(row.get("content") or ""),
                "state": "conflict",
                "subject_key": key[0],
                "predicate": key[1],
                "values": [],
                "source_event_ids": [],
            })
            value = _fact_value(str(row.get("content") or ""))
            if value and value not in group["values"]:
                group["values"].append(value)
            group["source_event_ids"].extend(
                ref for ref in (row.get("source_event_ids") or ()) if ref not in group["source_event_ids"]
            )
    claims.extend(direct_claims)
    claims.extend(group for group in conflict_groups.values() if len(group["values"]) > 1)
    return claims


def uncertain_memory_claim(
    text: str,
    claims: Iterable[dict[str, Any]],
    *,
    visible_event_ids: Iterable[str] = (),
    supported_texts: Iterable[str] = (),
) -> dict[str, Any] | None:
    """Return the first uncertain memory directly asserted as fact."""
    body = str(text or "")
    if _is_question(body):
        return None
    visible = {str(ref) for ref in (visible_event_ids or ()) if str(ref)}
    supported = "\n".join(str(item or "") for item in (supported_texts or ()))
    for claim in claims or ():
        refs = {str(ref) for ref in (claim.get("source_event_ids") or ()) if str(ref)}
        if refs & visible:
            continue
        value = _fact_value(str(claim.get("content") or ""))
        if len(value) < 2 or value.casefold() not in body.casefold():
            continue
        if value.casefold() in supported.casefold():
            continue
        if any(marker in body for marker in _HEDGES):
            continue
        return dict(claim)
    return None


def safe_uncertain_reply(state: str) -> str:
    if str(state or "") == "unverified":
        return "这条我没留下可核实的来源，怕记串了。你再提醒我一下当时的原话好吗？"
    return "这个我印象里有，但现在的依据没核实，我不敢当成确定的事说。你再提醒我一下原话好吗？"


def stale_corrected_memory_claim(
    text: str,
    corrections: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """Catch an old value reasserted after a successful in-turn correction."""
    sentences = _CORRECTION_SENTENCE_BREAK.split(str(text or "").casefold())
    historical = ("以前", "之前", "原来", "曾经", "记错", "叫错", "认错", "更正", "改成")
    for correction in corrections or ():
        old_value = str(correction.get("old_value") or "").strip()
        if len(old_value) < 2:
            continue
        old = old_value.casefold()
        new = str(correction.get("new_value") or "").strip().casefold()
        for sentence in sentences:
            sentence = sentence.strip()
            if old not in sentence:
                continue
            # A direct address remains a use of the old name even in a question.
            if sentence.startswith(old) and re.match(r"[，,]\s*[你您]", sentence[len(old):]):
                return dict(correction)
            if new and new in sentence:
                continue
            if any(marker in sentence for marker in historical):
                continue
            if _is_question(sentence):
                continue
            return dict(correction)
    return None


def safe_corrected_reply(correction: dict[str, Any]) -> str:
    if len(correction.get("values") or ()) > 1:
        return safe_conflict_reply(correction)
    value = str(correction.get("new_value") or "").strip()
    if not value:
        return "我刚才更正了那条记录，现在的情况还需要确认。"
    return f"我这里当前的记录是「{value}」。"


def conflicted_memory_claim(
    text: str,
    claims: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """Catch one side of a known memory conflict asserted as the sole fact."""
    body = str(text or "")
    if _is_question(body):
        return None
    conflict_markers = ("冲突", "两种说法", "几个说法", "谁说的", "核对", "不确定")
    for claim in claims or ():
        if str(claim.get("state") or "") != "conflict":
            continue
        values = [str(value) for value in (claim.get("values") or ()) if str(value)]
        mentioned = [value for value in values if value.casefold() in body.casefold()]
        if not mentioned:
            continue
        if len(mentioned) >= 2 or any(marker in body for marker in conflict_markers):
            continue
        return dict(claim)
    return None


def safe_conflict_reply(claim: dict[str, Any]) -> str:
    values = [str(value) for value in (claim.get("values") or ()) if str(value)]
    if len(values) >= 2:
        choices = "」和「".join(values[:3])
        return (
            f"这件事我现在看到「{choices}」两种说法，来源互相冲突，"
            "我不敢确定哪个是真的。你还记得哪个是对的吗？"
        )
    return "这件事我这里的说法互相冲突，我不敢确定哪个是真的。你再帮我确认一下好吗？"


def guard_memory_text(
    text: str,
    entries: Iterable[dict[str, Any]],
    *,
    visible_event_ids: Iterable[str] = (),
    supported_texts: Iterable[str] = (),
) -> tuple[str, dict[str, Any]]:
    """Apply the final memory consistency gate to any outbound path."""
    claims = claims_from_memory_data(list(entries or ()))
    conflict = conflicted_memory_claim(text, claims)
    if conflict is not None:
        safe = safe_conflict_reply(conflict)
        return replace_offending_clause(text, conflict.get("values") or (), safe), {
            "action": "rewrite",
            "reason": "conflicted_memory_claim",
            "values": list(conflict.get("values") or ()),
        }
    uncertain = uncertain_memory_claim(
        text,
        claims,
        visible_event_ids=visible_event_ids,
        supported_texts=supported_texts,
    )
    if uncertain is not None:
        safe = safe_uncertain_reply(str(uncertain.get("state") or ""))
        return replace_offending_clause(text, [claim_value(uncertain)], safe), {
            "action": "rewrite",
            "reason": "uncertain_memory_claim",
            "state": str(uncertain.get("state") or ""),
        }
    return str(text or ""), {}


__all__ = [
    "claim_value",
    "claims_from_memory_data",
    "claims_unverified_promise",
    "conflicted_memory_claim",
    "guard_memory_text",
    "replace_offending_clause",
    "safe_conflict_reply",
    "safe_corrected_reply",
    "safe_uncertain_reply",
    "stale_corrected_memory_claim",
    "uncertain_memory_claim",
]
