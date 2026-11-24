"""Phase 4 automatic "when should she speak first?" worker.

The explicit path already exists::

    emit_event(PROACTIVE_TRIGGER) -> run_proactive_turn -> OutboundGateway

This module supplies the missing half: a conservative, deterministic scanner
that looks at owner-whitelisted conversations, finds ones that have been idle
long enough to be worth a look, and publishes ``PROACTIVE_TRIGGER`` for them.
The model turn behind that event still owns the actual decision (it may answer
``[静默]``) and every send is re-checked by the owner-managed whitelist,
quiet-hours and frequency gates.

Safety properties
-----------------
* Always on after Phase 5 D; ``YINYUE_PROACTIVE_*`` params keep it conservative.
* Only conversations explicitly present in ``proactive_targets`` are scanned.
* A conversation must have real message history inside the lookback window and
  must have been idle for at least ``min_idle_minutes``.
* Per-target attempts are rate-limited in-process; actual sends are still
  rate-limited by :mod:`core.terminal.proactive_service` and the gateway.
* Model/tool errors in the trigger chain never crash the scheduler loop.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

import structlog

from core import temporal
from core.infra.event_bus import Event, EventBus, EventType
from safelog import warn
from core.infra.envflags import text

log = structlog.get_logger()

# Conservative defaults.  All are env-overridable; the feature flag itself is
# the final switch and stays off until the owner deliberately turns it on.
DEFAULT_MIN_IDLE_MINUTES = 180
DEFAULT_HISTORY_DAYS = 14
DEFAULT_ATTEMPT_INTERVAL_MINUTES = 240
DEFAULT_MAX_PER_CYCLE = 2


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        warn("core/terminal/proactive_decision.py:55 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return default


def _field(row: Any, name: str, default: Any = "") -> Any:
    """Read a field from either an ORM row or a plain mapping."""
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            warn("core/terminal/proactive_decision.py:77 ValueError", err=repr(__import__("sys").exc_info()[1]))
            try:
                from core.temporal import parse_datetime

                dt = parse_datetime(text)
            except Exception:
                warn("core/terminal/proactive_decision.py:82 Exception", err=repr(__import__("sys").exc_info()[1]))
                return None
    if dt is None:
        return None
    # Legacy rows are naive local time; tests/projections may hand us aware
    # values.  Compare consistently inside the scanner —— 换算口径唯一 owner
    # = ``core.temporal.to_naive_local``（naive 输入原样返回）。
    return temporal.to_naive_local(dt)


def _format_duration(seconds: float) -> str:
    minutes = max(1, int(round(max(0.0, seconds) / 60.0)))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {rest_minutes} 分钟" if rest_minutes else "")
    days, rest_hours = divmod(hours, 24)
    return f"{days} 天" + (f" {rest_hours} 小时" if rest_hours else "")


@dataclass(frozen=True)
class ProactiveOpportunity:
    platform: str
    bot_account_id: str
    chat_id: str
    chat_type: str
    reason: str
    idle_seconds: float
    last_message_at: datetime | None
    needs_response: bool
    hint: str
    target_id: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "bot_account_id": self.bot_account_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "reason": self.reason,
            "idle_seconds": round(float(self.idle_seconds), 1),
            "idle_text": _format_duration(self.idle_seconds),
            "last_message_at": (
                self.last_message_at.isoformat(timespec="seconds")
                if self.last_message_at else None
            ),
            "needs_response": bool(self.needs_response),
            "target_id": int(self.target_id or 0),
        }


class ProactiveDecisionWorker:
    """Scan whitelisted conversations and publish trigger events.

    All dependencies are injectable so the decision logic can be tested without
    a live bootstrap, model, or outgoing WeChat provider.
    """

    def __init__(
        self,
        *,
        meta_repo: Any,
        runtime: Any,
        policy_service: Any = None,
        message_provider: Callable[[Any, str], Any] | None = None,
        event_store: Any = None,
        now_provider: Callable[[], datetime] | None = None,
        enabled_check: Callable[[], bool] | None = None,
        min_idle_minutes: int = DEFAULT_MIN_IDLE_MINUTES,
        history_days: int = DEFAULT_HISTORY_DAYS,
        attempt_interval_minutes: int = DEFAULT_ATTEMPT_INTERVAL_MINUTES,
        max_per_cycle: int = DEFAULT_MAX_PER_CYCLE,
    ):
        self.meta_repo = meta_repo
        self.runtime = runtime
        self.policy_service = policy_service
        self.message_provider = message_provider
        self.event_store = event_store
        self.now_provider = now_provider or datetime.now
        self.enabled_check = enabled_check
        self.min_idle_seconds = max(1, int(min_idle_minutes)) * 60
        self.history_seconds = max(1, int(history_days)) * 24 * 3600
        self.attempt_interval_seconds = (
            max(1, int(attempt_interval_minutes)) * 60
        )
        self.max_per_cycle = max(1, int(max_per_cycle))
        self._last_attempt_at: dict[tuple[str, str, str], datetime] = {}
        self._guard = threading.RLock()
        self._cycle_running = False

    # ------------------------------------------------------------------ state
    def enabled(self) -> bool:
        if self.enabled_check is not None:
            try:
                return bool(self.enabled_check())
            except Exception:
                warn("core/terminal/proactive_decision.py:178 Exception", err=repr(__import__("sys").exc_info()[1]))
                return False
        return True

    def _now(self) -> datetime:
        try:
            value = self.now_provider()
        except Exception:
            warn("core/terminal/proactive_decision.py:185 Exception", err=repr(__import__("sys").exc_info()[1]))
            value = datetime.now()
        return _as_datetime(value) or datetime.now()

    # ------------------------------------------------------------- candidates
    def _list_targets(self) -> list[Any]:
        provider = getattr(self.meta_repo, "proactive", None)
        if provider is None:
            return []
        try:
            rows = provider.list_targets(enabled_only=True, limit=10000)
        except Exception as exc:  # noqa: BLE001 - scanner is best-effort
            log.debug("proactive_decision_list_targets_failed", error=str(exc)[:120])
            return []
        return list(rows or [])

    @staticmethod
    def _is_bot_context(ctx: Any) -> bool:
        role = str(getattr(ctx, "role", "") or "").strip()
        if role and role != "yinyue":
            return False
        return True

    @staticmethod
    def _send_enabled(ctx: Any) -> bool:
        cfg = getattr(ctx, "config", None)
        if cfg is None:
            return False
        return bool(getattr(cfg, "send_enabled", False))

    def _resolve_contexts(
        self, platform: str, requested_account: str
    ) -> list[Any]:
        runtime = self.runtime
        if runtime is None:
            return []
        requested_account = str(requested_account or "").strip()
        if requested_account:
            try:
                ctx = runtime.get(platform, requested_account)
            except Exception:
                warn("core/terminal/proactive_decision.py:225 Exception", err=repr(__import__("sys").exc_info()[1]))
                ctx = None
            if ctx is not None and self._is_bot_context(ctx):
                return [ctx]
            return []
        try:
            contexts: Iterable[Any] = runtime.by_platform(platform)
        except Exception:
            warn("core/terminal/proactive_decision.py:232 Exception", err=repr(__import__("sys").exc_info()[1]))
            contexts = ()
        return [ctx for ctx in contexts if self._is_bot_context(ctx)]

    def _last_terminal_event(self, ctx: Any, chat_id: str) -> Any | None:
        """Return a message-like dict from the formal event stream, if any.

        The formal stream is the Phase 5 source of truth.  ``messages`` remains
        the fallback for history that predates the event stream rollout.
        """
        store = self.event_store
        if store is None:
            try:
                from core.terminal.event_runtime import get_terminal_event_store

                store = get_terminal_event_store(force=True)
            except Exception:
                warn("core/terminal/proactive_decision.py:248 Exception", err=repr(__import__("sys").exc_info()[1]))
                return None
        if store is None:
            return None
        try:
            from core.terminal.event_model import EventType as TerminalEventType

            events = store.query(
                platform=str(getattr(ctx, "platform", "") or ""),
                bot_account_id=str(getattr(ctx, "account_id", "") or ""),
                chat_id=str(chat_id or ""),
                event_types=(
                    TerminalEventType.MESSAGE,
                    TerminalEventType.QUESTION,
                    TerminalEventType.REPLY,
                ),
                limit=1,
            )
        except Exception:
            warn("core/terminal/proactive_decision.py:266 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None
        if not events:
            return None
        event = events[0]
        event_type = getattr(event, "event_type", "")
        event_type_value = str(getattr(event_type, "value", event_type) or "")
        metadata = getattr(event, "metadata", None)
        if not isinstance(metadata, Mapping):
            metadata = {}
        is_self = (
            event_type_value == "reply"
            or bool(metadata.get("is_self"))
            or (
                str(getattr(event, "actor_account_id", "") or "")
                and str(getattr(event, "actor_account_id", "") or "")
                == str(getattr(ctx, "account_id", "") or "")
                and event_type_value != "question"
            )
        )
        return {
            "chat_id": str(getattr(event, "chat_id", "") or chat_id),
            "created_at": getattr(event, "occurred_at", None),
            "is_self": 1 if is_self else 0,
            "sender_id": str(
                getattr(event, "actor_account_id", "")
                or getattr(event, "actor_person_id", "")
                or ""
            ),
            "content": str(getattr(event, "content", "") or ""),
            "source": "terminal_events",
        }

    def _last_message(self, ctx: Any, chat_id: str) -> Any | None:
        """Return the newest activity for ``chat_id`` or ``None``.

        Prefer the formal ``terminal_events`` stream (Phase 5) and fall back to
        the per-account ``messages`` table for pre-rollout history.
        """
        event_row = self._last_terminal_event(ctx, chat_id)
        if event_row is not None:
            return event_row
        if self.message_provider is not None:
            try:
                return self.message_provider(ctx, chat_id)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "proactive_decision_message_provider_failed",
                    chat_id=chat_id,
                    error=str(exc)[:120],
                )
                return None
        repo = getattr(ctx, "repo", None)
        messages = getattr(repo, "messages", None)
        if messages is None:
            return None
        try:
            rows = messages.get_recent(chat_id=chat_id, limit=1)
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "proactive_decision_message_lookup_failed",
                chat_id=chat_id,
                error=str(exc)[:120],
            )
            return None
        if not rows:
            return None
        try:
            return rows[0]
        except (TypeError, IndexError, KeyError):
            warn("core/terminal/proactive_decision.py:335 (TypeError, IndexError, KeyError)", err=repr(__import__("sys").exc_info()[1]))
            return None

    @staticmethod
    def _message_is_self(ctx: Any, row: Any) -> bool:
        raw = _field(row, "is_self", 0)
        try:
            if int(raw or 0):
                return True
        except (TypeError, ValueError):
            warn("core/terminal/proactive_decision.py:344 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            if bool(raw):
                return True
        sender = str(
            _field(row, "sender_id", "")
            or _field(row, "from_user", "")
            or ""
        )
        account_id = str(getattr(ctx, "account_id", "") or "")
        return bool(sender and account_id and sender == account_id)

    def _policy_allows(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        chat_type: str,
        now: datetime,
    ) -> bool:
        service = self.policy_service
        if service is None:
            try:
                from core.terminal.proactive_service import (
                    build_proactive_service,
                )

                service = build_proactive_service()
            except Exception:
                warn("core/terminal/proactive_decision.py:372 Exception", err=repr(__import__("sys").exc_info()[1]))
                service = None
        if service is None:
            # Fail closed: an unavailable whitelist must never turn into an
            # automatic send.
            return False
        try:
            decision = service.evaluate(
                platform=platform,
                bot_account_id=account_id,
                chat_id=chat_id,
                chat_type=chat_type,
                kind="text",
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "proactive_decision_policy_failed",
                chat_id=chat_id,
                error=str(exc)[:120],
            )
            return False
        return bool(getattr(decision, "allowed", False))

    def _candidate_for_context(
        self,
        *,
        ctx: Any,
        target: Any,
        now: datetime,
    ) -> tuple[float, Any, Any, bool] | None:
        platform = str(getattr(target, "platform", "") or "").strip()
        chat_id = str(getattr(target, "chat_id", "") or "").strip()
        chat_type = (
            "group"
            if str(getattr(target, "chat_type", "") or "").strip().lower()
            == "group"
            else "private"
        )
        account_id = str(getattr(ctx, "account_id", "") or "")
        if not account_id or not self._send_enabled(ctx):
            return None

        row = self._last_message(ctx, chat_id)
        if row is None:
            return None
        last_at = _as_datetime(
            _field(row, "created_at", None)
            or _field(row, "synced_at", None)
        )
        if last_at is None:
            return None
        idle_seconds = (now - last_at).total_seconds()
        if idle_seconds < self.min_idle_seconds:
            return None
        if idle_seconds > self.history_seconds:
            return None

        with self._guard:
            last_attempt = self._last_attempt_at.get(
                (platform, account_id, chat_id)
            )
        if last_attempt is not None:
            if (now - last_attempt).total_seconds() < self.attempt_interval_seconds:
                return None

        if not self._policy_allows(
            platform=platform,
            account_id=account_id,
            chat_id=chat_id,
            chat_type=chat_type,
            now=now,
        ):
            return None

        is_self = self._message_is_self(ctx, row)
        return (idle_seconds, ctx, row, is_self)

    def collect_opportunities(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[ProactiveOpportunity]:
        """Return eligible opportunities, best first.  Pure of side effects."""
        current = _as_datetime(now) or self._now()
        if not self.enabled():
            return []

        opportunities: list[ProactiveOpportunity] = []
        for target in self._list_targets():
            try:
                platform = str(getattr(target, "platform", "") or "").strip()
                chat_id = str(getattr(target, "chat_id", "") or "").strip()
                if not platform or not chat_id:
                    continue
                chat_type = (
                    "group"
                    if str(getattr(target, "chat_type", "") or "").strip().lower()
                    == "group"
                    else "private"
                )
                requested_account = str(
                    getattr(target, "bot_account_id", "") or ""
                ).strip()
                contexts = self._resolve_contexts(platform, requested_account)
                candidates: list[tuple[float, Any, Any, bool]] = []
                for ctx in contexts:
                    candidate = self._candidate_for_context(
                        ctx=ctx, target=target, now=current
                    )
                    if candidate is not None:
                        candidates.append(candidate)
                if not candidates:
                    continue

                # Prefer a conversation where the other side spoke last, then
                # the one that has been waiting longest.
                idle_seconds, ctx, _row, is_self = max(
                    candidates,
                    key=lambda item: (
                        0 if item[3] else 1,
                        item[0],
                        str(getattr(item[1], "account_id", "")),
                    ),
                )
                needs_response = not is_self
                opportunity = ProactiveOpportunity(
                    platform=platform,
                    bot_account_id=str(getattr(ctx, "account_id", "") or ""),
                    chat_id=chat_id,
                    chat_type=chat_type,
                    reason=(
                        "unanswered_incoming"
                        if needs_response else "idle_followup"
                    ),
                    idle_seconds=idle_seconds,
                    last_message_at=_as_datetime(
                        _field(_row, "created_at", None)
                        or _field(_row, "synced_at", None)
                    ),
                    needs_response=needs_response,
                    hint=self._build_hint(
                        chat_type=chat_type,
                        idle_seconds=idle_seconds,
                        needs_response=needs_response,
                    ),
                    target_id=int(getattr(target, "id", 0) or 0),
                )
                opportunities.append(opportunity)
            except Exception as exc:  # noqa: BLE001 - one bad target never stops scan
                log.debug(
                    "proactive_decision_target_failed",
                    target_id=getattr(target, "id", 0),
                    error=str(exc)[:120],
                )

        opportunities.sort(
            key=lambda opp: (
                0 if opp.needs_response else 1,
                -opp.idle_seconds,
                opp.platform,
                opp.bot_account_id,
                opp.chat_id,
            )
        )
        return opportunities[: max(1, int(limit or self.max_per_cycle))]

    @staticmethod
    def _build_hint(
        *, chat_type: str, idle_seconds: float, needs_response: bool
    ) -> str:
        where = "这个群" if chat_type == "group" else "这个会话"
        if needs_response:
            situation = "上一次对话停在对方的消息，之后一直没有新的对话。"
        else:
            situation = "上一次是你先说的话，之后对方没有再回应。"
        return (
            "[系统触发 · 主动触达机会]\n"
            f"现在没有新消息，{where}已经安静了约 {_format_duration(idle_seconds)}。"
            f"{situation}\n"
            "主人已经把这里加入主动触达白名单，说明他愿意收到你的主动消息。\n"
            "结合当前会话历史、长期记忆和关系状态，主动找一个自然、有分寸的开场；"
            "优先说一句具体的话，合适时可以发表情。"
            "只有明显会冒犯、打扰或不合时宜时才输出 [静默]，不要解释这段规则。"
        )

    # --------------------------------------------------------------- dispatch
    def _publish(self, opportunity: ProactiveOpportunity, now: datetime) -> bool:
        try:
            EventBus.publish(
                Event(
                    type=EventType.PROACTIVE_TRIGGER,
                    timestamp=now,
                    payload={
                        "platform": opportunity.platform,
                        "bot_account_id": opportunity.bot_account_id,
                        "chat_id": opportunity.chat_id,
                        "chat_type": opportunity.chat_type,
                        "hint": opportunity.hint,
                        "source": "decision_worker",
                        "reason": opportunity.reason,
                    },
                    source="proactive_decision",
                    platform=opportunity.platform,
                    account_id=opportunity.bot_account_id,
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001 - never kill the queue worker
            log.warning(
                "proactive_decision_publish_failed",
                chat_id=opportunity.chat_id,
                error=str(exc)[:160],
            )
            return False

    async def run_cycle(self) -> dict[str, Any]:
        """One bounded scan.  Safe to call from a scheduler/background job."""
        with self._guard:
            if self._cycle_running:
                return {"enabled": self.enabled(), "skipped": "busy"}
            self._cycle_running = True
        try:
            if not self.enabled():
                return {"enabled": False, "dispatched": 0, "candidates": 0}
            now = self._now()
            try:
                opportunities = await asyncio.to_thread(
                    self.collect_opportunities,
                    now=now,
                    limit=self.max_per_cycle,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "proactive_decision_scan_failed", error=str(exc)[:160]
                )
                return {"enabled": True, "dispatched": 0, "error": str(exc)[:160]}

            dispatched: list[dict[str, Any]] = []
            for opportunity in opportunities:
                if not self._publish(opportunity, now):
                    continue
                key = (
                    opportunity.platform,
                    opportunity.bot_account_id,
                    opportunity.chat_id,
                )
                with self._guard:
                    self._last_attempt_at[key] = now
                dispatched.append(opportunity.as_dict())
                log.info(
                    "proactive_opportunity",
                    platform=opportunity.platform,
                    account_id=opportunity.bot_account_id,
                    chat_id=opportunity.chat_id,
                    chat_type=opportunity.chat_type,
                    reason=opportunity.reason,
                    idle_text=_format_duration(opportunity.idle_seconds),
                )
            if dispatched:
                timeout = max(
                    5.0,
                    float(
                        text("YINYUE_PROACTIVE_TRIGGER_TIMEOUT_SECONDS", "600")
                        or 600
                    ),
                )
                try:
                    await EventBus.await_pending(timeout=timeout)
                except Exception as exc:  # noqa: BLE001
                    log.debug(
                        "proactive_trigger_wait_failed", error=str(exc)[:120]
                    )
            return {
                "enabled": True,
                "candidates": len(opportunities),
                "dispatched": len(dispatched),
                "opportunities": dispatched,
            }
        finally:
            with self._guard:
                self._cycle_running = False


_default_worker: ProactiveDecisionWorker | None = None
_default_worker_lock = threading.RLock()


def build_proactive_decision_worker(
    *,
    meta_repo: Any = None,
    runtime: Any = None,
    policy_service: Any = None,
    message_provider: Callable[[Any, str], Any] | None = None,
    enabled_check: Callable[[], bool] | None = None,
) -> ProactiveDecisionWorker | None:
    """Build the production worker from bootstrap singletons.

    Returns ``None`` when the runtime is not initialised yet; the scheduler
    calls this lazily and retries on the next scheduled tick.
    """
    try:
        if meta_repo is None or runtime is None:
            from core.terminal.store_port import get_meta_repo
            from core.bootstrap import get_runtime

            if meta_repo is None:
                meta_repo = get_meta_repo()
            if runtime is None:
                runtime = get_runtime()
        if meta_repo is None or runtime is None:
            return None
        if policy_service is None:
            try:
                from core.terminal.proactive_service import (
                    build_proactive_service,
                )

                policy_service = build_proactive_service()
            except Exception:
                warn("core/terminal/proactive_decision.py:694 Exception", err=repr(__import__("sys").exc_info()[1]))
                policy_service = None
        return ProactiveDecisionWorker(
            meta_repo=meta_repo,
            runtime=runtime,
            policy_service=policy_service,
            message_provider=message_provider,
            enabled_check=enabled_check,
            min_idle_minutes=_env_int(
                "YINYUE_PROACTIVE_MIN_IDLE_MINUTES",
                DEFAULT_MIN_IDLE_MINUTES,
            ),
            history_days=_env_int(
                "YINYUE_PROACTIVE_HISTORY_DAYS",
                DEFAULT_HISTORY_DAYS,
            ),
            attempt_interval_minutes=_env_int(
                "YINYUE_PROACTIVE_ATTEMPT_INTERVAL_MINUTES",
                DEFAULT_ATTEMPT_INTERVAL_MINUTES,
            ),
            max_per_cycle=_env_int(
                "YINYUE_PROACTIVE_MAX_PER_CYCLE",
                DEFAULT_MAX_PER_CYCLE,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - scheduler keeps running
        log.warning("proactive_decision_build_failed", error=str(exc)[:160])
        return None


def get_proactive_decision_worker() -> ProactiveDecisionWorker | None:
    """Return the process-wide worker, building it on first use."""
    global _default_worker
    with _default_worker_lock:
        if _default_worker is None:
            _default_worker = build_proactive_decision_worker()
        return _default_worker


def reset_proactive_decision_worker() -> None:
    global _default_worker
    with _default_worker_lock:
        _default_worker = None


__all__ = [
    "DEFAULT_ATTEMPT_INTERVAL_MINUTES",
    "DEFAULT_HISTORY_DAYS",
    "DEFAULT_MAX_PER_CYCLE",
    "DEFAULT_MIN_IDLE_MINUTES",
    "ProactiveDecisionWorker",
    "ProactiveOpportunity",
    "build_proactive_decision_worker",
    "get_proactive_decision_worker",
    "reset_proactive_decision_worker",
]
