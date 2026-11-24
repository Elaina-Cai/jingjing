# -*- coding: utf-8 -*-
"""巩固环（批D Step 38）：②里高显著的情节 → ⑨长期记忆（memory_projections）。

* off（默认）一行不走、零读；shadow 只审计候选（consolidation_shadow）；
* on 才幂等 upsert：memory_id 由 (chat_id, kind, label) 稳定派生，重复扫描不重复写；
* 只读②事件流（经 data_channels），不调 LLM、不写②、不写业务判断；
* 候选只取「已了结/可核验」的事件 kind，避免把进行中的噪音沉淀成长期记忆。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

import structlog
from safelog import warn

log = structlog.get_logger()

__all__ = [
    "MAX_ITEMS",
    "MIN_SCORE",
    "SOURCE_KINDS",
    "Candidate",
    "collect_candidates",
    "consolidation_mode",
    "run_consolidation",
]

MIN_SCORE = 0.35
MAX_ITEMS = 5
SOURCE_KINDS = (
    "episode_closed",
    "position_observed",
    "hypothesis_confirmed",
    "hypothesis_contradicted",
    "fact_asserted",
)
_KIND_LABELS = {
    "episode_closed": "片断",
    "position_observed": "关系",
    "hypothesis_confirmed": "已证实",
    "hypothesis_contradicted": "已推翻",
    "fact_asserted": "事实",
}


@dataclass(frozen=True)
class Candidate:
    chat_id: str
    kind: str
    label: str
    score: float
    at: str = ""
    platform: str = ""
    account_id: str = ""
    chat_type: str = ""
    source_event_ids: tuple[str, ...] = ()
    visibility_scope: tuple[str, ...] = ()

    @property
    def memory_id(self) -> str:
        return memory_id(str((self.platform, self.account_id, self.chat_id)) if self.platform and self.account_id else self.chat_id, self.kind, self.label)


def consolidation_mode() -> str:
    """CONSOLIDATION 三态：off / shadow / on；读不到按 off，不阻断。"""
    try:
        from core import features

        return features.mode(features.CONSOLIDATION, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到按 off
        warn("core/quality/consolidation.py:67 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def memory_id(chat_id: str, kind: str, label: str) -> str:
    raw = "|".join([str(chat_id or ""), str(kind or ""), str(label or "")])
    return "cons-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _visibility_of(payload: Mapping[str, Any], scope: Mapping[str, Any]) -> tuple[str, ...]:
    """真实可见范围：只接受显式声明，否则按会话类型收紧，绝不回退 public。"""
    raw = payload.get("visibility_scope") or payload.get("visibility")
    if isinstance(raw, str):
        raw = (raw,)
    if isinstance(raw, (list, tuple, set, frozenset)):
        allowed = {"owner_only", "issuer_only", "group_members", "self"}
        values = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in raw
                if str(item).strip() in allowed
            )
        )
        if values:
            return values
    declared = str(payload.get("scope") or "").strip().lower()
    chat_type = str(scope.get("chat_type") or "").strip().lower()
    if declared in ("group", "group_members", "groupchat") or chat_type in ("group", "groupchat"):
        return ("group_members",)
    return ("owner_only",)


def collect_candidates(
    models: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    min_score: float = MIN_SCORE,
    max_items: int = MAX_ITEMS,
) -> tuple[Candidate, ...]:
    """从②事件流挑可沉淀的高显著事件（确定、可回放；不写任何东西）。"""
    from core.context.salience import top_salient

    found: dict[tuple[str, str, str], Candidate] = {}
    for identifier, model in (models or {}).items():
        scope = dict(getattr(model, "scope", {}) or {})
        chat_id = str(scope.get("chat_id") or identifier or "").strip()
        platform = str(scope.get("platform") or "").strip()
        account_id = str(scope.get("account_id") or "").strip()
        if not (chat_id and platform and account_id):
            continue
        items = top_salient(
            getattr(model, "events", ()) or (),
            limit=max(1, int(max_items)) * 4,
            now=now,
        )
        for item in items:
            kind = str(getattr(item, "kind", "") or "")
            score = float(getattr(item, "score", 0.0) or 0.0)
            if kind not in SOURCE_KINDS or score < float(min_score):
                continue
            label = " ".join(str(getattr(item, "label", "") or "").split())[:120]
            if not label:
                continue
            payload = (
                dict(getattr(item, "payload", {}) or {})
                if isinstance(getattr(item, "payload", None), Mapping)
                else {}
            )
            source_ids = tuple(
                str(value).strip()
                for value in (getattr(item, "source_ids", ()) or ())
                if str(value).strip()
            )
            if not source_ids:
                digest = hashlib.sha1(
                    "|".join([str(chat_id), kind, label, str(getattr(item, "at", "") or "")]).encode("utf-8")
                ).hexdigest()[:16]
                source_ids = ("world:" + digest,)
            key = (platform, account_id, chat_id, kind, label)
            found.setdefault(
                key,
                Candidate(
                    chat_id=chat_id,
                    platform=platform,
                    account_id=account_id,
                    kind=kind,
                    label=label,
                    score=round(score, 3),
                    at=str(getattr(item, "at", "") or ""),
                    chat_type=str(scope.get("chat_type") or ""),
                    source_event_ids=source_ids,
                    visibility_scope=_visibility_of(payload, scope),
                ),
            )
    ordered = sorted(found.values(), key=lambda item: (-item.score, item.chat_id, item.kind, item.at))
    return tuple(ordered[: max(1, int(max_items))])


def _parse_at(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        from core.temporal import parse_naive_local

        return parse_naive_local(text)
    except Exception:  # noqa: BLE001 - 时区怪值 -> 不写时间
        warn("core/quality/consolidation.py:124 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def _event_of(candidate: Candidate) -> Any:
    from core.terminal.event_model import Event, EventType
    from core.terminal.principal import VisibilityScope

    scopes: list[VisibilityScope] = []
    valid_scopes = {scope.value for scope in VisibilityScope}
    for value in candidate.visibility_scope or ("owner_only",):
        text = str(value)
        if text in valid_scopes:
            scopes.append(VisibilityScope(text))
    if not scopes:
        scopes = [VisibilityScope.OWNER_ONLY]
    label_kind = _KIND_LABELS.get(candidate.kind, candidate.kind)
    return Event.new(
        event_type=EventType.MEMORY_WRITE,
        event_id="consolidation:" + candidate.memory_id,
        occurred_at=_parse_at(candidate.at),
        platform=candidate.platform,
        bot_account_id=candidate.account_id,
        chat_id=candidate.chat_id,
        chat_type=candidate.chat_type,
        content=f"[{label_kind}] {candidate.label}"[:200],
        visibility_scope=frozenset(scopes),
        metadata={
            "memory_type": "episodic",
            "importance": min(0.8, 0.45 + float(candidate.score) * 0.5),
            "source": "consolidation",
            "memory_id": candidate.memory_id,
            "source_event_ids": list(candidate.source_event_ids),
            "kind": candidate.kind,
            "salience": round(float(candidate.score), 3),
            "consolidated_by": "batch_d_step38",
        },
    )


def _default_repo() -> Any:
    from core.terminal.store_port import get_meta_repo
    from db.repo_memory_projections import MemoryProjectionRepository

    return MemoryProjectionRepository(get_meta_repo().engine)


def run_consolidation(
    *,
    models: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    repo: Any = None,
    store: Any = None,
    mode: str = "",
) -> dict[str, Any]:
    """一次巩固扫描；off 零读；shadow 只审计；on 幂等写入长期记忆。"""
    resolved = mode or consolidation_mode()
    if resolved == "off":
        return {"mode": "off", "candidates": 0, "written": 0}
    repository = None
    if resolved != "shadow":
        repository = repo or _default_repo()
        from core.memory.legacy_write_freeze import projection_retired
        if projection_retired(repository):
            return {"mode": resolved, "status": "retired", "reason": "legacy_projection_writes_frozen", "candidates": 0, "written": 0}
    if models is None:
        from core.execution import data_channels

        models = data_channels.known_worlds()
    candidates = collect_candidates(models, now=now)
    if resolved == "shadow":
        if candidates:
            log.info(
                "consolidation_shadow",
                mode=resolved,
                candidates=len(candidates),
                kinds=[item.kind for item in candidates],
                chats=sorted({item.chat_id for item in candidates}),
            )
        return {"mode": resolved, "candidates": len(candidates), "written": 0}
    if not candidates:
        return {"mode": resolved, "candidates": 0, "written": 0}
    if store is None:
        from core.terminal.event_store_db import TerminalEventStore

        store = TerminalEventStore(repository.write_engine())
    from core.memory.explicit_write import commit_explicit

    written = 0
    for candidate in candidates:
        try:
            if repository.get(candidate.memory_id) is not None:
                continue
            commit_explicit(_event_of(candidate), store=store, repository=repository)
            written += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垓整轮
            log.warning(
                "consolidation_item_failed",
                memory_id=candidate.memory_id,
                error=f"{type(exc).__name__}: {exc}"[:160],
            )
    if candidates:
        log.info("consolidation_done", mode=resolved, candidates=len(candidates), written=written)
    return {"mode": resolved, "candidates": len(candidates), "written": written}
