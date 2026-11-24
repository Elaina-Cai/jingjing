from __future__ import annotations

import re
import structlog

from agents.tools.base import BaseTool, ToolResult
from core.speech.domain import OutboundDraft
from safelog import warn

log = structlog.get_logger()


def _should_defer_sticker(ctx) -> bool:
    """本轮表情是否延迟到文字回复之后：typed decision 优先，extra 仅兜底。

    * 直回路径挂 typed True；
    * 触发路径 typed 默认 False（没有 flush 口，保持原行为）；
    * Web/主动工具/duck 没有 typed carrier 时回落旧 extra 投影。
    """
    if ctx is None:
        return False
    value = None
    reader = getattr(ctx, "decision_value", None)
    if callable(reader):
        value = reader("defer_sticker_until_after_reply", None)
    if value is None:
        value = (getattr(ctx, "extra", None) or {}).get(
            "defer_sticker_until_after_reply"
        )
    return bool(value)


def _load_sticker_catalog() -> dict[str, dict]:
    """The moods the sender can actually resolve, from the live registry.

    Only sendable ones: the catalog is what validates a requested mood, so a
    registered-but-imageless entry would pass validation here and then fail in
    the sender with "未找到表情包配置" -- exactly how 得意 kept failing.
    """
    catalog: dict[str, dict] = {}
    try:
        from db import sticker_store

        for sticker in sticker_store.sendable_stickers("wechat", None):
            entry = catalog.get(sticker.emotion)
            if entry is None:
                catalog[sticker.emotion] = {
                    "label": sticker.label or sticker.emotion,
                    "aliases": list(sticker.aliases or ()),
                    "context": sticker.context or "",
                    "wording": _entry_wording({"context": sticker.context,
                                               "label": sticker.label}),
                    "scope": list(sticker.scope or ()),
                }
                continue
            entry["aliases"] = list(dict.fromkeys(
                list(entry.get("aliases") or ()) + list(sticker.aliases or ())))
            entry["scope"] = list(dict.fromkeys(
                list(entry.get("scope") or ()) + list(sticker.scope or ())))
            if not entry.get("context") and sticker.context:
                entry["context"] = sticker.context
    except Exception as exc:
        log.warning("sticker_catalog_load_failed", error=str(exc))
    return catalog


_VALID_RELATIONS = ["亲密", "熟悉", "普通", "群聊"]


def _sticker_candidates_mode() -> str:
    """off / shadow / on for the free-text candidate lens.

    Read with ``features.mode`` (three-state): ``enabled`` reads shadow as
    False, which would silently turn the shadow gauge into an off switch.
    """
    try:
        from core import features

        return features.mode(features.STICKER_CANDIDATES, features.MODE_OFF)
    except Exception:
        warn("agents/tools/send_sticker.py:79 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def _entry_wording(info: dict) -> str:
    """The entry's own wording; a machine-noise label is skipped."""
    values = [info.get("context"), info.get("label")]
    try:
        from db import sticker_store

        values = [v for v in values if not sticker_store.looks_encoded(str(v or ""))]
    except Exception:
        warn("agents/tools/send_sticker.py:90 Exception", err=repr(__import__("sys").exc_info()[1]))
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _wording_score(request: str, *fields: str) -> int:
    """Longest run of shared characters between the request and an entry's wording.

    Deliberately mechanical, not a keyword list: any two adjacent characters
    count, so the matched span can be reported back verbatim instead of being
    replaced by a category name.
    """
    needle = "".join(ch for ch in (request or "") if not ch.isspace())
    if len(needle) < 3:
        return 0
    best = 0
    for field in fields:
        hay = "".join(ch for ch in (field or "") if not ch.isspace())
        if not hay:
            continue
        for start in range(len(needle)):
            for size in range(best + 1, len(needle) - start + 1):
                if needle[start:start + size] in hay:
                    best = size
                else:
                    break
    return best


def _candidate_report(request: str, *, platform: str = "wechat") -> dict:
    """Every entry the model's own words could mean, each with its reason.

    Three tiers, strongest first: exact name, declared alias, wording overlap
    with the entry's own description.  Nothing is picked here -- the caller
    decides, and when the evidence does not decide, the answer is "abstain".
    """
    report = {"request": request, "by_name": [], "by_alias": [], "by_wording": []}
    try:
        from db import sticker_store

        entries = list(sticker_store.sendable_stickers(platform, None))
    except Exception as exc:
        log.warning("sticker_candidate_scan_failed", error=str(exc))
        return report

    for candidate in sticker_store.match_candidates(request, entries):
        row = candidate.as_dict()
        tier = "by_name" if candidate.matched_by == "name" else "by_alias"
        report[tier].append(row)

    if not report["by_name"] and not report["by_alias"]:
        scored = []
        for sticker in entries:
            wording = _entry_wording({"context": sticker.context,
                                      "label": sticker.label}) or sticker.emotion
            # 名字本身也算一个措辞场：模型写「喝奶茶看戏」对上登记的「喝奶茶，看戏」时，
            # 差的是一个逗号，不该被判成"没有这张"。同义词仍然要靠 registry 侧维护别名。
            score = _wording_score(request, sticker.description, wording, sticker.emotion)
            if score >= 3:
                scored.append((score, sticker, wording))
        for score, sticker, wording in scored:
            report["by_wording"].append({
                "emotion": sticker.emotion, "matched_by": "wording",
                "field": "description", "value": request, "siblings": [],
                "description": sticker.description, "wording": wording,
                "scope": list(sticker.scope or ()), "score": score,
            })
    return report


def _choose_candidate(report: dict, relation: str) -> tuple[str, str]:
    """(emotion, decision): 择一 only when the evidence is unique, else 弃权."""
    for tier in ("by_name", "by_alias", "by_wording"):
        rows = list(report.get(tier) or [])
        if not rows:
            continue
        allowed = [row for row in rows
                   if not row.get("scope") or relation in row.get("scope", [])]
        if len(rows) == 1 and len(allowed) == 1:
            return allowed[0].get("emotion", ""), tier
        if len(allowed) == 1:
            return allowed[0].get("emotion", ""), tier + "_by_scope"
        return "", "ambiguous"
    return "", "no_match"


def _candidate_hint(report: dict, limit: int = 6) -> str:
    """A short, honest hint: names, and the reason each one came up."""
    parts = []
    for row in report.get("by_name", []) + report.get("by_alias", []) + report.get("by_wording", []):
        reason = row.get("matched_by")
        label = row.get("emotion") or ""
        wording = row.get("description") or ""
        if reason == "wording":
            parts.append(f"{label}（措辞像：{wording}）")
        elif reason == "alias":
            parts.append(f"{label}（别名 {row.get('value')} 也对上，同时对上："
                         f"{'/'.join(row.get('siblings') or []) or '无'}）")
        else:
            parts.append(label)
    return "；".join(parts[:limit])


def _candidate_error_text(request: str, report: dict, decision: str) -> str:
    """Keep the model's words in the error; never answer with a closed list only."""
    hint = _candidate_hint(report)
    if decision == "ambiguous":
        return (f"「{request}」能对上不止一个表情（{hint}），没法替你挑；"
                "要哪个请直接说名字，或者说得更具体。本轮不发，免得发错。")
    return (f"没有登记的表情对得上「{request}」。你的原话我留着（不用从清单里挑名字，"
            "直接描述想发什么就行）。"
            + (f" 相近的有：{hint}。" if hint else ""))


def _build_sticker_enum() -> list[str]:
    return list(_load_sticker_catalog().keys())


def _build_sticker_description() -> str:
    catalog = _load_sticker_catalog()
    free_text = _sticker_candidates_mode() == "on"
    naming = (
        "想发什么就用自然语言描述（也可以用下面已登记的名字）：同名同义的说法会自动对上，"
        "对不上或有歧义时会明说，不拿最像的一个顶替。\n"
        if free_text else
        "表情名即id，从enum中精确选取一个名字，模型自行判断当前语境适合哪个。\n"
    )
    base = (
        "根据当前对话情境选择最合适的表情包发送；轻松、有情绪或气氛合适时优先主动发一张。\n"
        "请结合鲸鲸的人设和当前对话内容自行判断该发什么表情，不必拘泥于固定模式。\n"
        + naming +
        "关系层级会根据联系人数据库自动推断（主人→亲密，好友→熟悉，群聊→群聊），scope为硬约束。\n"
        "要经常主动使用，不用等用户开口；但一轮最多一张，不要连续刷屏，严肃、正式或对方明显不悦时不要发。"
    )
    if catalog:
        lines = []
        for mid, info in catalog.items():
            scope = info.get("scope", [])
            scope_str = f"[仅{','.join(scope)}]" if scope else ""
            wording = info.get("wording") or ""
            hint = f"｜{wording}" if (free_text and wording) else ""
            lines.append(f"- {mid}{hint} {scope_str}".rstrip())
        head = "\n\n可选表情（名字或描述都行）:\n" if free_text else "\n\n可选表情:\n"
        base += head + "\n".join(lines)
    return base


_INTIMACY_TO_RELATION = [
    (0.8, "亲密"),
    (0.5, "熟悉"),
    (0.0, "普通"),
]


def _resolve_relation_for_turn(who: str, fallback: str, snapshot) -> str:
    """Prefer the relationship facts already resolved onto the turn."""
    from db.models import is_group_chat_id

    if is_group_chat_id(who):
        return "群聊"
    if snapshot is not None:
        tier = (getattr(snapshot.turn, "relationship_tier", "") or "").strip()
        if tier in _VALID_RELATIONS:
            return tier
        intimacy = getattr(snapshot.turn, "intimacy", None)
        if intimacy is not None:
            for threshold, label in _INTIMACY_TO_RELATION:
                if intimacy >= threshold:
                    return label
    return fallback


def _resolve_relation_from_db(who: str, platform: str, account_id: str) -> str:
    # 2026-10-10 GAP-2：不再自己解析微信 id 后缀。群聊判定走唯一权威
    # ``core.identity.platforms``（它那里是 canonical chat_type 优先、平台后缀兜底），
    # 工具层不该知道"微信群 id 以 @chatroom 结尾"这种平台事实。
    from core.identity.platforms import is_group_chat

    if is_group_chat(platform, chat_id=who):
        return "群聊"
    try:
        from core.terminal.store_port import get_repo
        repo = get_repo()
        rel = repo.relationships.get(persona_id=1, target_wxid=who)
        if rel and rel.relation_type:
            rt = rel.relation_type
            if rt in _VALID_RELATIONS:
                return rt
        if rel and rel.intimacy is not None:
            for threshold, label in _INTIMACY_TO_RELATION:
                if rel.intimacy >= threshold:
                    return label
    except Exception:
        warn("agents/tools/send_sticker.py:286 Exception", err=repr(__import__("sys").exc_info()[1]))
    return "普通"


def _sticker_rule_gate(ctx, who: str, mood_id: str, emotion: str,
                       platform: str, account_choice: str):
    """N4 收编点 ⑧：问一次规则引擎里 ``send_sticker`` 的动作前置条件。

    只做形状适配（AgentContext → ``ActionContext``），**判据在引擎**。
    引擎 ``off``（默认）/shadow/shadow-放行一律返回 ``None``＝不拦；
    ``on`` 且判为拒绝时返回一个 ``ToolResult``（错误文案即 ``explanation``：
    R-V3「UI 能说出这条为什么生效」的载体）。引擎不可用一律 ``None``。
    """
    try:
        from core.rules import engine as rules_engine
    except Exception:  # noqa: BLE001 - 引擎不可导入就退回原路径
        warn("agents/tools/send_sticker.py:302 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    extra = (getattr(ctx, "extra", None) or {}) if ctx is not None else {}
    action_ctx = rules_engine.ActionContext(
        action_id="send_sticker",
        actor_id=str(extra.get("sender_id") or ""),
        chat_id=str(extra.get("chat_id") or ""),
        chat_type="group" if extra.get("is_group") else "private",
        platform=str(extra.get("platform") or platform or ""),
        account_id=str(extra.get("account_id") or ""),
        is_owner=bool(rules_engine.is_owner(ctx)),
        is_group=bool(extra.get("is_group")),
        params={
            "who": who, "mood_id": mood_id, "emotion": emotion,
            "account_choice": account_choice,
        },
    )
    try:
        decision = rules_engine.decide(action_ctx)
    except Exception as exc:  # noqa: BLE001 - 判定异常绝不挡发送
        log.warning("sticker_rule_gate_failed", error=str(exc)[:160])
        return None
    if decision.allowed:
        return None
    from agents.tools.base import ToolResult

    return ToolResult(
        "",
        error=decision.explanation or "动作 send_sticker 的前置条件未满足",
    )


class SendStickerTool(BaseTool):
    name = "send_sticker"
    description = _build_sticker_description()
    parameters = {
        "who": {
            "type": "string",
            "description": "接收者，可选。不填则默认发到当前会话（群聊会自动使用当前群）",
            "required": False,
        },
        "sticker": {
            "type": "string",
            "description": "表情包标识，从enum中选取精确值",
            "enum": _build_sticker_enum(),
        },
        "emotion": {
            "type": "string",
            "description": "自然语言情感描述，用于模糊匹配表情包。当sticker无法精确表达时使用",
        },
        "platform": {
            "type": "string",
            "description": "发送平台: wechat(默认)/qq/telegram",
        },
        "account": {
            "type": "string",
            "description": "使用哪个账号发送: self(鲸鲸自己的号，默认)/owner(主人的号)",
        },
    }

    def schema(self) -> dict:
        """Rebuild the mood enum per call so newly imported stickers work without a restart."""
        self.description = _build_sticker_description()
        params = dict(self.parameters)
        sticker = dict(params.get("sticker") or {})
        if _sticker_candidates_mode() == "on":
            # 写时不承诺：不把"能发什么"压成一个闭集去要求模型"必须是其中之一"。
            # 精确名字仍然优先，描述同样可选（``emotion`` 一直是自由文本）。
            sticker.pop("enum", None)
            sticker["description"] = ("想发什么就描述什么（自然语言），"
                                      "也可以用下面已登记的表情名")
        else:
            sticker["enum"] = _build_sticker_enum()
        params["sticker"] = sticker
        self.parameters = params
        return super().schema()

    def __init__(self, repo=None, intent_bus=None, unified_query=None):
        self.repo = repo
        self._intent_bus = intent_bus
        self._uq = unified_query

    async def execute(self, **kwargs) -> ToolResult:
        who = kwargs.get("who", "").strip()
        mood_id = kwargs.get("sticker", "").strip()
        emotion = kwargs.get("emotion", "").strip()
        platform = kwargs.get("platform", "wechat").strip()
        account_choice = kwargs.get("account", "self").strip()

        # Capability gate: using the owner's account for a send is a hard
        # permission, not a model choice.  Authority fails closed, so a missing
        # principal denies the send instead of falling back to config ids.
        # N4 收编点 ⑦（G1-13）：能力判定也只有一处 owner
        # （``core/rules/engine.py``；``authoritative_has_capability`` 是转发壳）。
        if account_choice == "owner":
            try:
                from core.terminal.principal import Capability

                allowed = self.authoritative_has_capability(
                    Capability.SEND_AS_OWNER
                )
            except Exception:
                warn("agents/tools/send_sticker.py:403 Exception", err=repr(__import__("sys").exc_info()[1]))
                allowed = False
            if not allowed:
                return ToolResult("", error="当前身份没有使用主人账号发送的权限")

        # N4 收编点 ⑧（G1-13）："能不能发这个表情"的动作前置条件交给引擎
        # （``target`` 限会话/角色、``quota`` 限次数、``action`` 允许/拒绝）。
        # ``off`` 档引擎一律放行，行为与接线前逐字节一致。
        gate = _sticker_rule_gate(self.ctx, who, mood_id, emotion, platform,
                                 account_choice)
        if gate is not None:
            return gate

        if not who:
            # In a group the model often has no name to give: it knows it is
            # answering this group but not what to call it, so it omits the
            # parameter and the send dies here.  Take the current conversation
            # from the context first.
            who = self._current_chat()
        if not who:
            return ToolResult(
                "",
                error="缺少接收者名称。必须传 who：私聊填联系人昵称，"
                      "群聊填群名称（例如 who='示例作业群'）。",
            )
        if not mood_id and not emotion:
            return ToolResult("", error="缺少sticker或emotion，至少需要一个来选择表情包")

        account_id = self._resolve_account_id(platform, account_choice)
        snapshot = getattr(self.ctx, "speech_snapshot", None) if self.ctx else None
        if snapshot is None and self.ctx is not None:
            try:
                from core.speech.turn import snapshot_from_context
                snapshot = snapshot_from_context(
                    self.ctx, platform=platform, chat_id=who,
                    chat_type="group" if (getattr(self.ctx, "extra", {}) or {}).get("is_group") else "private",
                    inbound_text=emotion or mood_id, msg_type="sticker",
                )
            except Exception as exc:
                log.warning("sticker_snapshot_resolve_failed", error=str(exc))
        fallback_relation = _resolve_relation_from_db(who, platform, account_id)
        principal = self.principal
        if principal is not None:
            try:
                from core.terminal.authority import relation_label

                fallback_relation = relation_label(
                    principal, fallback=fallback_relation
                )
            except Exception:
                warn("agents/tools/send_sticker.py:452 Exception", err=repr(__import__("sys").exc_info()[1]))
        relation = _resolve_relation_for_turn(who, fallback_relation, snapshot)

        catalog = _load_sticker_catalog()
        mode = _sticker_candidates_mode()
        request = (mood_id or emotion or "").strip()
        # Exact-match gate: a hallucinated or renamed-away name must fail
        # loudly, right here, with the usable list in the error -- not
        # silently fall through to the sender's guessing resolver.
        # From ``on`` onwards the same words are first read as *meaning*
        # (name / alias / wording); a name that resolves to more than one entry
        # is refused instead of silently taking the first one.
        if mood_id and mood_id not in catalog:
            if mode == "off":
                return ToolResult(
                    "",
                    error=f"表情「{mood_id}」不存在。可用表情: {sorted(catalog.keys())}",
                )
            report = _candidate_report(request, platform=platform)
            chosen, decision = _choose_candidate(report, relation)
            log.info("sticker_candidates", mode=mode, request=request,
                     decision=decision, chosen=chosen or None,
                     candidates=[row.get("emotion") for row in
                                 report["by_name"] + report["by_alias"] + report["by_wording"]])
            if mode != "on":
                return ToolResult(
                    "",
                    error=f"表情「{mood_id}」不存在。可用表情: {sorted(catalog.keys())}",
                )
            if not chosen:
                return ToolResult("", error=_candidate_error_text(request, report, decision))
            mood_id = chosen
            resolved_mood = chosen
        elif not mood_id and request and mode == "on":
            report = _candidate_report(request, platform=platform)
            chosen, decision = _choose_candidate(report, relation)
            log.info("sticker_candidates", mode=mode, request=request,
                     decision=decision, chosen=chosen or None,
                     candidates=[row.get("emotion") for row in
                                 report["by_name"] + report["by_alias"] + report["by_wording"]])
            if decision == "ambiguous":
                return ToolResult("", error=_candidate_error_text(request, report, decision))
            if chosen:
                resolved_mood = chosen
        else:
            resolved_mood = mood_id
        if resolved_mood and resolved_mood not in catalog:
            # The registry changed between the two reads: never index a name
            # this catalog has not seen.  The words still travel as a
            # description, so nothing the model said is lost.
            resolved_mood = ""
        if resolved_mood:
            scope = catalog[resolved_mood].get("scope", [])
            if scope and relation not in scope:
                allowed = [mid for mid, info in catalog.items()
                           if relation in info.get("scope", [])]
                return ToolResult(
                    "",
                    error=f"表情「{resolved_mood}」不适用于{relation}关系；该关系下可用: {allowed}",
                )

        if snapshot is None:
            return ToolResult("", error="当前会话没有可用的说话策略快照，发送已拒绝")
        try:
            from core.bootstrap import get_outbound_gateway
            from core.speech.domain import OutboundDraft
            draft = OutboundDraft(
                request_id=getattr(getattr(self.ctx, "turn_context", None), "request_id", ""),
                kind="sticker",
                target=snapshot.turn.target,
                account_choice=account_choice,
                account_id=account_id,
                who=who,
                mood_id=resolved_mood or mood_id,
                # The model's own words travel with the send: when they are
                # exactly the resolved name they add nothing, otherwise they are
                # the only record of what it was trying to say.
                emotion=emotion or (request if request and request != resolved_mood else ""),
            )
            defer = _should_defer_sticker(self.ctx)
            result = await get_outbound_gateway().send_sticker(
                draft, snapshot, self.ctx, defer=defer,
            )
            return ToolResult(result.output, error=result.error)
        except Exception as exc:
            log.error("sticker_gateway_send_failed", error=str(exc))
            return ToolResult("", error=f"表情包发送失败: {exc}")

    @staticmethod
    def _looks_like_chat_id(value: str) -> bool:
        value = (value or "").strip()
        return bool(value) and (
            value.endswith("@chatroom")
            or value.startswith("wxid_")
            or value.startswith("v1_")
        )

    def _current_chat(self) -> str:
        """The conversation this turn belongs to, if the context carries it.

        Prefer raw chat ids over display names: the sender keys its active-chat
        memory by chat id, and a display-name target would otherwise force a
        fresh contact search before every sticker.
        """
        ctx = self.ctx
        if ctx is None:
            return ""
        try:
            for key in ("chat_id", "source_chat", "who"):
                value = ctx.get(key)
                if isinstance(value, str) and self._looks_like_chat_id(value):
                    return value.strip()
        except Exception:
            warn("agents/tools/send_sticker.py:565 Exception", err=repr(__import__("sys").exc_info()[1]))
        try:
            for key in ("chat_name", "group_name", "chat_id"):
                value = ctx.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except Exception:
            warn("agents/tools/send_sticker.py:572 Exception", err=repr(__import__("sys").exc_info()[1]))
        for key in ("chat_id", "source_chat", "chat_name", "group_name", "who"):
            value = (getattr(ctx, "extra", None) or {}).get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        event = getattr(ctx, "event", None)
        for attr in ("chat_id", "chat_name"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _resolve_account_id(self, platform: str, account_choice: str) -> str:
        account_ctx = getattr(self.ctx, "account_context", None) if self.ctx else None
        if account_ctx is not None and account_choice != "owner":
            return getattr(account_ctx, "account_id", "") or ""
        if self.ctx:
            if account_choice == "owner" and self.ctx.owner_accounts:
                ids = self.ctx.owner_accounts.get(platform, [])
                if ids:
                    return ids[0]
            elif self.ctx.accounts:
                return self.ctx.accounts.get(platform, "")
        try:
            from core.config.settings import config
            if platform == "wechat":
                if account_choice == "owner":
                    ids = config.wechat.get_owner_wxids()
                    return ids[0] if ids else ""
                return config.wechat.yinyue_wxid
        except Exception:
            warn("agents/tools/send_sticker.py:603 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def extract_sticker_intents(text: str, who: str, platform: str = "wechat",
                            account_id: str = "") -> list[dict]:
    intents = []
    pattern = r"\[sticker:([^\]]+)\]"
    for match in re.finditer(pattern, text):
        mood_id = match.group(1).strip()
        intents.append({
            "intent_type": "send_sticker",
            "platform": platform,
            "account_id": account_id,
            "chat_id": who,
            "mood_id": mood_id,
        })
    return intents
