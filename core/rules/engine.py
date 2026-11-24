"""N4 规则引擎：**动作前置条件**的唯一判定点（docs/decisions/架构评估.md §3.4、§6 P1）。

主人口径
--------
「Web-UI 可配的**动作前置条件**」——她执行一个动作之前要满足什么条件（条件 /
对象 / 次数），不满足即不可执行；改一行数据就生效、**不用重启**。

它解决的三件事（乱点 #21 / #28 / G1-13）
---------------------------------------
1. **作用点只停在"说不说"**：``core/speech/resolver.py`` 的 ``conditions_json`` 只
   决定"这条规则命不命中这一轮对话"，没有任何一处拿它去**拒绝一个动作**。
   本模块把它延伸到"做不做"。
2. **12 处动作前置校验各写一份 owner 判定**：``core/speech/guards.py:249-302``
   （``AccountGuard`` / ``ToolGuard``）、``core/terminal/proactive_service.py:56``、
   ``agents/tools/{reminder,memory,person_profile,search,send_sticker}.py``。
   现在**判定只有这里一处**，各处只保留自己的业务参数校验（正文非空、时间格式、
   @对象解析、表情目录等）。
3. **``ToolPolicy.max_calls`` / ``ToolPolicy.require_confirmation`` 零读取方**
   （原 ``core/speech/domain.py:186-190`` 死字段）：读进本引擎的 ``quota`` 维与
   ``action.require_confirmation``；两者在各自 owner 处已删除，不留悬空字段。

五维（R-V2，字段级见 docs/decisions/架构评估.md §3.4 :259）
------------------------------------------------
``condition`` / ``target`` / ``quota`` / ``action`` / ``priority``——落
``policy_rules.payload_json["rules"]``，同一张表、同一条读取通道（
``core/speech/store.py:110`` ``output_gate_payloads`` 的 key 前缀 + ``get_revision()``
缓存），所以**改一行数据即生效**；读不到就退回"不判定"，绝不挡发送（同
``output_gate`` 兜底口径）。

**「未判出」不是一种取值（P0-3）**：``condition`` 维的 ``topic_type`` / ``mood``
只接受真正的意图 / 态度取值。N1 判不出来时写的是 ``未判出``
（``_UNKNOWN_VALUES``）：这里显式不匹配任何规则——
数据行就算把 ``未判出`` 抄进 ``topic_type``，也不许把「没有值」当成一种值接住。

三态开关
--------
``YINYUE_FEATURE_RULES``（``core.features.RULES``，用 :func:`core.features.mode` 读）：

* ``off``    ← **默认**：一行都不走，各调用点走原来的判定，与接线前**逐字节一致**；
* ``shadow`` ：判定 + 留痕 + **记账**，但**不拦**（读数用）；
* ``on``     ：判定生效（拒绝即返回 ``allowed=False``）。

**影子模式不进 ``decide()`` 的返回值（唯一例外：硬禁区）**：:class:`RuleDecision`
的 ``allowed`` 在 shadow 下**通常恒为 ``True``**，想读"本来会拦什么"请看
:attr:`RuleDecision.shadow` 与 :attr:`RuleDecision.explanation`（``core/speech/guards.py``
就是这么用的：``decision.shadow`` 为真时返回 ALLOW）。**唯一例外是硬禁区**
（:data:`HARD_WALL_ACTIONS`；依据 ``docs/decisions/架构评估.md`` §2.1 L5
"硬禁区写死代码不落数据行：金钱·隐私·代发他人·记忆范围"）：命中硬禁区时**无论档位
一律 ``allowed=False`` + ``shadow=True``** —— 安全底线，不受 shadow 影响（影子档也拦）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

import structlog

from core import features
from core.rules import quota_gate
from db import meta_models as mm
from safelog import warn

log = structlog.get_logger()

#: ``policy_rules`` 里本引擎的数据行 key（前缀命中，与 ``output_gate`` 同口径）。
RULES_RULE_KEY = "action_rules"

#: 沿用 N1 契约里「未判出」的占位值（契约模块已退，判据只留这一处）。
_UNKNOWN_VALUES = frozenset({"unknown", "未判出"})


def _is_unknown(value: Any) -> bool:
    return str(value or "").strip() in _UNKNOWN_VALUES

#: 硬禁区（写死代码，**不落数据行、不可配**，docs/decisions/架构评估.md :263）。
#: 任何数据行把它配成 allow 都不算数：:func:`decide` 先看它。
HARD_WALL_ACTIONS: frozenset[str] = frozenset({
    "money_transfer",        # ① 金钱相关动作
    "read_private",          # ② 隐私读取
    "send_delegated_content",  # ③ 代发他人内容（契约 R-H3 / R-V5）
})
_HARD_WALL_EXPLANATION = "硬禁区（不可配置）"

#: 关系角色的合法取值（``target.role``）。
ROLE_OWNER = "owner"
ROLE_MEMBER = "member"
ROLE_GROUP = "group"


# ---------------------------------------------------------------------------
# 五维的数据形状
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Condition:
    """第一维：什么条件下。三个可选子条件，缺省 = 不限制。"""

    time_window: tuple[str, str] | None = None      # [HH:MM, HH:MM)，支持跨午夜
    topic_type: tuple[str, ...] = ()                # intent 取值
    mood: tuple[str, ...] = ()                      # attitude 取值

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Condition":
        data = dict(raw or {})
        window = data.get("time_window")
        parsed_window: tuple[str, str] | None = None
        if isinstance(window, (list, tuple)) and len(window) >= 2:
            start = _norm_hhmm(window[0])
            end = _norm_hhmm(window[1])
            if start and end and start != end:
                parsed_window = (start, end)
        return cls(
            time_window=parsed_window,
            topic_type=_str_tuple(data.get("topic_type")),
            mood=_str_tuple(data.get("mood")),
        )

    @property
    def specified(self) -> int:
        return sum((
            self.time_window is not None,
            bool(self.topic_type),
            bool(self.mood),
        ))

    def matches(self, ctx: "ActionContext") -> bool:
        if self.time_window is not None:
            start, end = self.time_window
            now = _norm_hhmm(ctx.now.strftime("%H:%M")) or "00:00"
            if start < end:
                if not (start <= now < end):
                    return False
            elif not (now >= start or now < end):   # 跨午夜，如 [23:00,07:00)
                return False
        if self.topic_type:
            value = str(ctx.topic_type or "").strip()
            if not value or _is_unknown(value) or value not in self.topic_type:
                return False
        if self.mood:
            value = str(ctx.mood or "").strip()
            if not value or _is_unknown(value) or value not in self.mood:
                return False
        return True


@dataclass(frozen=True)
class Target:
    """第二维：对谁 / 哪个动作。四个可选子条件，缺省 = 不限制。"""

    people: tuple[str, ...] = ()
    chats: tuple[str, ...] = ()
    role: str = ""

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Target":
        data = dict(raw or {})
        role = str(data.get("role") or "").strip()
        return cls(
            people=_str_tuple(data.get("people")),
            chats=_str_tuple(data.get("chats")),
            role=role if role in (ROLE_OWNER, ROLE_MEMBER, ROLE_GROUP) else "",
        )

    @property
    def specified(self) -> int:
        return sum((bool(self.people), bool(self.chats), bool(self.role)))

    def matches(self, ctx: "ActionContext") -> bool:
        if self.people:
            if str(ctx.actor_id or "").strip() not in self.people:
                return False
        if self.chats:
            if str(ctx.chat_id or "").strip() not in self.chats:
                return False
        if self.role:
            if _role_of(ctx) != self.role:
                return False
        return True


@dataclass(frozen=True)
class Quota:
    """第三维：次数。三个可选上限，缺省 = 不限次。

    记账口径：命中本规则的**每一次动作**（除 ``off`` 外都记，见模块 docstring），
    计数窗口 **hour** = 滚动 1 小时、**day** = 滚动 24 小时（与
    ``core/terminal/proactive_service.py`` 既有口径一致，不新造日历窗口）。
    """

    per_hour: int = 0
    per_day: int = 0
    cooldown_seconds: int = 0

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Quota":
        data = dict(raw or {})
        return cls(
            per_hour=max(0, _as_int(data.get("max_per_hour"))),
            per_day=max(0, _as_int(data.get("max_per_day"))),
            cooldown_seconds=max(0, _as_int(data.get("cooldown_seconds"))),
        )

    @property
    def specified(self) -> int:
        return sum((self.per_hour > 0, self.per_day > 0, self.cooldown_seconds > 0))

    @property
    def any_limit(self) -> bool:
        return self.specified > 0


@dataclass(frozen=True)
class RuleAction:
    """第四维：允许还是拒绝（+ 动作身份与是否要确认）。"""

    action_id: str = ""
    effect: str = "allow"                 # allow | deny
    params: dict[str, Any] = field(default_factory=dict)
    require_confirmation: bool = False

    @classmethod
    def from_dict(cls, raw: dict | None) -> "RuleAction":
        data = dict(raw or {})
        effect = str(data.get("effect") or "allow").strip().lower()
        return cls(
            action_id=str(data.get("action_id") or "").strip(),
            effect="deny" if effect == "deny" else "allow",
            params=dict(data.get("params") or {}),
            require_confirmation=bool(data.get("require_confirmation")),
        )

    @property
    def specified(self) -> int:
        return sum((
            bool(self.action_id),
            self.effect == "deny",
            bool(self.params),
            self.require_confirmation,
        ))


@dataclass(frozen=True)
class Rule:
    """一条五维动作前置条件（``policy_rules`` 里的一行 payload）。"""

    key: str
    name: str = ""
    condition: Condition = field(default_factory=Condition)
    target: Target = field(default_factory=Target)
    quota: Quota = field(default_factory=Quota)
    action: RuleAction = field(default_factory=RuleAction)
    priority: int = 100
    enabled: bool = True
    #: ``policy_rules.scope_type``（复用既有作用域列，不新造维度）。
    scope_type: str = "global"
    scope_id: str = ""

    # ---- 判定 ---------------------------------------------------------
    @property
    def scope_specificity(self) -> int:
        return _SCOPE_SCORE.get(str(self.scope_type or "global"), 0)

    @property
    def specificity(self) -> int:
        """越具体越大；并列时 ``priority`` 大者先（见 :func:`decide`）。"""
        return (
            self.condition.specified
            + self.target.specified
            + self.quota.specified
            + self.action.specified
            + self.scope_specificity
        )

    def matches(self, ctx: "ActionContext", *, action_id: str = "") -> bool:
        if not self.enabled:
            return False
        wanted = self.action.action_id
        if wanted and wanted != action_id:
            return False
        if not wanted and self.scope_id and self.scope_id != str(ctx.chat_id or ""):
            # 没有 action_id 时用 scope_id 当"哪个会话"的限定（既有列复用）。
            return False
        return self.condition.matches(ctx) and self.target.matches(ctx)

    def describe(self) -> str:
        bits = []
        if self.condition.time_window:
            bits.append(f"时间 {self.condition.time_window[0]}-{self.condition.time_window[1]}")
        if self.condition.topic_type:
            bits.append("话题 " + "/".join(self.condition.topic_type))
        if self.condition.mood:
            bits.append("态度 " + "/".join(self.condition.mood))
        if self.target.people:
            bits.append("对象 " + "/".join(self.target.people))
        if self.target.chats:
            bits.append("会话 " + "/".join(self.target.chats))
        if self.target.role:
            bits.append(f"角色 {self.target.role}")
        if self.quota.per_hour:
            bits.append(f"每小时 ≤{self.quota.per_hour}")
        if self.quota.per_day:
            bits.append(f"每天 ≤{self.quota.per_day}")
        if self.quota.cooldown_seconds:
            bits.append(f"冷却 {self.quota.cooldown_seconds}s")
        detail = "、".join(bits) if bits else "无条件"
        return f"{self.name or self.key}（{detail}，作用域 {self.scope_type}）"

    # ---- 数据行 → 规则 ------------------------------------------------
    @classmethod
    def from_payload(cls, payload: dict | None, *, fallback_key: str = "") -> "Rule":
        data = dict(payload or {})
        action = RuleAction.from_dict(data.get("action"))
        top_effect = str(data.get("effect") or "").strip().lower()
        if top_effect == "deny" and action.effect != "deny":
            action = RuleAction(
                action_id=action.action_id,
                effect="deny",
                params=action.params,
                require_confirmation=action.require_confirmation,
            )
        return cls(
            key=str(data.get("key") or fallback_key or "").strip(),
            name=str(data.get("name") or data.get("key") or fallback_key or "").strip(),
            condition=Condition.from_dict(data.get("condition")),
            target=Target.from_dict(data.get("target")),
            quota=Quota.from_dict(data.get("quota")),
            action=action,
            priority=_as_int(data.get("priority"), 100),
            enabled=bool(data.get("enabled", True)),
            scope_type=str(data.get("scope_type") or "global").strip() or "global",
            scope_id=str(data.get("scope_id") or "").strip(),
        )


# ---------------------------------------------------------------------------
# 判定输入 / 输出契约
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ActionContext:
    """一次动作的**事实**（调用方填写，禁止把模型自报的名字当身份）。"""

    action_id: str = ""
    actor_id: str = ""
    chat_id: str = ""
    chat_type: str = ""          # private | group
    platform: str = ""
    account_id: str = ""
    is_owner: bool = False
    is_group: bool = False
    topic_type: str = ""         # intent 取值（R-A*）
    mood: str = ""               # attitude 取值（R-A3）
    params: dict[str, Any] = field(default_factory=dict)
    now: datetime = field(default_factory=datetime.now)
    #: 计数源不在台账里的收编点自带的事实（见 :class:`QuotaCounts`）。
    quota_counts: "QuotaCounts | None" = None

    def quota_key(self) -> str:
        """计数口径：动作 × 谁 × 哪个会话。换会话/换人各自计数。"""
        return "\x1f".join((
            str(self.action_id or ""),
            str(self.platform or ""),
            str(self.account_id or ""),
            str(self.chat_id or ""),
            str(self.actor_id or ""),
        ))


@dataclass(frozen=True)
class QuotaCounts:
    """调用点自带的计数事实（**判据仍在引擎**，这里只给事实）。

    给"计数源不在 ``rule_use_ledger`` 台账里"的收编点用：例如
    ``agents/tools/reminder.py`` 的非主人待提醒条数来自 ``reminders`` 表、
    ``core/terminal/proactive_service.py`` 的小时/每日次数来自
    ``ProactiveSendLog``。两者都是既有的、重启不重置的事实源，**不搬家**
    （搬了就是第二本账）；引擎照 ``quota`` 维判据算"超没超"。
    """

    counts: dict[str, int] = field(default_factory=dict)   # hour / day
    last_use: datetime | None = None


@dataclass(frozen=True)
class RuleDecision:
    """N4 输出契约（docs/decisions/架构评估.md :260，字段名逐字照抄）。"""

    allowed: bool
    rule_id: str = ""
    specificity: int = 0
    decided_at: str = ""
    explanation: str = ""
    shadow: bool = False
    #: 本次判定**实际生效**的档位（``off`` 时调用方不该问引擎，见模块 docstring）。
    mode: str = features.MODE_OFF
    #: 命中的**全部**规则 key（新→旧按具体度排序），供审计/看板用。
    matched_keys: tuple[str, ...] = ()
    #: 命中规则要求的确认（由 ``ToolPolicy.require_confirmation`` 收编而来）。
    require_confirmation: bool = False
    #: 超出配额时是哪个窗口（``hour`` / ``day`` / ``cooldown``），空=没超。
    exceeded: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "rule_id": self.rule_id,
            "specificity": int(self.specificity),
            "decided_at": self.decided_at,
            "explanation": self.explanation,
            "shadow": bool(self.shadow),
            "mode": self.mode,
            "matched_keys": list(self.matched_keys),
            "require_confirmation": bool(self.require_confirmation),
            "exceeded": self.exceeded,
        }


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
class RuleEngine:
    """**动作前置条件的唯一判定点**。

    数据来源是 ``SpeechStore``（同一张 ``policy_rules`` 表、同一条
    ``get_revision()`` 缓存通道），所以改一行数据在下一个判定就生效，
    **不用重启**。``store=None`` 时一律"不判定"（与 ``output_gate`` 读不到
    数据行同口径：绝不挡发送）。
    """

    def __init__(self, store: Any = None):
        self.store = store
        #: 没有台账表（老库没跑迁移 55）时的**同进程**回退计数：进程重启即清零。
        self._memory_uses: dict[str, list[datetime]] = {}

    # ---- 开关（唯一读点）----------------------------------------------
    def mode(self) -> str:
        """三态读法（**不要**用 :func:`core.features.enabled`：它把 shadow 读成 False）。

        ``YINYUE_FEATURE_RULES`` 默认 **off**（现状逐字节一致，这是回滚位）。
        """
        return features.mode(features.RULES, features.MODE_OFF)

    # ---- 数据面（复用 policy_rules 通道）-------------------------------
    def payloads(self) -> list[dict]:
        if self.store is None:
            return []
        try:
            return list(self.store.output_gate_payloads(RULES_RULE_KEY))
        except Exception as exc:  # noqa: BLE001 - 配置读不到就"不判定"，绝不挡发送
            log.warning("action_rules_payloads_failed", error=str(exc)[:160])
            return []

    def rules(self) -> list[Rule]:
        """展开数据行里的五维规则（一行可带多条，key 前缀命中即收）。"""
        found: list[Rule] = []
        for payload in self.payloads():
            rows = payload.get("rules")
            if isinstance(rows, list):
                for index, item in enumerate(rows):
                    if not isinstance(item, dict):
                        continue
                    rule = Rule.from_payload(item, fallback_key=f"{RULES_RULE_KEY}#{index}")
                    if rule.key:
                        found.append(rule)
                continue
            # 单条写法：整行 payload 就是一条规则。
            if any(k in payload for k in ("condition", "target", "quota", "action", "effect")):
                rule = Rule.from_payload(payload, fallback_key=RULES_RULE_KEY)
                if rule.key:
                    found.append(rule)
        return found

    # ---- 计数（quota 的唯一记账处）------------------------------------
    def use_count(self, quota_key: str, *, since: datetime) -> int:
        count = self._ledger_count(quota_key, since)
        if count is not None:
            return count
        return self._fallback_count(quota_key, since)

    def _ledger_count(self, quota_key: str, since: datetime) -> int | None:
        engine = self._engine()
        if engine is None or not _table_exists(engine, "rule_use_ledger"):
            return None
        try:
            from sqlalchemy import func, select
            from sqlalchemy.orm import sessionmaker

            with sessionmaker(bind=engine, expire_on_commit=False)() as session:
                return int(session.execute(
                    select(func.count()).select_from(mm.RuleUseLedger).where(
                        mm.RuleUseLedger.quota_key == quota_key,
                        mm.RuleUseLedger.used_at >= since,
                    )
                ).scalar() or 0)
        except Exception as exc:  # noqa: BLE001 - 计数读不到就按 0（不挡）
            log.warning("rule_use_count_failed", error=str(exc)[:160])
            return None

    def _fallback_count(self, quota_key: str, since: datetime) -> int:
        """没有台账表时的**同进程**计数（老库没跑迁移 55 时的可用路径）。

        进程重启即清零——口径写在 :meth:`quota_blocked` 的说明里，不假装持久。
        """
        rows = self._memory_uses.setdefault(quota_key, [])
        return sum(1 for stamp in rows if stamp >= since)

    def record(self, decision: RuleDecision, ctx: "ActionContext") -> RuleDecision:
        """把**刚刚真的执行了**的一次动作用记账（``quota`` 的唯一写入口）。

        与 :func:`decide` 分开是有意的：``decide`` 是纯判定（可读、可重放、
        无副作用），记账只在调用方拿到"动作真的发生了"之后调一次。影子模式下
        也记（读数要的就是"本来会超几次"），但要由调用方决定——见各调用点。
        """
        if self.mode() == features.MODE_OFF or not decision.rule_id:
            return decision
        key = ctx.quota_key()
        used_at = ctx.now
        engine = self._engine()
        if engine is not None and _table_exists(engine, "rule_use_ledger"):
            try:
                from sqlalchemy.orm import sessionmaker

                with sessionmaker(bind=engine, expire_on_commit=False)() as session:
                    session.add(mm.RuleUseLedger(
                        rule_key=decision.rule_id,
                        quota_key=key,
                        platform=str(ctx.platform or ""),
                        account_id=str(ctx.account_id or ""),
                        chat_id=str(ctx.chat_id or ""),
                        actor_id=str(ctx.actor_id or ""),
                        action_id=str(ctx.action_id or ""),
                        used_at=used_at,
                    ))
                    session.commit()
                return decision
            except Exception as exc:  # noqa: BLE001 - 记账失败不挡动作
                log.warning("rule_use_record_failed", error=str(exc)[:160])
        self._memory_uses.setdefault(key, []).append(used_at)
        return decision

    def reset_counts(self) -> None:
        """清空**同进程**回退计数（用例隔离用；台账行不在清理范围）。"""
        self._memory_uses.clear()

    # ---- 判定 ---------------------------------------------------------
    def decide(self, ctx: "ActionContext") -> RuleDecision:
        """五维判定：返 :class:`RuleDecision`（``explanation`` 永远非空）。"""
        return self._decide(ctx, extra_rows=())

    def decide_with_rows(
        self, ctx: "ActionContext", extra_rows: Sequence[Rule],
    ) -> RuleDecision:
        """判定，但用**调用点给的一组规则**取代数据面读到的那些。

        收编点专用（``core/terminal/proactive_service.py`` 把
        ``proactive_targets`` 那一行折成规则用它）：既有常数与阈值原样保留，
        只是"判据住在哪"从各处 if 链搬到这一处。数据面上有同名动作的数据行时，
        仍然**数据行优先**（见 ``proactive_service._engine_reject``）。
        """
        return self._decide(ctx, extra_rows=tuple(extra_rows or ()))

    def _decide(self, ctx: "ActionContext",
                *, extra_rows: Sequence[Rule]) -> RuleDecision:
        mode = self.mode()
        now_iso = ctx.now.isoformat(timespec="seconds")
        base = dict(decided_at=now_iso, mode=mode, shadow=mode == features.MODE_SHADOW)

        if mode == features.MODE_OFF:
            return RuleDecision(
                allowed=True,
                explanation="规则引擎 off（YINYUE_FEATURE_RULES=off）：不判定",
                **base,
            )

        action_id = str(ctx.action_id or "")

        # ① 硬禁区：写死代码，数据行配不动（docs/decisions/架构评估.md :263）。
        if action_id in HARD_WALL_ACTIONS:
            return RuleDecision(
                allowed=False,
                rule_id=f"hard_wall:{action_id}",
                specificity=10_000,
                explanation=f"拒绝执行 {action_id}：{_HARD_WALL_EXPLANATION}，任何规则都不放宽",
                **base,
            )

        pool = list(extra_rows) if extra_rows else self.rules()
        rows = [r for r in pool if r.matches(ctx, action_id=action_id)]
        # 越具体越优先；并列时 priority 大者先，再并列按 key 定序（判定确定）。
        rows.sort(key=lambda r: (-r.specificity, -int(r.priority or 0), r.key))

        # ② 配额：只要有命中规则的次数已用尽 → 拒绝（含 shadow：只记不拦）。
        blocked = self.quota_blocked(ctx, rows, precomputed=ctx.quota_counts)
        if blocked is not None:
            rule, exceeded, detail = blocked
            return self._verdict(
                allowed=False, rule=rule, rows=rows,
                explanation=(
                    f"拒绝执行 {action_id}：命中规则 {rule.name or rule.key} 的"
                    f"次数配额已用尽（{detail}）"
                ),
                exceeded=exceeded, **base,
            )

        if not rows:
            return RuleDecision(
                allowed=True,
                explanation=f"允许执行 {action_id or '该动作'}：没有命中任何动作前置条件",
                **base,
            )

        top = rows[0]
        if top.action.effect == "deny":
            return self._verdict(
                allowed=False, rule=top, rows=rows,
                explanation=(
                    f"拒绝执行 {action_id}：命中规则 {top.describe()}"
                    f"（具体度 {top.specificity}）"
                ),
                **base,
            )
        return self._verdict(
            allowed=True, rule=top, rows=rows,
            explanation=(
                f"允许执行 {action_id}：命中规则 {top.describe()}"
                f"（具体度 {top.specificity}）"
            ),
            **base,
        )

    def quota_blocked(
        self, ctx: "ActionContext", rows: Sequence[Rule] | None = None,
        *, precomputed: "QuotaCounts | None" = None,
    ) -> tuple[Rule, str, str] | None:
        """命中规则里第一个用尽配额的 → ``(rule, exceeded, 人读细节)``；没超 → ``None``。

        ``exceeded`` 取 ``hour`` / ``day`` / ``cooldown``。**读不到计数按 0**
        （绝不因为计数故障就拒绝动作，同 ``output_gate`` 口径）。

        ``precomputed`` 给"计数源不在台账里"的收编点用（如
        ``agents/tools/reminder.py`` 的非主人待提醒条数）：**判据仍是这里这一份**，
        调用点只提供事实。
        """
        candidates = list(rows) if rows is not None else [
            r for r in self.rules() if r.matches(ctx, action_id=str(ctx.action_id or ""))
        ]
        key = ctx.quota_key()
        for rule in candidates:
            quota = rule.quota
            if not quota.any_limit:
                continue
            if quota.cooldown_seconds:
                last = (precomputed.last_use if precomputed is not None
                        else self.last_use(key))
                if last is not None and ctx.now - last < timedelta(
                    seconds=quota.cooldown_seconds
                ):
                    waited = int((ctx.now - last).total_seconds())
                    return (
                        rule, "cooldown",
                        f"冷却 {quota.cooldown_seconds}s，距上次 {waited}s",
                    )
            if quota.per_hour:
                used = self.count_for("hour", key, ctx, precomputed)
                gate = quota_gate.observe(key, "hour", used, quota.per_hour)
                if gate == "blocked":
                    return (rule, "hour", f"每小时上限 {quota.per_hour}，已用 {used}")
            if quota.per_day:
                used = self.count_for("day", key, ctx, precomputed)
                gate = quota_gate.observe(key, "day", used, quota.per_day)
                if gate == "blocked":
                    return (rule, "day", f"每天上限 {quota.per_day}，已用 {used}")
        return None

    def count_for(self, window: str, quota_key: str, ctx: "ActionContext",
                  precomputed: "QuotaCounts | None" = None) -> int:
        """一个窗口里的已用次数（``precomputed`` 优先于台账）。"""
        if precomputed is not None:
            return max(0, int(precomputed.counts.get(window, 0)))
        delta = timedelta(hours=1) if window == "hour" else timedelta(days=1)
        return self.use_count(quota_key, since=ctx.now - delta)

    def last_use(self, quota_key: str) -> datetime | None:
        engine = self._engine()
        if engine is not None and _table_exists(engine, "rule_use_ledger"):
            try:
                from sqlalchemy import select
                from sqlalchemy.orm import sessionmaker

                with sessionmaker(bind=engine, expire_on_commit=False)() as session:
                    return session.execute(
                        select(mm.RuleUseLedger.used_at)
                        .where(mm.RuleUseLedger.quota_key == quota_key)
                        .order_by(mm.RuleUseLedger.used_at.desc())
                        .limit(1)
                    ).scalar_one_or_none()
            except Exception as exc:  # noqa: BLE001
                log.warning("rule_last_use_failed", error=str(exc)[:160])
        rows = self._memory_uses.get(quota_key) or []
        return max(rows) if rows else None

    # ---- 内部 ---------------------------------------------------------
    def _verdict(self, *, allowed: bool, rule: Rule, rows: Sequence[Rule],
                 explanation: str, exceeded: str = "",
                 decided_at: str, shadow: bool, mode: str) -> RuleDecision:
        return RuleDecision(
            # 影子模式**永不拦**：allowed 只在 on 下才可能是 False（模块 docstring）。
            allowed=True if shadow else bool(allowed),
            rule_id=rule.key,
            specificity=int(rule.specificity),
            decided_at=decided_at,
            explanation=(
                f"[shadow 只记录] {explanation}（本来会拒绝）"
                if (shadow and not allowed) else explanation
            ),
            shadow=bool(shadow),
            mode=mode,
            matched_keys=tuple(r.key for r in rows),
            require_confirmation=bool(rule.action.require_confirmation),
            exceeded=exceeded,
        )

    def _engine(self) -> Any:
        return getattr(self.store, "engine", None)


def _table_exists(engine: Any, name: str) -> bool:
    try:
        from sqlalchemy import inspect

        return name in set(inspect(engine).get_table_names())
    except Exception:  # noqa: BLE001
        warn("core/rules/engine.py:729 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


# ---------------------------------------------------------------------------
# 模块级入口（调用点的**唯一**门）
# ---------------------------------------------------------------------------
# 缓存键用**引擎句柄的对象身份**（``id``）+ 句柄本身，不只靠 ``id``：进程里
# 反复建/丢隔离库时 ``id`` 会被回收重用，只按 ``id`` 缓存会把旧引擎（连带旧
# store）交还给新用例。``_ENGINE_OWNERS`` 存句柄本身，重用时一并换新。
_ENGINES: dict[int, RuleEngine] = {}
_ENGINE_OWNERS: dict[int, Any] = {}


def get_engine(store: Any = None) -> RuleEngine:
    """取引擎实例（按 ``store.engine`` 句柄复用，避免逐次重建）。"""
    if store is None:
        try:
            from core.speech.store import get_speech_store

            store = get_speech_store()
        except Exception:  # noqa: BLE001 - 没有 store 就是"不判定"
            warn("core/rules/engine.py:750 Exception", err=repr(__import__("sys").exc_info()[1]))
            store = None
    handle = getattr(store, "engine", None)
    key = id(handle) if handle is not None else 0
    engine = _ENGINES.get(key)
    if (engine is None or engine.store is not store
            or _ENGINE_OWNERS.get(key) is not handle):
        engine = RuleEngine(store)
        _ENGINES[key] = engine
        _ENGINE_OWNERS[key] = handle
    return engine


def rules_mode(store: Any = None) -> str:
    """三态档位（唯一读点转发，调用方不用自己读环境）。"""
    return get_engine(store).mode()


# ---------------------------------------------------------------------------
# 身份/授权维：**动作前置条件里的“谁有资格”只有这一处**
# ---------------------------------------------------------------------------
# 为什么它不读 ``RULES`` 开关（**有意为之，不是漏接**）：身份判定是本仓早已
# 确立的**恒真能力**（``core/terminal/authority.py``：Authority is always active
# and fails closed），不是灰度对象——``RULES=off`` 时把它一起关掉就等于把每个
# 调用点推回"没有身份"的失败位，行为会变。所以 ``is_owner`` / ``authority``
# 在三个档位下**输出逐字节相同**，`RULES` 只管"要不要拿它去拦动作"。
#
# 它的价值是**收编**：``agents/tools/{search,memory,person_profile}.py`` 原先各自
# ``from core.terminal.authority import active_context_is_owner`` 再各写一遍
# ``bool(...)`` 的兜底（G1-13 的"5 处工具内部各写一份 owner 判定"），现在只留
# 一处口径。
def is_owner(context: Any = None, **kwargs: Any) -> bool:
    """当前发言人是不是主人（确定性身份服务的**唯一**转发点）。

    Args:
        context: ``AgentContext`` / ``AgentContext.extra`` 同族的上下文对象。
        **kwargs: 透传给 ``active_context_is_owner``（``platform`` /
            ``account_id`` / ``sender_id`` 等；``agents/tools/search.py`` 用它
            按租户解析）。

    Returns:
        ``bool``；解析不出来一律 ``False``（fail closed，绝不回落配置里
        写死的主人号）。
    """
    try:
        from core.terminal.authority import active_context_is_owner

        return bool(active_context_is_owner(context, **kwargs))
    except Exception:  # noqa: BLE001 - 身份不可得 = 不是主人
        warn("core/rules/engine.py:798 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def has_capability(context: Any, capability: Any) -> bool:
    """当前发言人有没有某个能力（``Capability``；同样 fail closed）。"""
    try:
        from core.terminal.authority import active_context_has_capability

        return bool(active_context_has_capability(context, capability))
    except Exception:  # noqa: BLE001
        warn("core/rules/engine.py:808 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def authority(**kwargs: Any) -> Any:
    """当前 principal（``core.terminal.authority.context_principal`` 的转发）。

    只给**需要 principal 本身**的调用点用（读 ``person_id`` / ``chat_type``）；
    只要"是不是主人"就调 :func:`is_owner`，别自己再判一遍。
    """
    context = kwargs.pop("context", None)
    try:
        from core.terminal.authority import context_principal

        return context_principal(context)
    except Exception:  # noqa: BLE001
        warn("core/rules/engine.py:823 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def decide(ctx: ActionContext, *, store: Any = None) -> RuleDecision:
    """**动作前置条件**的唯一判定入口。"""
    return get_engine(store).decide(ctx)


def decide_with_rows(
    ctx: ActionContext, extra_rows: Sequence[Rule], *, store: Any = None,
) -> RuleDecision:
    """同 :func:`decide`，但用调用点给的规则组（见 :meth:`RuleEngine.decide_with_rows`）。"""
    return get_engine(store).decide_with_rows(ctx, extra_rows)


def record(decision: RuleDecision, ctx: ActionContext, *, store: Any = None) -> RuleDecision:
    """记账入口（动作真的发生之后调一次；见 :meth:`RuleEngine.record`）。"""
    return get_engine(store).record(decision, ctx)


def quota_blocked(
    ctx: ActionContext, rows: Sequence[Rule] | None = None, *,
    precomputed: QuotaCounts | None = None, store: Any = None,
) -> tuple[Rule, str, str] | None:
    return get_engine(store).quota_blocked(ctx, rows, precomputed=precomputed)


def use_count(quota_key: str, *, since: datetime, store: Any = None) -> int:
    return get_engine(store).use_count(quota_key, since=since)


def rules(store: Any = None) -> list[Rule]:
    """展开当前数据面上的五维规则（Web 列表页与判定共用同一份读法）。"""
    return get_engine(store).rules()


# ---------------------------------------------------------------------------
# 写入口 + **唯一的 bump revision 入口**
# ---------------------------------------------------------------------------
def bump_revision(
    *, changed_by: str = "system", detail: dict[str, Any] | None = None,
    store: Any = None,
) -> str:
    """**唯一**的规则修订推进入口（Web 与脚本都走它）。

    ``policy_rules`` 的读缓存按 ``speech_config_revisions`` 的最新 revision 失效
    （``core/speech/store.py::_ensure_cache``），所以写完规则**必须**在这里
    bump 一次：漏了就得等 1 秒轮询甚至重启，那正是乱点 #28 说的
    「谁 bump revision 无统一入口」。
    """
    target = store
    if target is None:
        try:
            from core.speech.store import get_speech_store

            target = get_speech_store()
        except Exception:  # noqa: BLE001
            warn("core/rules/engine.py:880 Exception", err=repr(__import__("sys").exc_info()[1]))
            target = None
    if target is None:
        return ""
    revision = target.bump_revision(
        change_type="action_rules", changed_by=str(changed_by or "system"),
        detail=dict(detail or {}),
    )
    try:
        from core.speech.facade import get_speech_facade

        get_speech_facade(target).invalidate()
    except Exception:  # noqa: BLE001 - 缓存失效失败不影响数据已落库
        warn("core/rules/engine.py:892 Exception", err=repr(__import__("sys").exc_info()[1]))
    return revision


def save_rule(
    rule: dict[str, Any], *, changed_by: str = "system", store: Any = None,
) -> dict[str, Any]:
    """写一行五维规则（``policy_rules.payload_json["rules"]`` 的 upsert）。

    幂等：按 ``rule["key"]`` 找同一条——**同一 key 再写一次就是改数据**，
    不是插重复行（与 ``output_gate_payloads`` 的读法同一形状）。
    """
    key = str((rule or {}).get("key") or "").strip()
    if not key:
        raise ValueError("rule key is required")
    target = store
    if target is None:
        from core.speech.store import get_speech_store

        target = get_speech_store()
    row = target.get_rule(RULES_RULE_KEY)
    payload = _loads(getattr(row, "payload_json", None), {})
    rows = payload.get("rules")
    if not isinstance(rows, list):
        rows = []
    entry = dict(rule)
    entry["key"] = key
    replaced = False
    for index, item in enumerate(rows):
        if isinstance(item, dict) and str(item.get("key") or "").strip() == key:
            rows[index] = entry
            replaced = True
            break
    if not replaced:
        rows.append(entry)
    fields: dict[str, Any] = {
        "name": str(payload.get("name") or "动作前置条件"),
        "category": "action_rule",
        "enforcement": "deterministic",
        "match_type": "always",
        "payload_json": json.dumps(
            {**payload, "rules": rows}, ensure_ascii=False,
        ),
    }
    if row is None:
        fields["priority"] = 100
        fields["enabled"] = 1
    target.upsert_rule(RULES_RULE_KEY, **fields)
    bump_revision(changed_by=changed_by, detail={"rule_key": key}, store=target)
    return entry


def delete_rule(
    rule_key: str, *, changed_by: str = "system", store: Any = None,
) -> bool:
    """删一条五维规则（只改 ``payload_json["rules"]``，整行数据不动）。"""
    wanted = str(rule_key or "").strip()
    if not wanted:
        return False
    target = store
    if target is None:
        from core.speech.store import get_speech_store

        target = get_speech_store()
    row = target.get_rule(RULES_RULE_KEY)
    if row is None:
        return False
    payload = _loads(getattr(row, "payload_json", None), {})
    rows = payload.get("rules")
    if not isinstance(rows, list):
        return False
    kept = [
        item for item in rows
        if not (isinstance(item, dict) and str(item.get("key") or "").strip() == wanted)
    ]
    if len(kept) == len(rows):
        return False
    target.upsert_rule(
        RULES_RULE_KEY,
        payload_json=json.dumps({**payload, "rules": kept}, ensure_ascii=False),
    )
    bump_revision(changed_by=changed_by, detail={"rule_key": wanted, "deleted": True},
                  store=target)
    return True


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
_SCOPE_SCORE = {
    "global": 0,
    "platform": 1,
    "account": 2,
    "chat_type": 3,
    "scene": 3,
    "contact": 4,
    "chat": 4,
    "group": 5,
}


def _norm_hhmm(value: Any) -> str:
    """``"7:5"`` / ``"07:05"`` / ``"0705"`` → ``"07:05"``；非法 → ``""``。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if ":" not in text and len(text) == 4 and text.isdigit():
        text = f"{text[:2]}:{text[2:]}"
    if ":" not in text:
        return ""
    head, _, tail = text.partition(":")
    try:
        hour = int(head)
        minute = int(tail)
    except (TypeError, ValueError):
        warn("core/rules/engine.py:1007 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return ""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [part for part in value.replace("，", ",").split(",")]
    if not isinstance(value, (list, tuple, set, frozenset)):
        value = [value]
    return tuple(str(item).strip() for item in value if str(item).strip())


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        warn("core/rules/engine.py:1027 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return default


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:  # noqa: BLE001
        warn("core/rules/engine.py:1038 Exception", err=repr(__import__("sys").exc_info()[1]))
        return default


def _role_of(ctx: ActionContext) -> str:
    """``target.role`` 的唯一裁法：群里说话人按 ``member`` 判，主人按 ``owner``。"""
    if ctx.is_group:
        return ROLE_OWNER if ctx.is_owner else ROLE_GROUP
    return ROLE_OWNER if ctx.is_owner else ROLE_MEMBER


def describe_rules(store: Any = None) -> list[dict[str, Any]]:
    """Web 列表页用的规则视图（与判定读的是同一份 ``rules()``）。

    ``store`` 显式传（调用点把它手上的那一个读通道句柄递进来）：读列表与做判定
    必须是**同一个** store，否则"同进程改一行数据即生效"就成了两本缓存的对账。
    """
    return [
        {
            "key": rule.key,
            "name": rule.name,
            "scope_type": rule.scope_type,
            "scope_id": rule.scope_id,
            "priority": int(rule.priority),
            "enabled": bool(rule.enabled),
            "specificity": int(rule.specificity),
            "condition": {
                "time_window": list(rule.condition.time_window or []),
                "topic_type": list(rule.condition.topic_type),
                "mood": list(rule.condition.mood),
            },
            "target": {
                "people": list(rule.target.people),
                "chats": list(rule.target.chats),
                "role": rule.target.role,
            },
            "quota": {
                "max_per_hour": rule.quota.per_hour,
                "max_per_day": rule.quota.per_day,
                "cooldown_seconds": rule.quota.cooldown_seconds,
            },
            "action": {
                "action_id": rule.action.action_id,
                "effect": rule.action.effect,
                "params": dict(rule.action.params),
                "require_confirmation": rule.action.require_confirmation,
            },
            "describe": rule.describe(),
        }
        for rule in rules(store)
    ]


def action_ids(store: Any = None) -> list[str]:
    """当前数据面出现过的动作 id（Web 表单的动作下拉）。"""
    seen: list[str] = []
    for rule in rules(store):
        if rule.action.action_id and rule.action.action_id not in seen:
            seen.append(rule.action.action_id)
    return sorted(seen)


def guard_decision(decision: RuleDecision, *, reason: str = "") -> tuple[bool, str]:
    """给 ``core/speech/guards.py`` 用的形状适配：``(拦不拦, 理由)``。

    影子模式下 ``decision.allowed`` 已经是 ``True``（引擎自己保证），这里不再
    重复判 ``shadow``——判定口径只有一处，别在调用点写第二份。调用点是
    ``core/speech/guards.py::_rule_engine_gate``（出站侧那两个收编点）。
    """
    if decision.allowed:
        return False, ""
    return True, str(reason or decision.explanation or "动作前置条件未满足")
