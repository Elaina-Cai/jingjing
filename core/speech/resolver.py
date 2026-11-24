from __future__ import annotations

import json
import structlog
from dataclasses import dataclass
from typing import Any

from core.speech.domain import (
    AccountPolicy,
    AudienceKind,
    EngagementDecision,
    Enforcement,
    HardConstraint,
    OutputPolicy,
    PromptSection,
    ReplyMode,
    ResolvedPersona,
    RuleCategory,
    RuleEffect,
    ScopeType,
    SpeakingPolicy,
    TargetPolicy,
    ToolPolicy,
    TurnContext,
)
from core.speech.store import SpeechStore, _loads
from safelog import warn

log = structlog.get_logger()


def _reason_parts(reason: Any) -> tuple[str, ...]:
    """拆 ``trigger_reason`` 的复合形态（R-A9）。

    ``"patted_in_group+mentioned_in_group"`` → 两段。单一 reason 时就是它自己。
    空/None 返回空元组（没有 reason 段，不猜）。
    """
    return tuple(
        part.strip() for part in str(reason or "").split("+") if part.strip()
    )


def _scope_score(scope_type: str) -> int:
    return {
        ScopeType.GLOBAL.value: 0,
        ScopeType.PLATFORM.value: 10,
        ScopeType.ACCOUNT.value: 20,
        ScopeType.CHAT_TYPE.value: 30,
        ScopeType.CONTACT.value: 40,
        ScopeType.GROUP.value: 40,
        ScopeType.CHAT.value: 50,
    }.get(scope_type, 0)


def _condition_value_match(actual, expected) -> bool:
    if isinstance(expected, dict):
        if "gt" in expected and not (actual is not None and actual > expected["gt"]):
            return False
        if "gte" in expected and not (actual is not None and actual >= expected["gte"]):
            return False
        if "lt" in expected and not (actual is not None and actual < expected["lt"]):
            return False
        if "lte" in expected and not (actual is not None and actual <= expected["lte"]):
            return False
        if "in" in expected and actual not in expected["in"]:
            return False
        if "not_in" in expected and actual in expected["not_in"]:
            return False
        if "contains" in expected:
            try:
                if str(expected["contains"]) not in str(actual or ""):
                    return False
            except Exception:
                warn("core/speech/resolver.py:72 Exception", err=repr(__import__("sys").exc_info()[1]))
                return False
        if "regex" in expected:
            import re
            try:
                if not re.search(str(expected["regex"]), str(actual or "")):
                    return False
            except Exception:
                warn("core/speech/resolver.py:79 Exception", err=repr(__import__("sys").exc_info()[1]))
                return False
        if "exists" in expected:
            if bool(actual is not None) != bool(expected["exists"]):
                return False
        return True
    if isinstance(expected, list):
        return actual in expected
    return actual == expected


def match_conditions(conditions: dict | None, facts: dict) -> bool:
    if not conditions:
        return True
    if "all_of" in conditions:
        return all(match_conditions(c, facts) for c in conditions.get("all_of") or [])
    if "any_of" in conditions:
        return any(match_conditions(c, facts) for c in conditions.get("any_of") or [])
    if "not" in conditions:
        return not match_conditions(conditions.get("not") or {}, facts)
    for key, expected in conditions.items():
        if not _condition_value_match(facts.get(key), expected):
            return False
    return True


class AudienceResolver:
    """Derive the audience kind from canonical turn facts."""

    def resolve(self, turn: TurnContext) -> AudienceKind:
        if turn.target.platform == "web":
            return AudienceKind.WEB_CHAT
        if turn.trigger_reason == "proactive":
            return AudienceKind.PROACTIVE
        if turn.target.is_group and "patted_in_group" in _reason_parts(
            turn.trigger_reason
        ):
            # R-A9（2026-10-01）：拍是"对方呼唤鲸鲸的形式"之一，触发起点可能是**复合**
            # 的（``patted_in_group+mentioned_in_group``，拍与内容合并成一个回合）。
            # 只要这一段里有拍，受众就是 GROUP_PAT —— 这是 ``patted_in_group`` 的
            # 特例口径，不是"拍压过 @她"。
            return AudienceKind.GROUP_PAT
        if turn.target.is_group:
            if turn.trigger_reason == "patted_in_group":
                return AudienceKind.GROUP_PAT
            if turn.is_mentioned:
                return AudienceKind.GROUP_MENTIONED
            if turn.is_owner:
                return AudienceKind.GROUP_OWNER
            return AudienceKind.GROUP_AMBIENT
        if turn.is_owner:
            return AudienceKind.OWNER_PRIVATE
        return AudienceKind.NON_OWNER_PRIVATE


# ===========================================================================
# 受众/目标判定的 owner：这一轮她**允许** @ 谁（归属加固，R-A10）
# ===========================================================================
# 事故（2026-10-01 19:30，群；同类第三次复发）：
#
#     19:29:49 P6：@鲸鲸 推荐点可以带走的          ← 上一轮的请求者
#     19:30:05 P5：临武鸭东江鱼应该有是有，但是在永州，我没回那边  ← **本轮触发者**
#     实际发出：`@P6 临武鸭和东江鱼就别惦记了…`      ← @ 目标跟错了人
#
# `group_trigger_fired {trigger: ambient, sender: P5}` + `conversational_mode
# {sender: P5}` 都写着本轮触发者是P5，说出口却 @ 了更早的请求者 —— 因为
# "@ 谁"从来不是**判定**出来的，只是模型自己写在前面的一个字面量。
#
# 裁决（不是语义判断，是确定性事实）：
#
#     allowed = {本轮触发者} ∪ {被引用消息的作者} ∪ {本轮「呼唤形式」的发出者}
#
# 「呼唤形式」= @她 / 含她名字 / 拍她 / 引用她的消息（`docs/需求契约.md` §2.7 R-A10）。
# 文本里首个**招呼位**上的 @ 目标不在 allowed → 改成触发者；触发者也定不出来 → 去掉 @。
# 三个来源全部取**既有**上下文事实（turn / context.extra / canonical 解析器），
# 本模块不新造 @ 正则，也不新造人名→人 id 口径。
ADD_SOURCE_TRIGGER = "trigger"    # 本轮触发者（`turn.target.sender_name/sender_id`）
ADD_SOURCE_QUOTED = "quoted"      # 被引用消息的作者（本轮消息的 `[引用 X: …]`）
ADD_SOURCE_SUMMONS = "summons"    # 本轮「呼唤形式」的发出者（R-A10 四种形式）

ADD_ACTION_ALLOW = "allow"
ADD_ACTION_REPAIR = "repair"      # 换成本轮触发者
ADD_ACTION_DROP = "drop"          # 定不出触发者 → 去掉这个 @

#: 招呼位的分界字符：@ 出现在正文开头，或前面（去空白后）是它们时，这个 @ 是**在点名**。
#: 全是**结构化**的句读/行界，不含任何语义判断；句子里粘着的 ``@``（「让他@鲸鲸就行」）
#: 是**提及**，不是 @ 目标 —— 不认、也不改（裁决：只处理「@ 目标」）。
_ADDRESS_BOUNDARY_CHARS = frozenset("。！？!?…～~；;，,、：:\n\r")


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


@dataclass(frozen=True)
class AddresseePerson:
    """一个**允许被 @** 的人（``name`` = 正文里 @ 的那个显示名）。"""

    name: str = ""
    raw_id: str = ""      # 平台原始 id（有就带上：`@wxid_xxx` 直接命中）
    source: str = ""

    def label(self) -> str:
        return self.name or self.raw_id

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "raw_id": self.raw_id, "source": self.source}


@dataclass(frozen=True)
class AddresseeFacts:
    """本轮 @ 目标的**允许集合**（三个来源；``None`` = 这一来源本轮没有事实）。"""

    trigger: AddresseePerson | None = None      # 本轮触发者
    quoted: AddresseePerson | None = None       # 被引用消息的作者
    summons: AddresseePerson | None = None      # 本轮「呼唤形式」的发出者

    def allowed(self) -> tuple[AddresseePerson, ...]:
        out: list[AddresseePerson] = []
        for person in (self.trigger, self.quoted, self.summons):
            if person is None or not person.label():
                continue
            key = person.label().casefold()
            if any(item.label().casefold() == key for item in out):
                continue
            out.append(person)
        return tuple(out)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger.as_dict() if self.trigger else None,
            "quoted": self.quoted.as_dict() if self.quoted else None,
            "summons": self.summons.as_dict() if self.summons else None,
            "allowed": [p.label() for p in self.allowed()],
        }


@dataclass(frozen=True)
class AddresseeDecision:
    """一次 @ 目标裁定的结论（``text`` = 落地后的正文；``allow`` 时原样）。"""

    action: str = ADD_ACTION_ALLOW
    text: str = ""
    from_name: str = ""
    to_name: str = ""
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.action in (ADD_ACTION_REPAIR, ADD_ACTION_DROP)


def _owner_accounts(platform: str) -> tuple[str, ...]:
    """该平台的**主人账号**（``PLATFORMS_*_OWNER_ACCOUNTS``）；配置缺失回空元组。"""
    key = str(platform or "").strip()
    if not key:
        return ()
    try:
        from core.config.settings import config

        values = (config.get_all_owner_accounts() or {}).get(key, ()) or ()
        return tuple(str(v).strip() for v in values if str(v or "").strip())
    except Exception:  # noqa: BLE001 - 配置读不到就只按平台认人
        warn("core/speech/resolver.py:241 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ()


def _scope_accounts(platform: str, account_id: str) -> tuple[str, ...]:
    """名字收窄的账号序列：**主人账号**优先 → 本回合发送账号 → 全平台（空串）。

    为什么主人优先：person 名册按"消息在哪个账号的会话里被看到"落 ``account_id``
    （``PLATFORMS_*_OWNER_ACCOUNTS``），而**发送**用的是 bot 账号；两者在微信上不是同一个
    （主人 ``wxid_seed01`` / bot ``wxid_seed02``），QQ 上还有过同名重复
    实体挂在 bot 账号下的脏数据（``5ecaf80b…``）。先按主人账号认人、认不出再放宽，
    绝不反向（bot → 主人）。
    """
    ordered: list[str] = []

    def _add(value: str) -> None:
        value = str(value or "").strip()
        if value and value not in ordered:
            ordered.append(value)

    for value in _owner_accounts(platform):
        _add(value)
    _add(account_id)
    ordered.append("")  # 平台内唯一（不收窄）
    return tuple(ordered)


def _person_id_of(name: str, *, platform: str = "", account_id: str = "") -> str:
    """名字 → ``person_id``（canonical：``db.repo_people.PeopleRepository.find_persons``）。

    ``platform`` / ``account_id``（task-52）非空时把收窄交给仓库层
    （``find_persons(..., platform=, account_id=)``，本来就支持）；空串 = 不收窄，
    与收窄前逐字一致。

    **不新造一套**：认不出 / 同名多人 / 读不到一律返回 ``""``（不猜，与归属判定的
    "重名不消歧、不指派" 同一纪律）。任何失败都不挡发送。
    """
    value = str(name or "").strip()
    if not value:
        return ""
    scope_platform = str(platform or "")
    try:
        from core.terminal.store_port import get_meta_repo

        people = getattr(get_meta_repo(), "people", None)
        if people is None or not callable(getattr(people, "find_persons", None)):
            return ""
        if not scope_platform:
            # 平台未知 = 不收窄：与收窄前的调用**逐字一致**（off-diff 的判据）。
            rows = list(people.find_persons(value, platform="", limit=5) or ())
            found = {str(getattr(row, "person_id", "") or "").strip() for row in rows}
            found.discard("")
            return next(iter(found)) if len(found) == 1 else ""
        for scoped_account in _scope_accounts(scope_platform, account_id):
            rows = list(people.find_persons(
                value,
                platform=scope_platform,
                account_id=scoped_account,
                limit=5,
            ) or ())
            found = {str(getattr(row, "person_id", "") or "").strip() for row in rows}
            found.discard("")
            if len(found) == 1:
                return next(iter(found))
            if len(found) > 1:
                # 这一层已经有多人同名 ⇒ 真歧义，**不许再放宽**去找唯一。
                return ""
        return ""
    except Exception:  # noqa: BLE001 - 人 id 认不出就只按名字比
        warn("core/speech/resolver.py:309 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _turn_extra(context: Any) -> dict:
    extra = getattr(context, "extra", None)
    return extra if isinstance(extra, dict) else {}


def _inbound_text(turn: Any, extra: dict) -> str:
    """本轮**触发消息**的正文（turn 优先，回落 ``extra["user_message"]``）。"""
    text = str(getattr(turn, "inbound_text", "") or "")
    return text or str(extra.get("user_message") or "")


def _is_address_position(body: str, index: int) -> bool:
    """``body[index]`` 处的 ``@`` 是不是**招呼位**（正文开头 / 行首 / 句读之后）。"""
    before = body[:index].rstrip()
    return not before or before[-1] in _ADDRESS_BOUNDARY_CHARS


def _address_span(text: str) -> tuple[str, int, int] | None:
    """**新写正文**里处于招呼位的首个 ``@name`` → ``(名字, 起点, 终点)``。

    * 只看新写的正文：``[引用 …]`` 块是上下文，不是她的 @ 目标
      （canonical ``strip_quote_block``，与归属判定同一口径）；
    * 名字切词走 canonical ``mentioned_names``（本模块**不写第二份 @ 正则**）；
    * 招呼位 = 正文开头 / 行首 / 句读之后（见 ``_ADDRESS_BOUNDARY_CHARS``）。
    """
    from core.conversation.addressing import mentioned_names, strip_quote_block

    raw = str(text or "")
    if not raw.strip():
        return None
    body = strip_quote_block(raw)
    if not body.strip():
        return None
    names = mentioned_names(body)
    if not names:
        return None
    name = names[0]
    index = body.find("@" + name)
    if index < 0 or not _is_address_position(body, index):
        return None
    offset = 0 if body == raw else raw.rfind(body)
    if offset < 0:
        return None
    return name, offset + index, offset + index + len(name) + 1


def _is_broadcast_mention(name: str) -> bool:
    """``@所有人`` / ``@全体成员`` 这类广播收件人 —— 它**不是"某人"**。

    裁决管的是"@ 一个**人**"；广播不指向任何个人，把它改成触发者等于把一句广播
    变成一句私下交代（那是顺手改内容，已定决策禁止）。词表是 canonical 的
    （``addressing._BROADCAST_MENTIONS``），这里只消费。
    """
    value = str(name or "").strip().casefold()
    if not value:
        return False
    try:
        from core.conversation.addressing import _BROADCAST_MENTIONS

        return value in {str(item).casefold() for item in _BROADCAST_MENTIONS}
    except Exception:  # noqa: BLE001 - 词表读不到就按最小口径认
        warn("core/speech/resolver.py:373 Exception", err=repr(__import__("sys").exc_info()[1]))
        return value in {"所有人", "全体成员", "all", "everyone"}


def _drop_address(text: str, start: int, end: int) -> str:
    """去掉 ``@name`` 这个点名（连名字一起去 —— 只删 ``@`` 会留下一个直呼其名）。"""
    after = end
    while after < len(text) and text[after] in " \u3000\t":
        after += 1
    return text[:start] + text[after:]


class AddresseeResolver:
    """出站文本「@ 的是谁」的裁定（受众/目标判定的 owner）。

    ``facts()`` 给出本轮允许集合，``decide()`` 给出结论（**纯判定**：改不改、
    改成什么、为什么）——落地与审计在发送前的那个确定性裁定点
    （2026-10-23 N1 扫尾后已无此环节：出口不再按归属结论渲染 @）。

    ``person_id_of`` 可注入（测试/回放不用碰库）；默认走 canonical
    :func:`_person_id_of`。
    """

    def __init__(self, person_id_of: Any = None):
        self._person_id_of = person_id_of or _person_id_of

    # ------------------------------------------------------------------ 事实
    def person_id_of(self, name: str, *, platform: str = "", account_id: str = "") -> str:
        try:
            try:
                value = self._person_id_of(
                    str(name or ""),
                    platform=str(platform or ""),
                    account_id=str(account_id or ""),
                )
            except TypeError:
                # 注入的旧签名（只接 platform）：退回两参调用，行为不变。
                warn("core/speech/resolver.py:408 TypeError", err=repr(__import__("sys").exc_info()[1]))
                value = self._person_id_of(str(name or ""), platform=str(platform or ""))
        except Exception:  # noqa: BLE001 - 认人失败只按名字比，绝不挡发送
            warn("core/speech/resolver.py:411 Exception", err=repr(__import__("sys").exc_info()[1]))
            return ""
        return str(value or "")

    def facts(self, turn: Any, extra: Any = None) -> AddresseeFacts:
        """本轮允许集合的三个来源（全部取既有上下文，缺哪个就是哪个没有）。"""
        from core.conversation.addressing import pat_sender, quote_of

        data = extra if isinstance(extra, dict) else {}
        target = getattr(turn, "target", None)

        # ① 本轮触发者：turn 的规范事实优先，回落 router 写进 extra 的那份。
        trigger_name = _text(getattr(target, "sender_name", "")) or _text(
            data.get("sender_name"))
        trigger_id = _text(getattr(target, "sender_id", "")) or _text(
            data.get("sender_id"))
        trigger = (
            AddresseePerson(
                name=trigger_name, raw_id=trigger_id, source=ADD_SOURCE_TRIGGER,
            )
            if (trigger_name or trigger_id) else None
        )

        # ② 被引用消息的作者：本轮指代锚（`extra["referent"]`）优先，回落 canonical
        #    对触发消息正文的 `[引用 X: …]` 解析（认不出作者 → ""，不装懂）。
        quoted_name = ""
        referent = data.get("referent")
        if isinstance(referent, dict):
            quoted_name = _text(referent.get("quoted_author"))
        if not quoted_name:
            quoted_name = _text(quote_of(_inbound_text(turn, data)).author_name)
        quoted = (
            AddresseePerson(name=quoted_name, source=ADD_SOURCE_QUOTED)
            if quoted_name else None
        )

        return AddresseeFacts(
            trigger=trigger,
            quoted=quoted,
            summons=self._summons_sender(turn, data, trigger),
        )

    @staticmethod
    def _summons_sender(turn: Any, extra: dict,
                        trigger: AddresseePerson | None) -> AddresseePerson | None:
        """本轮「呼唤形式」的发出者（R-A10 四种）。

        四种形式里，@她 / 含她名字 / 引用她的**发出者就是那条消息的作者** ——
        与触发者同一个人，已在 ``trigger`` 槽里；只有**拍她**的作者不在这条链上：
        它只存在于拍一拍的 prose 里（canonical ``addressing.pat_sender``，非拍返回
        ``""``），``sender_name`` 缺失的拍一拍（线上拍一拍常无 ``sender_id``）靠它
        才认得出人。所以这一槽只补拍。
        """
        from core.conversation.addressing import pat_sender

        name = _text(pat_sender(_inbound_text(turn, extra)))
        if not name or (trigger is not None and name == trigger.name):
            return None
        return AddresseePerson(name=name, source=ADD_SOURCE_SUMMONS)

    # ------------------------------------------------------------------ 判定
    def decide(self, text: str, facts: AddresseeFacts | None, *,
               platform: str = "") -> AddresseeDecision:
        """文本里首个招呼位 @ 目标是否合法；不合法给出改正/去掉后的正文。"""
        raw = str(text or "")
        if not raw.strip():
            return AddresseeDecision(text=raw, reason="empty")
        span = _address_span(raw)
        if span is None:
            return AddresseeDecision(text=raw, reason="no_address")
        name, start, end = span
        if _is_broadcast_mention(name):
            # `@所有人` 不是"某人"：裁决不适用，绝不把它改成个人目标。
            return AddresseeDecision(text=raw, from_name=name, reason="broadcast")
        allowed = facts.allowed() if facts is not None else ()
        if self._matches(name, allowed, platform=platform) is not None:
            return AddresseeDecision(
                text=raw, from_name=name, reason="allowed",
            )
        trigger = getattr(facts, "trigger", None) if facts is not None else None
        target = _text(getattr(trigger, "name", "")) or _text(
            getattr(trigger, "raw_id", ""))
        if target:
            return AddresseeDecision(
                action=ADD_ACTION_REPAIR,
                text=raw[:start] + "@" + target + raw[end:],
                from_name=name, to_name=target, reason="not_in_allowed_set",
            )
        return AddresseeDecision(
            action=ADD_ACTION_DROP,
            text=_drop_address(raw, start, end),
            from_name=name, reason="no_trigger",
        )

    def _matches(self, name: str, allowed: tuple[AddresseePerson, ...], *,
                 platform: str = "") -> AddresseePerson | None:
        """@ 到的名字是不是允许集合里的人（名字 / 原始 id / canonical 人 id 三档）。"""
        key = str(name or "").strip().casefold()
        if not key or not allowed:
            return None
        for person in allowed:
            if str(person.name or "").strip().casefold() == key:
                return person
            if str(person.raw_id or "").strip().casefold() == key:
                return person
        # 昵称 / 备注 / 别名：字面对不上时走 canonical「人名 → 人 id」再比一次
        # （同一个人的两个名字算同一个人；认不出人 id 就不猜）。
        person_id = self.person_id_of(name, platform=platform)
        if not person_id:
            return None
        for person in allowed:
            other = self.person_id_of(person.name, platform=platform)
            if other and other == person_id:
                return person
        return None


_ADDRESSEE_RESOLVER: AddresseeResolver | None = None


def addressee_resolver() -> AddresseeResolver:
    """进程内单例（无状态判定器；测试可另建实例注入 ``person_id_of``）。"""
    global _ADDRESSEE_RESOLVER
    if _ADDRESSEE_RESOLVER is None:
        _ADDRESSEE_RESOLVER = AddresseeResolver()
    return _ADDRESSEE_RESOLVER


class PersonaResolver:
    """Resolve the effective persona for a turn using scope bindings."""

    def __init__(self, store: SpeechStore):
        self.store = store

    def _binding_candidates(self, turn: TurnContext) -> list[tuple[str, str]]:
        t = turn.target
        candidates: list[tuple[str, str]] = [
            (ScopeType.CHAT.value, t.chat_id),
            (ScopeType.GROUP.value, t.chat_id) if t.is_group else ("__skip__", ""),
            (ScopeType.CONTACT.value, t.chat_id if not t.is_group else t.sender_id),
            (ScopeType.ACCOUNT.value, t.account_id),
            (ScopeType.PLATFORM.value, t.platform),
            (ScopeType.CHAT_TYPE.value, turn.scene),
            (ScopeType.GLOBAL.value, ""),
        ]
        return [(s, i) for s, i in candidates if s != "__skip__"]

    def resolve(self, turn: TurnContext, repo: Any) -> ResolvedPersona:
        binding = None
        for scope_type, scope_id in self._binding_candidates(turn):
            binding = self.store.find_binding(
                scope_type, scope_id,
                platform=turn.target.platform,
                account_id=turn.target.account_id,
                chat_type=turn.target.chat_type,
            )
            if binding is not None:
                break

        persona_id = binding.persona_id if binding else None
        if persona_id is None:
            persona = self._active_persona(repo)
            persona_id = getattr(persona, "id", None)
            if persona_id is None:
                raise RuntimeError("no active persona available for speech resolution")
        else:
            persona = None
            try:
                persona = repo.personas.get_by_id(int(persona_id))
            except Exception:
                warn("core/speech/resolver.py:580 Exception", err=repr(__import__("sys").exc_info()[1]))
                persona = None

        version_model = None
        if binding and binding.persona_version:
            version_model = self.store.get_persona_version(
                int(persona_id), int(binding.persona_version)
            )
        if version_model is None:
            version_model = self.store.get_latest_persona_version(int(persona_id))
        if version_model is None:
            if persona is None:
                persona = self._active_persona(repo)
            if persona is None:
                raise RuntimeError("persona has no published version")
            version_model = self.store.upsert_persona_version_from_persona(
                persona, created_by="runtime",
            )

        scene_styles = _loads(version_model.scene_styles_json, {})
        identity = _loads(version_model.identity_json, {})
        traits = _loads(version_model.traits_json, {})
        voice = _loads(version_model.voice_json, {})
        prompt_sections_raw = _loads(version_model.prompt_sections_json, []) or []
        aliases = tuple(_loads(version_model.aliases_json, []) or ())

        if binding is not None:
            overrides = _loads(binding.overrides_json, {}) or {}
            if overrides.get("identity"):
                identity = dict(identity)
                identity.update(overrides["identity"])
            if overrides.get("traits"):
                traits = dict(traits)
                traits.update(overrides["traits"])
            if overrides.get("voice"):
                voice = dict(voice)
                voice.update(overrides["voice"])
            if overrides.get("scene_styles"):
                scene_styles = dict(scene_styles)
                scene_styles.update(overrides["scene_styles"])
            if overrides.get("prompt_sections"):
                prompt_sections_raw = overrides["prompt_sections"]

        sections = tuple(
            PromptSection(
                key=str(s.get("key") or f"section_{i}"),
                template=str(s.get("template") or ""),
                priority=int(s.get("priority") or 100),
                enabled=bool(s.get("enabled", True)),
            )
            for i, s in enumerate(prompt_sections_raw)
            if isinstance(s, dict)
        )
        return ResolvedPersona(
            persona_id=int(version_model.persona_id),
            version=int(version_model.version),
            key=str(getattr(persona, "id", version_model.persona_id)),
            # 2026-09-26 批0：组装层要的是「她是谁」（personas.name），不是版本说明。
            # persona_versions.name 形如「鲸鲸 V2 · 殷勤狐狼侍从」——原顺序把它排在
            # 真名前面，system 首行直接渲染成「你是鲸鲸 V2 · …」，被问「你是谁」会
            # 自报版本号。真名优先，版本名只作兜底。
            name=(
                str(getattr(persona, "name", "") or "").strip()
                or str(getattr(version_model, "name", "") or "").strip()
                or "默认人设"
            ),
            aliases=aliases or (getattr(persona, "name", "") or "",),
            identity=identity,
            traits=traits,
            voice=voice,
            scene_styles=scene_styles,
            prompt_sections=sections,
            source="binding" if binding else "active_persona",
        )

    def _active_persona(self, repo: Any):
        try:
            active = list(repo.personas.list_active())
            if active:
                return active[0]
        except Exception:
            warn("core/speech/resolver.py:660 Exception", err=repr(__import__("sys").exc_info()[1]))
        try:
            rows = list(repo.personas.list_all())
            if rows:
                return rows[0]
        except Exception:
            warn("core/speech/resolver.py:666 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


class ConductResolver:
    """Compile matching policy rules into a SpeakingPolicy."""

    def __init__(self, store: SpeechStore):
        self.store = store

    def _matches_scope(self, rule, turn: TurnContext) -> bool:
        t = turn.target
        scope = rule.scope_type or ScopeType.GLOBAL.value
        sid = rule.scope_id or ""
        if rule.platform and rule.platform != t.platform:
            return False
        if rule.account_id and rule.account_id != t.account_id:
            return False
        if rule.chat_type and rule.chat_type != t.chat_type:
            return False
        if rule.scene and rule.scene != turn.scene:
            return False

        if scope == ScopeType.GLOBAL.value:
            return True
        if scope == ScopeType.PLATFORM.value:
            return sid == t.platform if sid else True
        if scope == ScopeType.ACCOUNT.value:
            return sid == t.account_id if sid else True
        if scope == ScopeType.CHAT_TYPE.value:
            return sid == turn.scene if sid else True
        if scope == ScopeType.CHAT.value:
            return sid == t.chat_id
        if scope == ScopeType.GROUP.value:
            return t.is_group and sid == t.chat_id
        if scope == ScopeType.CONTACT.value:
            target_id = t.chat_id if not t.is_group else t.sender_id
            return sid == target_id
        return False

    def _matches_conditions(self, rule, turn: TurnContext) -> bool:
        cond = _loads(getattr(rule, "conditions_json", None), {}) or {}
        facts = {
            "audience": turn.audience.value if isinstance(turn.audience, AudienceKind) else str(turn.audience),
            "sender_id": turn.target.sender_id,
            "sender_name": turn.target.sender_name,
            "chat_id": turn.target.chat_id,
            "chat_type": turn.target.chat_type,
            "platform": turn.target.platform,
            "account_id": turn.target.account_id,
            "msg_type": turn.msg_type,
            "is_owner": bool(turn.is_owner),
            "is_mentioned": bool(turn.is_mentioned),
            "trigger_reason": turn.trigger_reason,
            "sender_role": turn.sender_role,
            "relationship_tier": turn.relationship_tier,
            "intimacy": turn.intimacy,
        }
        return match_conditions(cond, facts)

    def _rule_payload(self, rule) -> dict[str, Any]:
        return _loads(getattr(rule, "payload_json", None), {}) or {}

    def _profile_scope_hits(self, turn: TurnContext) -> list[tuple[str, str]]:
        t = turn.target
        hits = [
            (ScopeType.CHAT.value, t.chat_id),
            (ScopeType.GROUP.value, t.chat_id) if t.is_group else None,
            (ScopeType.CONTACT.value, t.chat_id if not t.is_group else t.sender_id),
            (ScopeType.ACCOUNT.value, t.account_id),
            (ScopeType.PLATFORM.value, t.platform),
            (ScopeType.CHAT_TYPE.value, turn.scene),
            (ScopeType.GLOBAL.value, ""),
        ]
        return [h for h in hits if h is not None]

    def resolve(self, turn: TurnContext, persona: ResolvedPersona) -> SpeakingPolicy:
        all_rules = self.store.list_rules(enabled_only=True)
        profile_keys = self.store.resolve_profile_keys(self._profile_scope_hits(turn))
        profile_rule_keys = {
            r.rule_key for r in self.store.list_profile_rules()
            if r.profile_key in profile_keys
        }
        direct_rules = [
            r for r in all_rules
            if self._matches_scope(r, turn) and self._matches_conditions(r, turn)
        ]
        profile_rules = [
            r for r in all_rules
            if r.key in profile_rule_keys and self._matches_conditions(r, turn)
        ]
        seen: set[str] = set()
        rules = []
        for rule in direct_rules + profile_rules:
            key = str(rule.key or rule.id)
            if key in seen:
                continue
            seen.add(key)
            rules.append(rule)
        # More specific scopes and higher priorities apply later/stronger.
        rules.sort(key=lambda r: (int(r.priority or 0), _scope_score(r.scope_type)))

        account = {
            "allowed_accounts": ("self", "owner"),
            "allow_cross_chat": False,
            "require_confirmation": False,
        }
        target = {
            "allow_cross_chat": False,
            "denied_sender_ids": (),
            "required_chat_id": turn.target.chat_id,
        }
        tool = {
            "allowed_tools": None,
            "denied_tools": (),
            "max_calls": 0,
            "sticker_scopes": (),
            "require_confirmation": (),
        }
        output = {
            "max_chars": 0,
            "max_lines": 0,
            "forbidden_terms": (),
            "required_phrases": (),
            "replacement_text": "",
            "style_examples": "",
        }
        hard: list[HardConstraint] = []
        prompt_parts: list[str] = []
        source_ids: list[int] = []

        for rule in rules:
            source_ids.append(int(rule.id))
            payload = dict(self._rule_payload(rule))
            payload.setdefault("match_type", getattr(rule, "match_type", "") or "always")
            if getattr(rule, "pattern", None):
                payload.setdefault("pattern", rule.pattern)
            category = rule.category or RuleCategory.STYLE.value
            enforcement = rule.enforcement or Enforcement.PROMPT.value
            effect = rule.effect or RuleEffect.ALLOW.value
            rid = str(rule.key or rule.id)

            if enforcement == Enforcement.SEMANTIC.value:
                payload = dict(payload)
                payload["category"] = category
                payload.setdefault("action", "regenerate_then_replace")
                hard.append(HardConstraint(
                    "semantic", rid, payload, int(rule.priority or 0),
                ))
                hint = str(payload.get("prompt_hint") or "")
                if hint:
                    prompt_parts.append(hint)
                continue

            if category == RuleCategory.ACCOUNT.value:
                allowed = payload.get("allowed_accounts")
                if allowed:
                    account["allowed_accounts"] = tuple(str(x) for x in allowed)
                if "allow_cross_chat" in payload:
                    account["allow_cross_chat"] = bool(payload["allow_cross_chat"])
                if "require_confirmation" in payload:
                    account["require_confirmation"] = bool(payload["require_confirmation"])
                if enforcement in (Enforcement.DETERMINISTIC.value, Enforcement.HYBRID.value):
                    hard.append(HardConstraint("account", rid, dict(account), int(rule.priority or 0)))
                continue

            if category == RuleCategory.TARGET.value:
                if "allow_cross_chat" in payload:
                    target["allow_cross_chat"] = bool(payload["allow_cross_chat"])
                if payload.get("denied_sender_ids"):
                    target["denied_sender_ids"] = tuple(str(x) for x in payload["denied_sender_ids"])
                if payload.get("required_chat_id"):
                    target["required_chat_id"] = str(payload["required_chat_id"])
                if enforcement in (Enforcement.DETERMINISTIC.value, Enforcement.HYBRID.value):
                    hard.append(HardConstraint("target", rid, dict(target), int(rule.priority or 0)))
                continue

            if category == RuleCategory.TOOL.value:
                if "allowed_tools" in payload:
                    allowed = payload.get("allowed_tools")
                    tool["allowed_tools"] = tuple(str(x) for x in allowed) if allowed else None
                if payload.get("denied_tools"):
                    tool["denied_tools"] = tuple(
                        set(tool["denied_tools"]) | {str(x) for x in payload["denied_tools"]}
                    )
                if "max_calls" in payload:
                    tool["max_calls"] = int(payload.get("max_calls") or 0)
                if payload.get("sticker_scopes"):
                    tool["sticker_scopes"] = tuple(str(x) for x in payload["sticker_scopes"])
                if payload.get("require_confirmation"):
                    tool["require_confirmation"] = tuple(
                        set(tool["require_confirmation"]) |
                        {str(x) for x in payload["require_confirmation"]}
                    )
                if enforcement in (Enforcement.DETERMINISTIC.value, Enforcement.HYBRID.value):
                    hard.append(HardConstraint("tool", rid, dict(tool), int(rule.priority or 0)))
                continue

            if category == RuleCategory.FORMAT.value:
                if payload.get("max_chars"):
                    output["max_chars"] = int(payload["max_chars"])
                if payload.get("max_lines"):
                    output["max_lines"] = int(payload["max_lines"])
                if payload.get("forbidden_terms"):
                    output["forbidden_terms"] = tuple(
                        set(output["forbidden_terms"]) | {str(x) for x in payload["forbidden_terms"]}
                    )
                if payload.get("required_phrases"):
                    output["required_phrases"] = tuple(
                        set(output["required_phrases"]) | {str(x) for x in payload["required_phrases"]}
                    )
                if payload.get("replacement"):
                    output["replacement_text"] = str(payload["replacement"])
                if enforcement in (Enforcement.DETERMINISTIC.value, Enforcement.HYBRID.value):
                    hard.append(HardConstraint("format", rid, dict(output), int(rule.priority or 0)))
                continue

            if category == RuleCategory.SENSITIVE.value:
                terms = list(payload.get("forbidden_terms") or [])
                lexicon_key = payload.get("lexicon_key")
                if lexicon_key and not terms:
                    terms = [t["term"] for t in self.store.get_lexicon_terms(str(lexicon_key))]
                if terms:
                    output["forbidden_terms"] = tuple(set(output["forbidden_terms"]) | set(terms))
                payload = dict(payload)
                payload["terms"] = terms

                # Prompt-first: tell the model before generation so the text
                # does not need to be patched afterwards.  The hard constraint
                # below is still the guarantee when the model slips.
                hint = str(payload.get("prompt_hint") or "").strip()
                action = str(payload.get("action") or "").strip().lower()
                mode = str(payload.get("mode") or "").strip().lower()
                term_replace = action in {
                    "replace_terms", "replace_words", "replace_word",
                    "mask_terms", "mask",
                } or mode in {"term", "terms", "mask", "word", "words"}
                if not hint and terms:
                    replacement = str(payload.get("replacement") or "").strip()
                    joined = "、".join(str(x) for x in terms[:20])
                    if term_replace:
                        hint = f"绝对不能出现这些词或说法：{joined}。"
                        if replacement:
                            hint += f"需要表达相同意思时，改用：{replacement}。"
                        else:
                            hint += "直接删除这些词后自然重写。"
                        hint += "不要因为命中这些词就把整句回答变成生硬的固定话术。"
                    else:
                        hint = (
                            f"避免出现这些词或话题：{joined}。"
                            "如果不小心提到，换一种安全、自然的表达。"
                        )
                if hint:
                    prompt_parts.append(hint)
                if effect == RuleEffect.DENY.value and enforcement in (
                    Enforcement.DETERMINISTIC.value, Enforcement.HYBRID.value,
                ):
                    hard.append(HardConstraint("sensitive", rid, payload, int(rule.priority or 0)))
                continue

            # topic / privacy / style / rate and generic fallback
            rule_key = str(getattr(rule, "key", "") or "")
            is_examples_rule = (
                rule_key == "simple_style_examples"
                or bool(payload.get("style_examples_rule"))
            )
            if is_examples_rule:
                examples_text = str(
                    payload.get("prompt_hint")
                    or payload.get("style_examples")
                    or ""
                ).strip()
                if examples_text:
                    output["style_examples"] = examples_text
                continue

            hint = str(payload.get("prompt_hint") or "")
            if hint:
                prompt_parts.append(hint)
            terms = [str(x) for x in (payload.get("forbidden_terms") or [])]
            if terms:
                payload = dict(payload)
                payload["terms"] = terms
                if enforcement in (Enforcement.DETERMINISTIC.value, Enforcement.HYBRID.value):
                    hard.append(HardConstraint(category, rid, payload, int(rule.priority or 0)))

        prompt_brief = "\n".join(p for p in prompt_parts if p).strip()
        return SpeakingPolicy(
            revision=self.store.get_revision(),
            target=turn.target,
            audience=turn.audience,
            persona=persona,
            prompt_brief=prompt_brief,
            hard_constraints=tuple(hard),
            account_policy=AccountPolicy(**account),
            target_policy=TargetPolicy(**target),
            tool_policy=ToolPolicy(**tool),
            output_policy=OutputPolicy(**output),
            source_rule_ids=tuple(str(x) for x in source_ids),
            engagement={
                "reply_mode": next(
                    (
                        p.get("reply_mode")
                        for p in (
                            self._rule_payload(r)
                            for r in rules
                            if r.category == RuleCategory.ENGAGEMENT.value
                        )
                        if p.get("reply_mode")
                    ),
                    "",
                )
            },
        )


class EngagementResolver:
    """Resolve the inbound "should I speak?" policy.

    The defaults encode the historical product behaviour, while every part can
    be overridden by ``category=engagement`` rows in ``policy_rules``.  The
    router no longer hardcodes owner/mention/pat/deferred decisions.
    """

    def __init__(self, store: SpeechStore):
        self.store = store

    def _default(self, turn: TurnContext) -> EngagementDecision:
        target = turn.target
        if target.platform == "web":
            return EngagementDecision(ReplyMode.ALWAYS, reply_reason="web_chat")
        if target.is_group:
            if turn.trigger_reason == "patted_in_group":
                return EngagementDecision(ReplyMode.ALWAYS,
                                          reply_reason="patted_in_group")
            if turn.is_mentioned:
                return EngagementDecision(ReplyMode.ALWAYS,
                                          reply_reason="mentioned_in_group")
            return EngagementDecision(ReplyMode.DEFER, defer_seconds=15.0,
                                      reply_reason="group_deferred_to_state")
        if turn.is_owner:
            return EngagementDecision(ReplyMode.ALWAYS,
                                      reply_reason="owner_private_message")
        return EngagementDecision(ReplyMode.URGENT,
                                  reply_reason="non_owner_private_urgent")

    def resolve(self, turn: TurnContext, repo: Any = None) -> EngagementDecision:
        default = self._default(turn)
        # USER_IGNORE_GROUPS predates the rule engine; keep it authoritative
        # for operators who already configured it in .env.
        try:
            from core.config.settings import config
            if turn.target.is_group and turn.target.chat_id in set(
                    config.user_profile.ignore_groups or []):
                return EngagementDecision(ReplyMode.NEVER, reply_reason="ignored_group")
        except Exception:
            warn("core/speech/resolver.py:1022 Exception", err=repr(__import__("sys").exc_info()[1]))
        try:
            rules = [
                r for r in self.store.list_rules(enabled_only=True)
                if r.category == RuleCategory.ENGAGEMENT.value
            ]
        except Exception:
            warn("core/speech/resolver.py:1029 Exception", err=repr(__import__("sys").exc_info()[1]))
            return default

        matched: list[Any] = []
        for rule in rules:
            if not self._matches_scope(rule, turn):
                continue
            if not self._matches_conditions(rule, turn):
                continue
            matched.append(rule)
        if not matched:
            return default

        matched.sort(key=lambda r: (int(r.priority or 0), _scope_score(r.scope_type)))
        chosen = matched[-1]
        payload = _loads(getattr(chosen, "payload_json", None), {}) or {}
        mode_raw = str(payload.get("reply_mode") or "").lower()
        mode_map = {
            "always": ReplyMode.ALWAYS,
            "immediate": ReplyMode.ALWAYS,
            "mention": ReplyMode.MENTION,
            "mentioned": ReplyMode.MENTION,
            "urgent": ReplyMode.URGENT,
            "defer": ReplyMode.DEFER,
            "deferred": ReplyMode.DEFER,
            "never": ReplyMode.NEVER,
            "silent": ReplyMode.NEVER,
        }
        mode = mode_map.get(mode_raw, default.reply_mode)
        return EngagementDecision(
            reply_mode=mode,
            defer_seconds=float(payload.get("defer_seconds") or default.defer_seconds or 0.0),
            reply_reason=str(payload.get("reason") or chosen.key or default.reply_reason),
            rule_ids=(str(chosen.key or chosen.id),),
        )

    def _matches_scope(self, rule, turn: TurnContext) -> bool:
        # Reuse the same scope semantics as ConductResolver.
        resolver = ConductResolver.__new__(ConductResolver)
        return resolver._matches_scope(rule, turn)

    def _matches_conditions(self, rule, turn: TurnContext) -> bool:
        resolver = ConductResolver.__new__(ConductResolver)
        return resolver._matches_conditions(rule, turn)
