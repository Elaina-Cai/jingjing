"""世界事实的有效版本读取：先限会话，再解析替代与有效区间，最后匹配问题。"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
import re

from core.context.situation import WorldModel, project
from core.infra import skips
from core.memory.contracts import MemoryItem


def _date(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value))
    if result.tzinfo is not None:
        raise ValueError("belief_time_requires_local_clock")
    return result


_OPAQUE_LABEL = re.compile(
    r"^(?:belief-correct:|belief-revoked?:|[0-9a-f]{8,}$|wxid_[a-z0-9]+|(?:person|user|account|acct|member|group)[-_:])",
    re.IGNORECASE,
)


def belief_label(belief) -> str:
    """给模型看的标签：纠错后的 key 是操作号（belief-correct:<hash>），念出来没有意义。"""
    for candidate in (belief.subject_key, belief.key, belief.supersedes):
        text = str(candidate or "").strip()
        if text and not _OPAQUE_LABEL.match(text):
            return text
    return str(belief.key or "")


def query_context(context, valid_at: str = ""):
    """历史查询仅改副本的有效时间，不污染共享工具上下文或已绑定身份。"""
    import copy
    normalized = _date(valid_at).isoformat(sep=" ") if valid_at else ""
    if context is None:
        return None
    result = copy.copy(context)
    result.extra = {**(getattr(context, "extra", None) or {}), "belief_as_of": normalized}
    return result


def effective_beliefs(model: WorldModel, *, at=None):
    """at 是事实有效时间，不是知识录入时间；纠错不能把错误旧值复活。"""
    from core.memory.belief_commit import committed_facts
    return resolve_versions([*model.events, *committed_facts(model.scope)], scope=model.scope, at=at)


def belief_evidence_quality(
    belief,
    scope,
    *,
    verify: bool = True,
) -> str:
    refs = tuple(
        str(ref).strip()
        for ref in (getattr(belief, "evidence_refs", ()) or ())
        if str(ref).strip()
    )
    if not refs:
        return "unverified"
    if not verify:
        return "not_checked"
    from core.memory import belief_commit

    states = []
    for ref in refs:
        try:
            states.append(belief_commit.evidence_state(ref, scope))
        except Exception:
            states.append("unknown")
    if states and all(state == "dead" for state in states):
        return "dead"
    if any(state == "alive" for state in states):
        return "alive"
    return "unknown"


def resolve_versions(events, *, scope: dict, at=None):
    """同一版本算法用于现役读取和隔离验收；只解析传入事件，不打开任何库。"""
    from core.memory.belief_commit import fingerprint
    stamp = _date(at) if at is not None else datetime.now()
    versions = []
    latest = {}
    identity = tuple(scope.get(key, "") for key in ("platform", "account_id", "chat_id"))
    for event in events:
        declared_scope = str(event.payload.get("scope") or "")
        if declared_scope and all(identity) and declared_scope != ":".join(identity):
            continue
        key = str(event.payload.get("key") or "")
        if event.kind == "fact_revoked":
            for index, belief in enumerate(versions):
                if belief.key == key:
                    versions[index] = replace(belief, status="revoked")
            continue
        if event.kind != "fact_asserted" or not key:
            continue
        belief = project([event]).beliefs[key]
        previous = latest.get(belief.supersedes) if belief.supersedes != key else None
        target = str(event.payload.get("supersedes_fingerprint") or "")
        if target:
            previous = next((index for index, item in enumerate(versions) if fingerprint(item) == target), None)
        if previous is not None:
            old = versions[previous]
            compatible = all(not left or not right or left == right for left, right in ((old.subject_key, belief.subject_key), (old.predicate, belief.predicate)))
            if compatible:
                change = str(event.payload.get("change_kind") or "correction")
                if change == "time_change":
                    boundary = belief.valid_from or event.at
                    versions[previous] = replace(old, valid_to=boundary)
                    belief = replace(belief, valid_from=boundary)
                else:
                    versions[previous] = replace(old, status="superseded")
                    belief = replace(belief, valid_from=belief.valid_from or old.valid_from or old.as_of)
        latest[key] = len(versions)
        versions.append(belief)
    selected = []
    for belief in versions:
        if belief.status != "active":
            continue
        try:
            start = _date(belief.valid_from) if belief.valid_from else None
            if start is None and belief.as_of:
                try:
                    start = _date(belief.as_of)
                except ValueError as exc:
                    skips.skip("beliefs.resolve_versions:as_of", "malformed", repr(exc)[:120])
            end = _date(belief.valid_to) if belief.valid_to else None
        except (ValueError, TypeError) as exc:
            skips.skip("beliefs.resolve_versions:row", "malformed", repr(exc)[:120])
            continue
        if (start is None or start <= stamp) and (end is None or stamp < end):
            selected.append(belief)
    return tuple(selected)


class BeliefMemoryProvider:
    kind = "belief"

    def __init__(self, *, verify_evidence: bool = False) -> None:
        self.verify_evidence = bool(verify_evidence)

    def _evidence_quality(self, belief, scope) -> str:
        return belief_evidence_quality(
            belief,
            scope,
            verify=self.verify_evidence,
        )

    def recall(self, principal, query="", *, limit=8, context=None):
        from core.terminal.principal import PrincipalContext
        from core.memory.index_scope import context_contradicts
        from core.context.cognitive_commit import Scope
        from core.context.situation_store import get_world_model
        if not isinstance(principal, PrincipalContext) or context_contradicts(context, principal):
            return []
        scope = Scope(principal.platform, principal.bot_account_id, principal.chat_id)
        if not scope.complete:
            return []
        model = get_world_model(scope.chat_id, scope=scope)
        at = (getattr(context, "extra", None) or {}).get("belief_as_of") or None
        beliefs = effective_beliefs(model, at=at)
        if at is not None:
            dated = []
            for belief in beliefs:
                try:
                    _date(belief.valid_from or belief.as_of)
                except (ValueError, TypeError) as exc:
                    skips.skip("beliefs.recall:valid_from", "malformed", repr(exc)[:120])
                    continue  # 起点不明的旧记录不能被断言为任意历史时点有效。
                dated.append(belief)
            beliefs = tuple(dated)
        qualified = []
        for belief in beliefs:
            quality = self._evidence_quality(belief, scope)
            if quality == "dead":
                continue
            qualified.append((belief, quality))
        # 相互冲突的可见来源并列；先解析所有版本再匹配关键词，不能搜回失效旧值。
        groups = {}
        for belief, _ in qualified:
            subject = (belief.subject_key or belief.key, belief.predicate)
            groups.setdefault(subject, set()).add(belief.value)
        from core.memory.providers import _match  # 同一套中文分词，别在这里再写一遍
        hits = []
        for belief, quality in qualified:
            content = f"{belief_label(belief)} = {belief.value}"
            if query and not _match(query, content):
                continue
            conflict = len(groups[(belief.subject_key or belief.key, belief.predicate)]) > 1
            raw = json.dumps([scope.key, asdict(belief), conflict], ensure_ascii=False, sort_keys=True)
            try:
                recorded = _date(belief.as_of) if belief.as_of else None
            except ValueError:
                recorded = None
            hits.append(MemoryItem(
                memory_id="belief:" + hashlib.sha256(raw.encode()).hexdigest(), kind=self.kind, occurred_at=recorded,
                content=(
                    ("[冲突待核对] " if conflict else "")
                    + (
                        "[未核实旧记录] "
                        if quality in {"unverified", "unknown"}
                        else ""
                    )
                    + content
                ),
                platform=scope.platform, bot_account_id=scope.account_id, chat_id=scope.chat_id,
                chat_type=str(getattr(principal.chat_type, "value", principal.chat_type)),
                speaker=belief.who, source_event_ids=belief.evidence_refs,
                confidence=belief.confidence, metadata={"belief_key": belief.key, "subject_key": belief.subject_key or belief.key, "predicate": belief.predicate, "belief_as_of": str(at or ""), "valid_from": belief.valid_from, "valid_to": belief.valid_to, "conflict": conflict, "source": belief.source, "evidence_state": quality},
            ))
        if query:
            from core.memory.providers import _tokens
            q = _tokens(query)
            hits.sort(key=lambda item: len(q & _tokens(item.content)), reverse=True)
        return hits[:max(1, int(limit))]
