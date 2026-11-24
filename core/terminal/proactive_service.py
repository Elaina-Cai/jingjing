"""Phase 4 proactive whitelist + frequency policy.

A proactive send is allowed only when the owner explicitly whitelisted the
conversation and the target's own quiet-hours / cooldown / hourly / daily caps
still allow it.  Normal replies are never affected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog

from core.rules import quota_gate
from core.terminal.principal import PrincipalContext
from core.infra import skips
from safelog import warn

log = structlog.get_logger()


@dataclass(frozen=True)
class ProactiveDecision:
    allowed: bool
    reason: str = ""
    target: Any = None


def _builtin_target_rule(target: Any, kind: str, quiet: list[int]):
    """把 ``proactive_targets`` 那一行折成引擎的一条 ``proactive_send`` 规则。

    **语义逐字对齐接线前的 if 链**（``target`` 维收 enabled / allow_sticker /
    白名单，``condition`` 维收静默时段，``quota`` 维收冷却与小时/每日上限），
    这样 ``YINYUE_FEATURE_RULES=off`` 与 ``on`` 对同一行的结论相同——差别只在
    "判据住在哪"：以前散在本模块，现在只有 ``core/rules/engine.py`` 一处。
    """
    from core.rules.engine import Condition, Quota, Rule, RuleAction, Target

    if target is None:
        return Rule(
            key="builtin:proactive.not_whitelisted",
            name="主动开口白名单（代码兜底）",
            action=RuleAction(action_id="proactive_send", effect="deny"),
            priority=20_000,
        )

    effect = "allow"
    if not int(getattr(target, "enabled", 0) or 0):
        effect = "deny"
    if str(kind or "text") == "sticker" and not int(
        getattr(target, "allow_sticker", 1) or 0
    ):
        effect = "deny"

    window = None
    if len(quiet) == 2:
        window = (f"{int(quiet[0]):02d}:00", f"{int(quiet[1]):02d}:00")

    return Rule(
        key="builtin:proactive.target_row",
        name="主动开口阈值（代码兜底，来源 proactive_targets）",
        scope_type="chat",
        scope_id=str(getattr(target, "chat_id", "") or ""),
        condition=Condition(time_window=window),
        target=Target(role=""),
        quota=Quota(
            per_hour=int(getattr(target, "max_per_hour", 0) or 0),
            per_day=int(getattr(target, "max_per_day", 0) or 0),
            cooldown_seconds=int(getattr(target, "cooldown_minutes", 0) or 0) * 60,
        ),
        action=RuleAction(action_id="proactive_send", effect=effect),
        priority=10_000,
    )


class ProactivePolicyService:
    def __init__(self, meta_repo: Any):
        self.meta_repo = meta_repo

    def _target(self, platform: str, bot_account_id: str, chat_id: str) -> Any:
        try:
            return self.meta_repo.proactive.get_target(
                platform, bot_account_id, chat_id
            )
        except Exception:
            warn("core/terminal/proactive_service.py:85 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None

    @staticmethod
    def _quiet_hours(target: Any) -> list[int]:
        """Return ``[start_hour, end_hour)`` or ``[]``.

        ``start == end`` is treated as "no quiet hours" rather than an
        all-day wrap; malformed or out-of-range values also fall back to [].
        """
        try:
            value = json.loads(getattr(target, "quiet_hours_json", "[]") or "[]")
            if isinstance(value, list) and len(value) >= 2:
                hours = [int(item) for item in value[:2]]
                if hours[0] == hours[1]:
                    return []
                if all(0 <= hour <= 23 for hour in hours):
                    return hours
        except Exception:
            warn("core/terminal/proactive_service.py:103 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []

    def evaluate(
        self,
        *,
        platform: str,
        bot_account_id: str,
        chat_id: str,
        chat_type: str = "",
        kind: str = "text",
        principal: PrincipalContext | None = None,
        now: datetime | None = None,
    ) -> ProactiveDecision:
        target = self._target(platform, bot_account_id, chat_id)
        current = now or datetime.now()
        quiet = self._quiet_hours(target)

        # N4 收编点 ⑨⑩⑪（G1-13）：白名单/启用/表情开关（``target`` 维）、
        # 静默时段（``condition`` 维）、冷却与小时/每日上限（``quota`` 维）的
        # **判据只有一处**：``core/rules/engine.py``。这里只把事实（目标行 +
        # 既有计数）折进去，不再自己写第二份 if 链。
        #
        # 数据面上有 ``proactive_send`` 规则行 → 用数据行（主人可在 Web 上改
        # 阈值，改一行即生效）；没有 → 用 :func:`_builtin_target_rule` 把
        # ``proactive_targets`` 那一行折成引擎规则，**语义与接线前逐字节相同**。
        # 引擎 off（默认）时一律放行，走到下面仍是同一条结论。
        engine_reason = self._engine_reject(
            target=target, platform=platform, bot_account_id=bot_account_id,
            chat_id=chat_id, chat_type=chat_type, kind=kind, quiet=quiet,
            current=current,
        )
        if engine_reason is not None:
            return ProactiveDecision(False, engine_reason, target)

        if target is None:
            return ProactiveDecision(False, "not_whitelisted")
        if not int(getattr(target, "enabled", 0) or 0):
            return ProactiveDecision(False, "disabled", target)
        if kind == "sticker" and not int(getattr(target, "allow_sticker", 1) or 0):
            return ProactiveDecision(False, "sticker_not_allowed", target)

        if len(quiet) == 2:
            start, end = quiet
            hour = current.hour
            in_quiet = (
                start <= hour < end
                if start < end
                else (hour >= start or hour < end)
            )
            if in_quiet:
                return ProactiveDecision(False, "quiet_hours", target)

        cooldown = int(getattr(target, "cooldown_minutes", 0) or 0)
        if cooldown > 0:
            # PIT-111：间隔只问一本流水（core.infra.actions）——DB 的 last_send_at
            # 只记主动搭话，看不见她刚被动回过的这一句，于是「刚回完又搭一句」。
            quiet = None
            try:
                from core.infra import actions as _actions

                quiet = _actions.quiet_ms(
                    chat_id, scope=(platform, bot_account_id, chat_id),
                )
            except Exception:  # noqa: BLE001 - 读不出来就说不知道，不许当「没发过」
                skips.skip("proactive_service.cooldown:quiet", "degraded",
                           repr(__import__("sys").exc_info()[1])[:120])
                quiet = None
            if quiet is not None and quiet < cooldown * 60_000:
                return ProactiveDecision(False, "cooldown", target)

        try:
            hour_count = self.meta_repo.proactive.count_since(
                platform=platform,
                bot_account_id=bot_account_id,
                chat_id=chat_id,
                since=current - timedelta(hours=1),
            )
        except Exception:
            warn("core/terminal/proactive_service.py:175 Exception", err=repr(__import__("sys").exc_info()[1]))
            hour_count = 0
        guard_key = f"{platform}|{bot_account_id}|{chat_id}"
        max_hour = int(getattr(target, "max_per_hour", 0) or 0)
        if quota_gate.observe(guard_key, "hour", hour_count, max_hour) == "blocked":
            return ProactiveDecision(False, "hourly_limit", target)

        try:
            day_count = self.meta_repo.proactive.count_since(
                platform=platform,
                bot_account_id=bot_account_id,
                chat_id=chat_id,
                since=current - timedelta(days=1),
            )
        except Exception:
            warn("core/terminal/proactive_service.py:188 Exception", err=repr(__import__("sys").exc_info()[1]))
            day_count = 0
        max_day = int(getattr(target, "max_per_day", 0) or 0)
        if quota_gate.observe(guard_key, "day", day_count, max_day) == "blocked":
            return ProactiveDecision(False, "daily_limit", target)

        # The owner-managed DB row is the authoritative explicit grant.
        # ``principal`` is accepted for compatibility/diagnostics, but must not
        # become a second stale source of truth: in a long-lived process a
        # whitelist write via Web/CLI can reach the DB before the cached
        # IdentityService rebuilds.  The gateway therefore treats this DB
        # lookup as the final authority.
        return ProactiveDecision(True, "allowed", target)

    # ------------------------------------------------------------------
    # N4 收编点 ⑨⑩⑪：主动开口的"能不能发"由规则引擎判
    # ------------------------------------------------------------------
    def _engine_reject(
        self, *, target: Any, platform: str, bot_account_id: str, chat_id: str,
        chat_type: str, kind: str, quiet: list[int], current: datetime,
    ) -> str | None:
        """引擎判为拒绝 → 返回**既有**的拒绝原因码；放行/不可用 → ``None``。

        原因码逐字沿用本模块历史上的那七个（``not_whitelisted`` / ``disabled`` /
        ``sticker_not_allowed`` / ``quiet_hours`` / ``cooldown`` / ``hourly_limit`` /
        ``daily_limit``）：它们在 ``web/routes_proactive.py`` 与看板上是**对外
        契约**，收编判定不该顺手改文案。

        事实全在这里凑齐（``proactive_targets`` 那一行 + 既有计数），**判据全在
        引擎**：目标行折成 ``condition``/``target``/``quota`` 三维的内置规则交给
        ``RuleEngine.builtin_rules``；数据面上出现 ``proactive_send`` 规则行时
        **数据行优先**（主人可在 Web 上改阈值，改一行即生效）。引擎 ``off``
        （默认）时本函数直接返回 ``None``，``evaluate`` 走原判据——逐字节一致。
        """
        try:
            from core.rules import engine as rules_engine
        except Exception:  # noqa: BLE001 - 引擎不可导入就退回原判据
            warn("core/terminal/proactive_service.py:224 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None
        if rules_engine.rules_mode() == "off":
            return None

        is_group = str(chat_type or "") == "group"
        action_ctx = rules_engine.ActionContext(
            action_id="proactive_send",
            actor_id=str(bot_account_id or ""),
            chat_id=str(chat_id or ""),
            chat_type="group" if is_group else "private",
            platform=str(platform or ""),
            account_id=str(bot_account_id or ""),
            is_group=is_group,
            now=current,
            params={"kind": str(kind or "text")},
            quota_counts=rules_engine.QuotaCounts(
                counts={
                    "hour": self._count(platform, bot_account_id, chat_id, current, hours=1),
                    "day": self._count(platform, bot_account_id, chat_id, current, days=1),
                },
                last_use=self._last_send(platform, bot_account_id, chat_id),
            ),
        )
        try:
            engine = rules_engine.get_engine()
            # 数据面上有 ``proactive_send`` 规则行 → **数据行优先**（主人可在 Web
            # 上改阈值，改一行即生效）；没有 → 用 ``proactive_targets`` 折成的
            # 内置规则（语义与接线前的 if 链逐字相同）。
            data_rows = [
                r for r in engine.rules() if r.action.action_id == "proactive_send"
            ]
            rows = data_rows or [_builtin_target_rule(target, kind, quiet)]
            decision = engine.decide_with_rows(action_ctx, rows)
        except Exception as exc:  # noqa: BLE001 - 判定异常不挡主动开口（同 output_gate）
            log.warning("proactive_rule_decision_failed", error=str(exc)[:160])
            return None
        if decision.allowed:
            return None
        # ``exceeded`` 是引擎给的窗口名；映射回本模块对外的原因码。
        return {
            "cooldown": "cooldown",
            "hour": "hourly_limit",
            "day": "daily_limit",
        }.get(decision.exceeded, "disabled")

    def _count(self, platform: str, bot_account_id: str, chat_id: str,
               current: datetime, *, hours: int = 0, days: int = 0) -> int:
        try:
            return int(self.meta_repo.proactive.count_since(
                platform=platform,
                bot_account_id=bot_account_id,
                chat_id=chat_id,
                since=current - timedelta(hours=hours, days=days),
            ))
        except Exception:  # noqa: BLE001 - 读不到计数按 0（不挡）
            warn("core/terminal/proactive_service.py:279 Exception", err=repr(__import__("sys").exc_info()[1]))
            return 0

    def _last_send(self, platform: str, bot_account_id: str,
                   chat_id: str) -> datetime | None:
        try:
            return self.meta_repo.proactive.last_send_at(
                platform, bot_account_id, chat_id
            )
        except Exception:  # noqa: BLE001
            warn("core/terminal/proactive_service.py:288 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None

    def record_send(
        self,
        *,
        platform: str,
        bot_account_id: str,
        chat_id: str,
        kind: str = "text",
        trigger: str = "proactive",
        content_preview: str = "",
        ok: bool = True,
        error: str = "",
    ) -> None:
        try:
            self.meta_repo.proactive.record_send(
                platform=platform,
                bot_account_id=bot_account_id,
                chat_id=chat_id,
                kind=kind,
                trigger=trigger,
                content_preview=content_preview,
                ok=ok,
                error=error,
            )
        except Exception:
            warn("core/terminal/proactive_service.py:314 Exception", err=repr(__import__("sys").exc_info()[1]))


def build_proactive_service() -> ProactivePolicyService | None:
    try:
        from core.terminal.store_port import get_meta_repo

        return ProactivePolicyService(get_meta_repo())
    except Exception:
        warn("core/terminal/proactive_service.py:323 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
