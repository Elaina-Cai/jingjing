"""AnalysisService: the single read entry for analysis results (Phase 5 D2).

Mirrors ``core/memory/service.py`` -- two physical layers, one contract:

* **event layer** -- ``analysis_projections`` (meta DB).  Derived from
  ``terminal_events`` by ``core.analysis.workers``; every item carries
  ``source_event_ids``.
* **legacy layer** -- the frozen account-level ``analysis`` table (old chain,
  no writer since Track B).  Items are returned with ``legacy=True`` so callers
  can display "历史（旧分析链）· 无 provenance" instead of mixing them into
  event-derived results.

Readers (Web routes, agent tools) must depend on this service instead of
touching repositories directly; ``scripts/check_layering.py`` enforces that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from core import temporal
from safelog import warn

_EPOCH = datetime.min


def _aware(value: Any) -> datetime | None:
    """归一为应用时钟（naive 本地墙钟）；换算口径唯一 owner = ``temporal``。"""
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value
    return temporal.to_naive_local(value)


def _allowed(draft: Any, principal: Any) -> bool:
    """Projection visibility check; no principal means internal/admin read."""
    if principal is None:
        return True
    if bool(getattr(principal, "is_owner", False)):
        return True
    scopes = {str(scope) for scope in (getattr(draft, "visibility_scope", ()) or ())}
    return "public" in scopes


@dataclass(frozen=True)
class AnalysisItem:
    item_id: str
    analysis_type: str
    result: str
    created_at: datetime | None = None
    chat_id: str = ""
    platform: str = ""
    bot_account_id: str = ""
    source_event_ids: tuple[str, ...] = ()
    confidence: float = 0.0
    legacy: bool = False
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "chat_id": self.chat_id,
            "type": self.analysis_type,
            "result": self.result,
            "summary": self.summary,
            "created_at": self.created_at.isoformat() if self.created_at else "",
            "platform": self.platform,
            "bot_account_id": self.bot_account_id,
            "source_event_ids": list(self.source_event_ids),
            "confidence": self.confidence,
            "legacy": self.legacy,
        }


class AnalysisService:
    def __init__(self, meta_repo: Any = None, legacy_repo: Any = None):
        self._meta = meta_repo
        self._legacy = legacy_repo

    # ------------------------------------------------------------ layers
    @property
    def projections(self) -> Any:
        if self._meta is None:
            from core.terminal.store_port import get_meta_repo

            self._meta = get_meta_repo()
        return self._meta.analysis_projections

    @staticmethod
    def _from_draft(draft: Any) -> AnalysisItem:
        return AnalysisItem(
            item_id=str(getattr(draft, "projection_id", "") or ""),
            analysis_type=str(getattr(draft, "projection_type", "") or ""),
            result=str(getattr(draft, "content", "") or ""),
            summary=str(getattr(draft, "summary", "") or ""),
            created_at=_aware(getattr(draft, "occurred_at", None)),
            chat_id=str(getattr(draft, "chat_id", "") or ""),
            platform=str(getattr(draft, "platform", "") or ""),
            bot_account_id=str(getattr(draft, "bot_account_id", "") or ""),
            source_event_ids=tuple(
                str(value)
                for value in (getattr(draft, "source_event_ids", ()) or ())
            ),
            confidence=float(getattr(draft, "confidence", 0.0) or 0.0),
            legacy=False,
            metadata=dict(getattr(draft, "metadata", None) or {}),
        )

    @staticmethod
    def _from_legacy(row: Any) -> AnalysisItem:
        return AnalysisItem(
            item_id=str(getattr(row, "id", "") or ""),
            analysis_type=str(getattr(row, "analysis_type", "") or ""),
            result=str(getattr(row, "result", "") or ""),
            created_at=_aware(getattr(row, "created_at", None)),
            chat_id=str(getattr(row, "chat_id", "") or ""),
            legacy=True,
            metadata={"source": "legacy_analysis_table"},
        )

    # ------------------------------------------------------------ reads
    def recent(
        self,
        *,
        chat_id: str | None = None,
        analysis_type: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        platform: str = "",
        account_id: str = "",
        include_legacy: bool = True,
        principal: Any = None,
    ) -> list[AnalysisItem]:
        size = max(1, int(limit or 1))
        items: list[AnalysisItem] = []
        try:
            drafts = self.projections.all(limit=max(size * 5, 500))
        except Exception:
            warn("core/analysis/service.py:141 Exception", err=repr(__import__("sys").exc_info()[1]))
            drafts = []
        for draft in drafts:
            if not _allowed(draft, principal):
                continue
            if analysis_type and str(getattr(draft, "projection_type", "")) != analysis_type:
                continue
            if chat_id and str(getattr(draft, "chat_id", "")) != chat_id:
                continue
            if platform and str(getattr(draft, "platform", "")) != platform:
                continue
            if account_id and str(getattr(draft, "bot_account_id", "")) != account_id:
                continue
            if since is not None and (_aware(getattr(draft, "occurred_at", None)) or _EPOCH) < _aware(since):
                continue
            items.append(self._from_draft(draft))

        is_owner_view = principal is None or bool(getattr(principal, "is_owner", False))
        if include_legacy and self._legacy is not None and is_owner_view:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "analysis_type": analysis_type,
                "limit": size,
            }
            if since is not None:
                kwargs["msg_since"] = since
            try:
                rows = self._legacy.analysis.get_recent(**kwargs)
            except Exception:
                warn("core/analysis/service.py:169 Exception", err=repr(__import__("sys").exc_info()[1]))
                rows = []
            for row in rows or []:
                # Defensive re-check: the repository filters too, but the
                # service is the contract boundary.
                if analysis_type and str(getattr(row, "analysis_type", "")) != analysis_type:
                    continue
                if chat_id and str(getattr(row, "chat_id", "")) != chat_id:
                    continue
                items.append(self._from_legacy(row))

        items.sort(key=lambda item: item.created_at or _EPOCH, reverse=True)
        return items[:size]

    def daily_items(
        self,
        target_date: date,
        *,
        platform: str = "",
        account_id: str = "",
        include_legacy: bool = True,
        principal: Any = None,
    ) -> list[AnalysisItem]:
        items: list[AnalysisItem] = []
        try:
            drafts = self.projections.query_for_date(target_date)
        except Exception:
            warn("core/analysis/service.py:195 Exception", err=repr(__import__("sys").exc_info()[1]))
            drafts = []
        for draft in drafts:
            if str(getattr(draft, "projection_type", "")) != "daily_digest":
                continue
            if not _allowed(draft, principal):
                continue
            if platform and str(getattr(draft, "platform", "")) != platform:
                continue
            if account_id and str(getattr(draft, "bot_account_id", "")) != account_id:
                continue
            items.append(self._from_draft(draft))

        is_owner_view = principal is None or bool(getattr(principal, "is_owner", False))
        if include_legacy and self._legacy is not None and is_owner_view:
            try:
                legacy = self._legacy.analysis.get_daily_digest(target_date)
            except Exception:
                warn("core/analysis/service.py:212 Exception", err=repr(__import__("sys").exc_info()[1]))
                legacy = None
            rows = []
            if legacy is not None:
                rows = list(legacy) if isinstance(legacy, (list, tuple)) else [legacy]
            items.extend(self._from_legacy(row) for row in rows)
        return items

    def digest(
        self,
        target_date: date,
        *,
        platform: str = "",
        account_id: str = "",
        include_legacy: bool = True,
        principal: Any = None,
    ) -> dict[str, Any]:
        items = self.daily_items(
            target_date,
            platform=platform,
            account_id=account_id,
            include_legacy=include_legacy,
            principal=principal,
        )
        has_new = any(not item.legacy for item in items)
        return {
            "date": target_date.isoformat(),
            "items": [item.as_dict() for item in items],
            "legacy": any(item.legacy for item in items),
            "source": "event" if has_new else ("legacy" if items else "empty"),
        }


__all__ = ["AnalysisItem", "AnalysisService"]
