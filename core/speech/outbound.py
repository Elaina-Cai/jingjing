from __future__ import annotations
import time

import asyncio
import re
import dataclasses
from threading import Lock
from typing import Any, Mapping

import structlog

from core.speech.domain import (
    OutboundDraft,
    OutboundResult,
    PolicyAction,
    PolicyDecision,
    SpeechSnapshot,
)
from core.speech.guards import (
    GATE_MODE_ON,
    GuardChain,
    gate_audit_fields,
    run_output_gate_async,
)
from core.speech.store import SpeechStore, get_speech_store
from core.speech import claim_consistency

log = structlog.get_logger()

from core.speech import tone_readings  # noqa: E402  —— 出口读数（R2 拆大文件第一刀）
from core.speech import plan_ingest  # noqa: E402  —— 出站文本的最后一步（R2 合一）
from core.speech import proactive_ledger  # noqa: E402  —— 主动触达的认与记（R2）
from safelog import warn


def _dry_run_send_enabled(chat_id: str = "") -> bool:
    """``YINYUE_FEATURE_DRY_RUN_SEND``：on = 走完整链路但不真发（缺省 off）。

    读不到 flag 时按正常发送处理——dry-run 是演练开关，不能因为配置问题把生产
    发送悄悄停掉。``chat_id`` 命中 ``YINYUE_DRY_RUN_SEND_ALLOW_CHATS`` 时放行。
    """
    try:
        from core import features

        return features.dry_run_send(str(chat_id or ""))
    except Exception:  # noqa: BLE001
        warn("core/speech/outbound.py:43 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def _preview(content: str, limit: int = 200) -> str:
    content = (content or "").replace("\n", " ")
    return content[:limit]


def _text_values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    return tuple(str(item) for item in value if str(item))




# 进程内环形缓冲：本进程最近**真的发成功**的正文（重复指纹的补源）
# ---------------------------------------------------------------------------
# 主源是 send_log（权威、跨进程），但账号库未初始化、多账号归属不明、或某条
# provider 路径没写 send_log 时，主源给不出刚发的那条——而"同进程内我刚发出去
# 什么"是确定知道的。两者取并集，仍**不使用** outbound_audit.content_preview
# （200 字截断列 + 闸门元数据，见 gate_sent_text 的警示）。
#
# 归属：**环形缓冲挂在网关实例上**（不是模块级全局表）。线上只有一个网关
# （``get_outbound_gateway()`` 的模块单例），所以语义仍是"进程内"；而隔离库 /
# 测试各自建网关时天然互不串味——模块级 id-keyed 全局表会因对象地址复用而让
# 不同 store 的历史互相污染（实测踩过）。
# 键只到 (platform, chat_id)：草稿的 account_id 可能为空、也可能是解析后的 wxid，
# 同一会话就该给出同一份历史。


_PROACTIVE_REASON_TEXT = {
    "not_whitelisted": "该会话未列入主动触达白名单",
    "disabled": "该会话的主动触达已关闭",
    "sticker_not_allowed": "该会话不允许主动发表情",
    "quiet_hours": "当前处于该会话的免打扰时段",
    "cooldown": "距离上次主动发送的时间不足冷却时长",
    "hourly_limit": "该会话每小时主动发送次数已达上限",
    "daily_limit": "该会话每天主动发送次数已达上限",
}


def _proactive_reason_text(reason: str) -> str:
    reason = str(reason or "")
    return _PROACTIVE_REASON_TEXT.get(reason, reason or "主动触达策略拒绝")


def build_send_log_extra(platform: str, chat_id: str) -> str:
    """send_log.extra：平台特有审计字段（批次 4，目前只有 QQ 的 group_id）。

    返回 JSON 字符串；无内容返回 ""（不写空 extra）。取不到/异常时也不挡发送。
    """
    platform = str(platform or "")
    if platform != "qq":
        return ""
    try:
        from core.execution.platform_registry import get_extra_cls

        cls = get_extra_cls(platform, "send")
        if cls is None:
            return ""
        from core.execution.qq_provider import parse_chat_id

        kind, target = parse_chat_id(chat_id)
        if kind != "group" or not target:
            return ""
        text = cls(group_id=target).model_dump_json(exclude_none=True)
        return "" if text == "{}" else text
    except Exception:  # noqa: BLE001 - 审计字段尽力而为
        warn("core/speech/outbound.py:106 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


class OutboundGateway:
    """The single outbound policy boundary and transport entry point.

    Phase 3 rule: every text/file/sticker action flows through deterministic
    hard safety, permissions and target checks, writes an audit row, and then
    delegates to the account-scoped Provider / IntentBus.  It does not call an
    LLM to rewrite style.
    """

    def __init__(self, store: SpeechStore):
        self.store = store
        self.guard = GuardChain()
        # 闸门配置按 revision 缓存（见 _gate_config）：数据行一变就重建。
        self._gate_config_revision = ""
        self._gate_config_cache = None
        # @ 目标裁定器（归属加固）：默认走进程内单例；测试/回放可注入自己的
        # ``core.speech.resolver.AddresseeResolver``（比如钉死人名→人 id 的索引）。
        self._addressee_resolver: Any = None

    def _guard_memory_claims(self, content: str, context: Any = None) -> tuple[str, dict]:
        """Apply memory consistency to every gateway-backed visible text path."""
        if context is None:
            return str(content or ""), {}
        try:
            from core.memory.index_scope import bound_index

            snapshot = bound_index(context)
            if snapshot is None:
                return str(content or ""), {}
            entries = snapshot.entries_for(context)
            extra = getattr(context, "extra", None) or {}
            visible = _text_values(extra.get("group_window_event_ids"))
            supported_texts = _text_values(extra.get("memory_claim_supported_texts"))
            return claim_consistency.guard_memory_text(
                str(content or ""),
                entries,
                visible_event_ids=visible,
                supported_texts=supported_texts,
            )
        except Exception as exc:  # noqa: BLE001 - fail open on broken auxiliary metadata
            warn("memory_claim_gate_failed", err=repr(exc)[:160])
            return str(content or ""), {}

    # ------------------------------------------------------------------
    # 重复指纹补源（进程内环形缓冲）
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # policy
    # ------------------------------------------------------------------
    def evaluate(self, draft: OutboundDraft,
                 snapshot: SpeechSnapshot) -> PolicyDecision:
        return self.guard.evaluate(draft, snapshot.policy)

    async def evaluate_async(self, draft: OutboundDraft,
                             snapshot: SpeechSnapshot):
        # Phase 3: outbound is deterministic hard safety + permissions only.
        # No LLM style rewrite here; semantic constraints belong in the prompt
        # or in a deterministic rule.
        return self.evaluate(draft, snapshot)

    def apply(self, draft: OutboundDraft,
              decision: PolicyDecision) -> tuple[OutboundDraft, str]:
        if decision.action == PolicyAction.ALLOW:
            return draft, ""
        if decision.action in (PolicyAction.REWRITE, PolicyAction.REPLACE):
            content = decision.rewritten_content or draft.content
            return dataclasses.replace(draft, content=content), ""
        if decision.action == PolicyAction.REGENERATE:
            # Direct sends cannot ask the model to rewrite; use the prepared
            # fallback content when one exists instead of dropping the send.
            if decision.rewritten_content:
                return dataclasses.replace(
                    draft, content=decision.rewritten_content), ""
            return draft, decision.reason or "该内容被当前会话规则拦截"
        if decision.action == PolicyAction.LOG_ONLY:
            return draft, ""
        return draft, decision.reason or "该内容被当前会话规则拦截"

    def _record_delivery_receipt(self, applied, result, account_id: str = "",
                                 context: Any = None, *, skip: bool = False) -> None:
        """把这次发送落成一行回执（`docs/decisions/出口.md` §5.3）。

        旁注：失败只 log。开关 `DELIVERY_CONTRACT` 不是 on 就一行不写，
        所以默认行为与接线前逐字一致。

        ``skip=True``：这一条是**新出口自己发的**，它已经在
        `core/execution/gui/wechat_send.py::_record` 里写过一行真回执
        （带 `ref=wechat#<local_id>`）。出口再写一行会让「一条消息两行回执」，
        直接把 `duplicate_rate` 从 0 抬到 50%——重复发送率是我们唯一的
        安全量具，不能用自己的重复记账污染它。
        """
        if skip:
            return
        turn_id = ""
        try:
            if context is not None:
                turn_id = str(context.extra.get("turn_id") or "")
        except Exception:  # noqa: BLE001 - 观测字段尽力而为
            warn("core/speech/outbound.py:183 Exception", err=repr(__import__("sys").exc_info()[1]))
            turn_id = ""
        try:
            from core.speech.receipts import record_delivery_receipt

            record_delivery_receipt(
                applied, result, turn_id=turn_id,
                chat_id=str(getattr(applied.target, "chat_id", "") or ""),
            )
        except Exception:  # noqa: BLE001 - 旁注不许挡住已经发出去的消息
            log.warning("delivery_receipt_hook_failed",
                        chat_id=str(applied.who or ""), account=account_id)

    def record(self, draft: OutboundDraft, snapshot: SpeechSnapshot,
               decision: PolicyDecision | None = None,
               status: str = "sent", error: str = "",
               gate: Any = None) -> int | None:
        """Write **one** audit row per send (gate info is folded into it).

        ``gate`` 是 ``core.speech.guards.GateResult``：命中时用闸门处置当
        ``decision``（allow|warn|rewrite|block）、把 `gate:<rule>` 加进
        ``rule_ids_json``、把 `rule/evidence/before_len/after_len/mode/hits` 写进
        ``content_preview``——**不新增表、不新增行**，正是"复用 outbound_audit"。
        """
        decision = decision or PolicyDecision()
        fields = gate_audit_fields(gate) if gate is not None else {}
        rule_ids = list(decision.rule_ids) + list(fields.get("rule_ids") or [])
        preview = _preview(draft.content or draft.filepath)
        if fields.get("content_preview"):
            preview = fields["content_preview"]
        try:
            row = self.store.write_audit(
                request_id=draft.request_id,
                platform=draft.target.platform,
                account_id=draft.account_id or snapshot.turn.target.account_id,
                chat_id=draft.target.chat_id,
                chat_type=draft.target.chat_type,
                kind=draft.kind,
                persona_id=snapshot.persona.persona_id,
                persona_version=snapshot.persona.version,
                policy_revision=snapshot.policy.revision,
                decision=str(fields.get("decision") or decision.action.value),
                rule_ids_json=(
                    __import__("json").dumps(rule_ids, ensure_ascii=False)
                    if rule_ids else None
                ),
                content_hash=self.store.content_hash(draft.content or draft.filepath),
                content_preview=preview,
                status=status,
                error=error or decision.reason,
            )
            return getattr(row, "id", None)
        except Exception:
            warn("core/speech/outbound.py:235 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None

    # ------------------------------------------------------------------
    # output gate (MVP-2)
    # ------------------------------------------------------------------
    def _gate_config(self, config=None):
        """闸门阈值：``policy_rules`` 数据行优先，代码常量兜底（**不重启可改**）。

        ``SpeechStore.output_gate_payloads`` 读数据行 → ``GateConfig.from_payloads``
        合并白名单键（数据行没写的键由代码默认值补齐）。缓存按
        ``SpeechStore.get_revision()``：revision 变了才重建配置——任何 speech 配置
        写入都会 ``bump_revision``（revision 本身 1 秒一查，见 store.get_revision），
        所以**改一行 policy_rules 数据 / bump 一次 revision，下一个判定就生效，
        不用重启进程**；``store.invalidate()`` 亦能立刻失效（运维复位用）。
        读不到/异常 → 代码常量，绝不挡发送。
        """
        if config is not None:
            return config
        from core.speech.guards import GateConfig

        try:
            payloads = getattr(self.store, "output_gate_payloads", None)
            if not callable(payloads):
                return GateConfig()
            revision = ""
            if callable(getattr(self.store, "get_revision", None)):
                try:
                    revision = str(self.store.get_revision())
                except Exception:  # noqa: BLE001 - revision 读不到就每次重读配置
                    warn("core/speech/outbound.py:264 Exception", err=repr(__import__("sys").exc_info()[1]))
                    revision = ""
            if revision and revision == self._gate_config_revision:
                return self._gate_config_cache
            built = GateConfig.from_payloads(payloads())
            if revision:
                self._gate_config_revision = revision
                self._gate_config_cache = built
            return built
        except Exception:  # noqa: BLE001 - 配置读不到就用代码常量，绝不挡发送
            warn("core/speech/outbound.py:273 Exception", err=repr(__import__("sys").exc_info()[1]))
            return GateConfig()

    # ------------------------------------------------------------------
    # M2-4 claim 台账接线（`docs/archive/任务清单_2026-10-06.md:71`；台账本体在 core/context/message_facts.py）
    # ------------------------------------------------------------------
    @staticmethod
    def _gate_person_keys(draft: OutboundDraft,
                          snapshot: SpeechSnapshot) -> tuple[str, ...]:
        """本回合**该被判定的人**（台账 ``person_key`` 槽位的取值来源）。

        口径与台账写侧同源：``person_key`` = **发言人的规范身份 id**
        （``wxid_*`` / QQ 号，见 ``core/context/message_facts.py`` 的
        ``source_actor_id`` 与 ``tests/test_claim_ledger.py`` 的 ``ANAHIDA = "wxid_seed03"``），
        不是昵称。owner 在 :class:`core.speech.domain.ConversationTarget`：路由与
        ``core.speech.turn.snapshot_from_context`` 两条建快照的路都填
        ``turn.target.sender_id``（``sender_name`` 只是显示名，绝不进台账键）。

        **保守方向**：定不出人就交空元组——``evaluate_claims`` 会按
        "没有可判定的人（归属未知，按无支撑处理）"判，宁可降级也不放过。
        """
        candidates = (
            str(getattr(getattr(snapshot, "turn", None), "target", None)
                and getattr(snapshot.turn.target, "sender_id", "") or ""),
            str(getattr(draft, "who", "") or ""),
        )
        out: list[str] = []
        for item in candidates:
            value = str(item or "").strip()
            if value and value not in out:
                out.append(value)
        return tuple(out)

    def _stage_claim_ledger(self, draft: OutboundDraft,
                            snapshot: SpeechSnapshot) -> tuple[Any, ...]:
        """把 claim 台账结论折进闸门 config；返回交给 ``run_output_gate`` 的入参。

        ``(config, mode, chat_type, chat_id)``——台账的 IO **只**发生在这里（唯一
        接缝 :func:`core.speech.guards.claim_ledger_inputs` 内部），``run_output_gate``
        仍是纯函数（结构守卫继续成立）。复读 / 指纹两项语义判定已退役，所以这里
        不再取"她最近说过什么"的窗口。

        ``YINYUE_FEATURE_CLAIM_LEDGER`` 三态（唯一读点在
        ``core.context.message_facts.claim_ledger_mode``）：

        * ``off``（默认）→ 接缝内部第一行就返回传入的 config 与窗口：**不 import
          台账、不查库、不构造台账对象**，``GateConfig.unsupported_claims`` 恒空 →
          闸门行为与接线前**逐字节一致**（回滚位）；
        * ``shadow`` → 判定 + 留痕（台账结论进 findings/审计 ``hits=unfounded_claim``），
          ``run_output_gate`` 的收敛段不改文本、不阻断；
        * ``on`` → 无支撑断言按 ``uncertain_claim_text`` 降级（或按配置拦）。
        """
        from core import features
        from core.speech.guards import claim_ledger_inputs

        config = self._gate_config()
        mode = features.mode(features.OUTPUT_GATE, features.OUTPUT_GATE_SHADOW)
        chat_type = str(getattr(snapshot.turn.target, "chat_type", "") or "")
        chat_id = str(draft.target.chat_id or draft.who or "")
        ledger_config = claim_ledger_inputs(
            str(draft.content or ""),
            chat_id=chat_id,
            person_keys=self._gate_person_keys(draft, snapshot),
            config=config,
        )
        return ledger_config, mode, chat_type, chat_id

    async def _apply_output_gate(
        self, draft: OutboundDraft, snapshot: SpeechSnapshot, context: Any = None,
    ) -> tuple[Any, Any, OutboundDraft]:
        """对待发送文本跑一次输出闸门；返回 ``(gate, block_decision, draft)``。

        三态由 ``core.features.OUTPUT_GATE`` 决定：``off`` 不做事、``shadow`` 只写
        审计与计数（不改文本、不阻断）、``on`` 执行**拦截项**（名单泄漏）的处置。
        **观测项（字数/无证据断言）在 ``on`` 下也只记不改**——主人裁决"闸门只观测"
        + R-O1/R-O2；复读与用语词表改写已退役（2026-10-23），超长回复不再重生成、
        不再截断。

        闸门**不调模型**：重生成已随"字数观测化"退役，``draft.extra`` 里的
        ``output_gate_regenerate`` 回调**不再被读取**（键名保留只为兼容调用方，
        router 仍会传）。

        ``chat_id`` 一路带进闸门：沉默熔断按会话计数（``guards.record_gate_drop``）。

        **M2-4 台账接线**：判断在前（``off`` 一行都不走，护栏与接线前一致），
        台账的读库只在 :meth:`_stage_claim_ledger` 里发生一次；它给出的 config 原样
        交给 ``run_output_gate``——"闸门自己不查库"的结构前提不变。
        """
        if draft.kind not in ("text", "file", "image") or not str(draft.content or ""):
            return None, None, draft
        try:
            from core import features

            mode = features.mode(features.OUTPUT_GATE, features.OUTPUT_GATE_SHADOW)
            if mode == features.OUTPUT_GATE_OFF:
                return None, None, draft
            config, mode, chat_type, chat_id = self._stage_claim_ledger(
                draft, snapshot)
            gate = await run_output_gate_async(
                draft.content,
                chat_type=chat_type,
                chat_id=chat_id,
                config=config,
                mode=mode,
            )
        except Exception as exc:  # noqa: BLE001 - 闸门故障不能让回复发不出去
            log.warning("output_gate_failed", error=str(exc)[:120])
            return None, None, draft

        if gate.mode != GATE_MODE_ON or not gate.enforced:
            return gate, None, draft
        if not gate.allowed:
            return gate, PolicyDecision(
                action=PolicyAction.BLOCK,
                reason=f"输出闸门拦截：{gate.rule or 'output_gate'}",
            ), draft
        applied_text = str(gate.applied_text or "")
        if not applied_text.strip():
            # 空串绝不下发（task-51 A：命中重复的处置是"不说"，不是"发一条空的"）。
            # 走到这里说明闸门判了 enforced 却给不出正文——按拦截处理，宁可不发。
            return gate, PolicyDecision(
                action=PolicyAction.BLOCK,
                reason=f"输出闸门拦截：{gate.rule or 'output_gate'}",
            ), draft
        return gate, None, dataclasses.replace(draft, content=applied_text)

    # ------------------------------------------------------------------
    # @ 目标裁定（归属加固，R-A10 / 2026-10-01 19:30「@错人」）
    # ------------------------------------------------------------------
    def addressee_resolver(self) -> Any:
        """本网关的 @ 目标判定器（受众/目标判定的 owner 在 speech.resolver）。"""
        resolver = self._addressee_resolver
        if resolver is None:
            from core.speech.resolver import addressee_resolver

            resolver = addressee_resolver()
        return resolver

    # ------------------------------------------------------------------
    # 出口读数（observe-only；owner 见各函数 docstring）
    # 函数体已搬到 core/speech/tone_readings.py（R2 拆大文件第一刀）。
    # 这里保留同名方法 + 一行裸语句委托：不返回值 = 不可能拿读数改草稿
    # （结构守卫 tests/test_persona_tone_target.py::test_readout_is_observe_only_not_a_gate）。
    # ------------------------------------------------------------------
    def _observe_human_tone(self, draft: OutboundDraft,
                            snapshot: SpeechSnapshot) -> None:
        """说话质感读数（observe-only）：只审计，不改文本。"""
        tone_readings.observe_human_tone(draft, snapshot)

    def _observe_tone_target(self, draft: OutboundDraft,
                             snapshot: SpeechSnapshot) -> None:
        """语气形态读数（observe-only）：只写审计，不改文本、不拦发送。"""
        tone_readings.observe_tone_target(draft, snapshot)

    def _observe_emotion_layers(self, draft: OutboundDraft,
                                snapshot: SpeechSnapshot) -> None:
        """情绪分层读数（observe-only）：只写审计，不改文本、不拦发送。"""
        tone_readings.observe_emotion_layers(draft, snapshot)

    def _note_self_message(self, draft: OutboundDraft,
                           snapshot: SpeechSnapshot) -> None:
        """自我投影（SELF_MODEL 三态）：off 一行不做，shadow 只审计，on 才落盘。"""
        tone_readings.note_self_message(draft, snapshot)

    # ------------------------------------------------------------------
    # transport helpers
    # ------------------------------------------------------------------
    def _trigger_info(self, context: Any) -> tuple[str, int | None, int | None]:
        if context is not None:
            return (
                getattr(context, "trigger", "unknown"),
                getattr(context, "trigger_msg_id", None),
                getattr(context, "thread_id", None),
            )
        return ("unknown", None, None)

    # ------------------------------------------------------------------
    # 主动触达（怎么认 / 记一笔）——实现搬到 ``core.speech.proactive_ledger``
    # ------------------------------------------------------------------
    # 判据只看入参（draft.extra / 快照 / 上下文），不从「显示名与 wxid 不一致」
    # 推断跨会话；记账失败安静放弃，绝不影响已经发出去的那句话。
    @staticmethod
    def _is_proactive_send(draft: OutboundDraft,
                           snapshot: SpeechSnapshot,
                           context: Any = None) -> bool:
        return proactive_ledger.is_proactive_send(draft, snapshot, context)

    @staticmethod
    def _proactive_target_id(draft: OutboundDraft,
                             snapshot: SpeechSnapshot,
                             account_id: str = "") -> str:
        return proactive_ledger.proactive_target_id(draft, snapshot, account_id)

    @staticmethod
    def _proactive_service():
        return proactive_ledger.proactive_service()

    def _record_proactive_send(self, draft: OutboundDraft,
                               snapshot: SpeechSnapshot,
                               context: Any,
                               account_id: str,
                               *,
                               kind: str = "text",
                               ok: bool = True,
                               error: str = "") -> None:
        proactive_ledger.record_send(
            draft, snapshot, context, account_id,
            kind=kind, ok=ok, error=error,
            content_preview=_preview(draft.content or draft.filepath),
        )

    def _record_action_receipt(self, applied: OutboundDraft, account_id: str = "") -> None:
        """把「她真的说了一句」落成动作流水的一行（PIT-111）。

        为什么在这里：语音这条出口原来只往 DB 记主动触达的配额，从不进流水，
        于是「最近对这个会话说过话吗」在冷却那侧看不见她刚回过的这一句。
        旁注纪律：写失败只记日志，绝不把已经发出去的消息改判成失败。
        """
        try:
            from core.infra import actions as _actions

            target = str(getattr(applied, "who", "") or "") or str(
                getattr(getattr(applied, "target", None), "chat_id", "") or ""
            )
            chat_id = str(getattr(getattr(applied, "target", None), "chat_id", "") or target)
            platform = str(getattr(getattr(applied, "target", None), "platform", "") or "")
            extra = {"via": "speech", "account": account_id}
            if platform and account_id and chat_id:
                extra["scope"] = {
                    "platform": platform,
                    "account_id": str(account_id),
                    "chat_id": chat_id,
                }
            _actions.append_receipt(
                act="say",
                target=target,
                result="sent",
                text=_preview(applied.content or applied.filepath),
                extra=extra,
            )
        except Exception:  # noqa: BLE001 - 旁注不许挡住已经发出去的消息
            warn("outbound_action_receipt_failed",
                 err=repr(__import__("sys").exc_info()[1]))


    def _resolve_account_id(self, draft: OutboundDraft,
                            snapshot: SpeechSnapshot,
                            context: Any = None) -> str:
        if draft.account_id:
            return draft.account_id
        choice = draft.account_choice or "self"
        platform = draft.target.platform
        if context is not None:
            if choice == "owner" and getattr(context, "owner_accounts", None):
                ids = (context.owner_accounts or {}).get(platform, [])
                if ids:
                    return ids[0]
            if choice != "owner" and getattr(context, "accounts", None):
                account_id = (context.accounts or {}).get(platform, "")
                if account_id:
                    return account_id
        try:
            from core.config.settings import config
            if platform == "wechat":
                if choice == "owner":
                    ids = config.wechat.get_owner_wxids()
                    return ids[0] if ids else ""
                return config.wechat.yinyue_wxid or ""
        except Exception:
            warn("core/speech/outbound.py:507 Exception", err=repr(__import__("sys").exc_info()[1]))
        return snapshot.turn.target.account_id or ""

    def _account_repo(self, platform: str, account_id: str):
        try:
            from core.terminal.store_port import get_account_db_manager
            manager = get_account_db_manager()
            if manager and account_id:
                return manager.get_repo(platform, account_id)
        except Exception:
            warn("core/speech/outbound.py:517 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None

    def _write_send_log(self, *, platform: str, chat_id: str, content: str,
                        msg_type: str, source: str, trigger: str,
                        account: str, trigger_msg_id: int | None = None,
                        extra: str = "") -> int:
        from db.models import MsgSource
        repo = self._account_repo(platform, account) or None
        if repo is None or not hasattr(repo, "send_log"):
            return 0
        try:
            return repo.send_log.add(
                platform=platform,
                chat_id=chat_id,
                content=content[:500],
                msg_type=msg_type,
                source=source,
                trigger=trigger,
                account=account,
                trigger_msg_id=trigger_msg_id,
                extra=extra or None,
            )
        except Exception:
            warn("core/speech/outbound.py:541 Exception", err=repr(__import__("sys").exc_info()[1]))
            return 0

    def _mark_send_failed(self, platform: str, account_id: str,
                          send_log_id: int, error: str) -> None:
        repo = self._account_repo(platform, account_id)
        if repo is not None and send_log_id and hasattr(repo, "send_log"):
            try:
                repo.send_log.mark_failed(send_log_id, error)
            except Exception:
                warn("core/speech/outbound.py:550 Exception", err=repr(__import__("sys").exc_info()[1]))

    def _resolve_provider(self, platform: str, account_id: str = ""):
        from core.execution.provider import Platform, WechatProvider
        from core.execution.platform_registry import get_platform

        defn = get_platform(platform)
        if not defn:
            return None
        try:
            target = Platform(defn.key)
        except ValueError:
            warn("core/speech/outbound.py:562 ValueError", err=repr(__import__("sys").exc_info()[1]))
            return None

        def pick(pm):
            if pm is None:
                return None
            if account_id:
                provider = pm.get_by_account(platform, account_id)
                if provider is not None and getattr(provider, "is_connected", False):
                    return provider
            for provider in pm.by_platform(target):
                if getattr(provider, "is_connected", False):
                    return provider
            if account_id:
                return pm.get_by_account(platform, account_id)
            return None

        try:
            from core.bootstrap import get_provider_manager
            provider = pick(get_provider_manager())
            if provider is not None:
                return provider
        except Exception:
            warn("core/speech/outbound.py:584 Exception", err=repr(__import__("sys").exc_info()[1]))

        try:
            from core.config.settings import config
            if target == Platform.WECHAT and config.wechat.send_enabled:
                db_path = ""
                if account_id:
                    ac = config.get_account_config("wechat", account_id)
                    if ac:
                        db_path = ac.db_path
                if not db_path:
                    ac = config.get_account_config("wechat", config.wechat.yinyue_wxid)
                    db_path = ac.db_path if ac else config.wechat.db_path
                return WechatProvider(
                    account_id=account_id,
                    db_path=db_path,
                    send_enabled=True,
                )
        except Exception:
            warn("core/speech/outbound.py:603 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None

    # ------------------------------------------------------------------
    # public send API
    # ------------------------------------------------------------------
    @staticmethod
    def _owner_account_error(draft: OutboundDraft, context: Any) -> str:
        """Hard capability gate for ``account_choice == \"owner\"``.

        Authority is always active and fails closed: a missing or unresolvable
        principal denies the owner-account send instead of delegating to a
        legacy config-id path.
        """
        if (draft.account_choice or "self") != "owner":
            return ""
        try:
            from core.terminal.authority import active_context_has_capability
            from core.terminal.principal import Capability

            allowed = active_context_has_capability(
                context, Capability.SEND_AS_OWNER
            )
        except Exception:
            warn("core/speech/outbound.py:627 Exception", err=repr(__import__("sys").exc_info()[1]))
            return "当前身份没有使用主人账号发送的权限"
        if not allowed:
            return "当前身份没有使用主人账号发送的权限"
        return ""

    @staticmethod
    def _mark_context_sent(context: Any, audit_id: int | None = None) -> None:
        """Let the calling turn know some outbound action already succeeded.

        The proactive trigger runner uses this to avoid sending the agent's
        textual reply twice when the agent already invoked send_message or
        send_sticker itself.
        """
        if context is None:
            return
        extra = getattr(context, "extra", None)
        if not isinstance(extra, dict):
            return
        try:
            extra["_last_sent_ok"] = True
            if audit_id:
                extra["_last_audit_id"] = audit_id
        except Exception:
            warn("core/speech/outbound.py:650 Exception", err=repr(__import__("sys").exc_info()[1]))


    async def send_text(self, draft: OutboundDraft,
                        snapshot: SpeechSnapshot,
                        context: Any = None) -> OutboundResult:
        return await self._send_text_or_file(draft, snapshot, context)

    async def send_file(self, draft: OutboundDraft,
                        snapshot: SpeechSnapshot,
                        context: Any = None) -> OutboundResult:
        return await self._send_text_or_file(draft, snapshot, context)

    async def _send_via_exit_bridge(self, applied: Any, target_id: str,
                                    context: Any, *, turn_id: str = "") -> Any | None:
        """U-14：把这一条交给新出口。返回 ``None`` = 不归桥接管，调用方走旧路。

        同步阻塞（微信真发约 30 秒）→ 扔进线程，别把事件循环按住。
        """
        bridge_started = False
        try:
            from core.speech.legacy_bridge import deliver_via_exit, legacy_via_exit_mode, BridgeResult, unknown_bridge_result

            mode = legacy_via_exit_mode()
            if mode not in ("on", "shadow"):
                return None
            platform = str(getattr(applied.target, "platform", "") or "")
            if mode == "shadow":
                # 影子档：旧路照发，新出口只 dry_run 走一遍闸门，记一行比对日志。
                # 返回值一律 None —— 影子档绝不允许接管传输。
                shadowed = await asyncio.to_thread(
                    deliver_via_exit,
                    platform,
                    str(target_id or ""),
                    str(applied.content or ""),
                    kind=str(getattr(applied, "kind", "") or "say"),
                    filepath=str(getattr(applied, "filepath", "") or ""),
                    turn_id=turn_id,
                    chat_id=str(getattr(applied.target, "chat_id", "") or ""),
                    dry_run=True,
                )
                # structlog 不吃 printf 位置参数（会原样留下 %s），所以先拼好再交。
                shadow_status = shadowed.status or ("sent" if shadowed.outcome.ok else "failed")
                shadow_reason = shadowed.reason or str(getattr(shadowed.outcome, "error", "") or "")
                log.info(
                    f"legacy_exit_shadow platform={platform} target={target_id or ''}"
                    f" kind={getattr(applied, 'kind', '') or 'say'}"
                    f" handled={bool(shadowed.handled)}"
                    f" exit_status={shadow_status} exit_reason={shadow_reason}"
                )
                return None
            bridge_started = True
            handled = await asyncio.to_thread(
                deliver_via_exit,
                platform,
                str(target_id or ""),
                str(applied.content or ""),
                kind=str(getattr(applied, "kind", "") or "say"),
                filepath=str(getattr(applied, "filepath", "") or ""),
                turn_id=turn_id,
                chat_id=str(getattr(applied.target, "chat_id", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 已开始投递不能退回旧路
            log.warning("legacy_bridge_failed platform=%s",
                        getattr(getattr(applied, "target", None), "platform", ""))
            if bridge_started:
                return unknown_bridge_result(kind=str(getattr(applied, "kind", "") or "say"),
                    target=str(target_id or ""), detail=f"{type(exc).__name__}: {exc}").as_send_result()
            return None
        if not isinstance(handled, BridgeResult):
            return unknown_bridge_result(kind=str(getattr(applied, "kind", "") or "say"),
                target=str(target_id or ""), detail="桥接未返回有效结果").as_send_result()
        if not handled.handled:
            return None
        return handled.as_send_result()

    # 下面三件是「出站文本的最后一步」，实现搬到了 ``core.speech.plan_ingest``
    # （2026-10-23 合一：路由侧原来另有两份并行实现，一份还不记账）。
    # 这里只留同名入口，调用点与 ``OutboundGateway._drop_done_claims`` 这类外部取用不变。
    @staticmethod
    def _drop_done_claims(text: str) -> str:
        return plan_ingest.drop_done_claims(text, log=log)

    @staticmethod
    def _consume_plan_block(text: str, context: Any) -> str:
        return plan_ingest.consume_plan_block(text, context, log=log)

    @staticmethod
    def _append_pending_ask(text: str, context: Any) -> str:
        return plan_ingest.append_pending_ask(text, context, log=log)

    def _roster_target_error(self, draft: OutboundDraft) -> str:
        """BR-069：跨人开口之前问一句「这个人在这个房间的名单里吗」。

        三态：``在册`` 放行；``说不准``（没同步过 / 快照过期 / 重名）放行；
        ``不在册`` 只看开关 ``ROSTER_DENY``：关着就留一行痕迹，开着才拦。
        默认关 —— 名单再权威也有「刚入群还没同步」的窗口，一上来就拒发会把她变成哑巴。
        """
        try:
            from core.context import room_members as _rooms
            from core.infra import envflags

            who = str(getattr(draft, "who", "") or "").strip()
            target = getattr(draft, "target", None)
            chat_id = str(getattr(target, "chat_id", "") or "").strip()
            if not who or not chat_id:
                return ""
            verdict, why = _rooms.resolve(chat_id, who)
            if verdict != _rooms.VERDICT_OUT:
                return ""
            try:
                _rooms.note_out(chat_id, who, why)  # 2026-11-02 修 F821：chat 未定义，台账从没写过
            except Exception as exc:  # noqa: BLE001 - 台账写不进去不许影响发送
                from safelog import warn as _warn

                _warn("roster_out_write_failed", error=repr(exc)[:80])
            if not envflags.flag("ROSTER_DENY"):
                log.warning(
                    "send_target_not_in_roster",
                    who=who[:24], chat=chat_id[:24], why=why[:60],
                )
                return ""
            return "not_in_roster:" + why[:60]
        except Exception as exc:  # noqa: BLE001 - 判据坏了也不许把发送带崩
            log.warning("send_target_roster_unchecked", error=repr(exc)[:80])
            return ""

    async def _send_text_or_file(self, draft: OutboundDraft,
                                 snapshot: SpeechSnapshot,
                                 context: Any = None) -> OutboundResult:
        # 正文里若夹着 <plan> 块：先摘掉并消费成「一件事」，再发干净的那句。
        try:
            from dataclasses import replace as _replace

            _clean = self._drop_done_claims(self._consume_plan_block(getattr(draft, "text", ""), context))
            _clean = self._append_pending_ask(_clean, context)
            if _clean != getattr(draft, "text", ""):
                draft = _replace(draft, text=_clean)
        except Exception as exc:  # noqa: BLE001 - 摘块失败照发原文
            log.warning("plan_block_strip_failed error=%s", repr(exc))
        roster_error = self._roster_target_error(draft)
        if roster_error:
            decision = PolicyDecision(
                action=PolicyAction.BLOCK, reason=roster_error
            )
            audit_id = self.record(
                draft, snapshot, decision, status="blocked", error=roster_error
            )
            return OutboundResult(
                False, error=roster_error, decision=decision, audit_id=audit_id
            )
        owner_error = self._owner_account_error(draft, context)
        if owner_error:
            decision = PolicyDecision(
                action=PolicyAction.BLOCK, reason=owner_error
            )
            audit_id = self.record(
                draft, snapshot, decision, status="blocked", error=owner_error
            )
            return OutboundResult(
                False, error=owner_error, decision=decision, audit_id=audit_id
            )
        decision = draft.extra.get("prechecked_decision")
        if decision is None:
            decision = await self.evaluate_async(draft, snapshot)
        applied, error = self.apply(draft, decision)
        if error:
            audit_id = self.record(applied, snapshot, decision,
                                   status="blocked", error=error)
            return OutboundResult(False, error=error, decision=decision,
                                  audit_id=audit_id)

        guarded_text, memory_claim_gate = self._guard_memory_claims(
            applied.content,
            context,
        )
        if memory_claim_gate:
            applied = dataclasses.replace(applied, content=guarded_text)
            try:
                context.extra["_memory_claim_gate"] = memory_claim_gate
            except Exception as exc:  # noqa: BLE001 - audit enrichment only
                log.warning("memory_claim_gate_extra_failed", err=repr(exc)[:120])
            log.info(
                "memory_claim_gate",
                action=memory_claim_gate.get("action"),
                reason=memory_claim_gate.get("reason"),
                chat_id=applied.who or applied.target.chat_id,
            )

        account_id = self._resolve_account_id(applied, snapshot, context)
        applied = dataclasses.replace(applied, account_id=account_id)

        # ------------------------------------------------------------------
        # 输出闸门（MVP-2）：出站文本的最后一道确定性检查。
        # 挂在网关这一层，所以走 provider/IntentBus 的文字都过闸：会话回复
        # （router → SendMessageTool）、主动触达、提醒投递、Web 手动发送。
        # off/shadow 只写审计、不改文本、不阻断；on 才处置（见 core.features）。
        # ------------------------------------------------------------------
        gate, gate_block, applied = await self._apply_output_gate(
            applied, snapshot, context)
        if gate is not None:
            if gate.hit:
                log.info("output_gate", mode=gate.mode, action=gate.action,
                         rule=gate.rule or "-", rules=list(gate.rules),
                         enforced=bool(gate.enforced),
                         before_len=gate.before_len, after_len=gate.after_len,
                         chat_id=applied.who or applied.target.chat_id)
            if context is not None:
                try:
                    context.extra["_output_gate"] = gate
                except Exception:  # noqa: BLE001 - 观测字段尽力而为
                    warn("core/speech/outbound.py:789 Exception", err=repr(__import__("sys").exc_info()[1]))
        if gate_block is not None:
            # DROP 的唯一落地点：audit 写 status=blocked + error=<规则 id>
            # （对齐规格"任何 DROP 必须写 outbound_audit"）。
            audit_id = self.record(applied, snapshot, gate_block,
                                   status="blocked",
                                   error=gate_block.rule or "output_gate",
                                   gate=gate)
            return OutboundResult(False, error=gate_block.reason,
                                  decision=gate_block, audit_id=audit_id)


        # ------------------------------------------------------------------
        # 语气形态读数（observe-only，`core.speech.tone_target`）：挂在发送前、
        # 在 @ 目标裁定**之后**——读到的是真正要发出去的那句话。三态由 persona
        # 数据给（缺省 off 一行都不做）。**只写审计**：不改文本、不拦发送、不进
        # GATE_RULES；调用写成一条语句（返回值被丢弃），这是"它不是判据"的结构证据。
        # ------------------------------------------------------------------
        self._observe_tone_target(applied, snapshot)
        self._observe_emotion_layers(applied, snapshot)
        self._observe_human_tone(applied, snapshot)
        if _dry_run_send_enabled(str(applied.who or applied.target.chat_id or "")):
            dry_target = applied.who or applied.target.chat_id
            audit_id = self.record(applied, snapshot, decision,
                                   status="dry_run", gate=gate)
            log.info(
                "dry_run_send",
                platform=applied.target.platform,
                chat_id=dry_target,
                who=applied.who,
                kind=applied.kind,
                chars=len(applied.content or ""),
                text=(applied.content or "")[:120],
            )
            output = f"dry-run（未实际发送）给 {dry_target}: {(applied.content or '')[:50]}"
            return OutboundResult(True, output=output, decision=decision,
                                  audit_id=audit_id)

        choice = applied.account_choice or "self"
        from db.models import MsgSource
        source = MsgSource.YINYUE_PROXY if choice == "owner" else MsgSource.YINYUE_OWN
        trigger, trigger_msg_id, _ = self._trigger_info(context)
        send_log_id = self._write_send_log(
            platform=applied.target.platform,
            chat_id=applied.who or applied.target.chat_id,
            content=applied.content or applied.filepath,
            msg_type="file" if applied.kind in ("file", "image") else "text",
            source=source,
            trigger=trigger,
            account=account_id,
            trigger_msg_id=trigger_msg_id,
            extra=build_send_log_extra(
                applied.target.platform, applied.who or applied.target.chat_id,
            ),
        )
        _t0 = time.time()
        provider = self._resolve_provider(applied.target.platform, account_id)
        if provider is None:
            msg = f"平台 {applied.target.platform} 的 Provider 未注册或未连接"
            self._mark_send_failed(applied.target.platform, account_id, send_log_id, msg)
            self._record_proactive_send(
                applied, snapshot, context, account_id,
                kind=applied.kind, ok=False, error=msg,
            )
            audit_id = self.record(applied, snapshot, decision,
                                   status="failed", error=msg, gate=gate)
            return OutboundResult(False, error=msg, decision=decision,
                                  audit_id=audit_id)

        target_id = applied.who or applied.target.chat_id
        # ------------------------------------------------------------------
        # U-14：最后一段传输换成新出口（`LEGACY_VIA_EXIT=on` 才换）。
        # 上面十几道闸门已经跑完，这里只回答「用什么把这句话送出去」。
        # `handled=False` = 不归桥接管（平台没适配器 / 没启用 / 种类不支持 /
        # 新出口自己炸了），照旧走 provider —— 新路出问题不许让回复发不出去。
        # ------------------------------------------------------------------
        bridged = await self._send_via_exit_bridge(
            applied, target_id, context,
            turn_id=str(getattr(context, "trace_id", "") or ""),
        )
        if bridged is not None:
            result = bridged
        elif applied.kind in ("file", "image") and applied.filepath:
            result = await provider.send(target_id, applied.content, filepath=applied.filepath)
        else:
            result = await provider.send(target_id, applied.content)

        _t2 = time.time()
        log.info("outbound_send_timing", bridged=bool(bridged), ms_total=int((_t2 - _t0) * 1000),
                 kind=str(getattr(applied, "kind", "")), target=target_id)
        if result.ok:
            if context is not None:
                try:
                    context.extra["_last_send_log_id"] = send_log_id
                except Exception:
                    warn("core/speech/outbound.py:880 Exception", err=repr(__import__("sys").exc_info()[1]))
            self._note_self_message(applied, snapshot)
            self._record_proactive_send(
                applied, snapshot, context, account_id,
                kind=applied.kind, ok=True,
            )
            self._record_action_receipt(applied, account_id)  # PIT-111：她做过什么只有一本流水
            audit_id = self.record(applied, snapshot, decision, status="sent",
                                   gate=gate)
            self._mark_context_sent(context, audit_id)
            self._record_delivery_receipt(
                applied, result, account_id, context,
                skip=bridged is not None,
            )
            output = (f"已发送文件给 {target_id}: {applied.filepath}"
                      if applied.kind in ("file", "image")
                      else f"已发送消息给 {target_id}: {(applied.content or '')[:50]}")
            return OutboundResult(True, output=output, msg_id=result.msg_id,
                                  decision=decision, audit_id=audit_id)

        error_text = result.error or "发送失败"
        self._mark_send_failed(applied.target.platform, account_id, send_log_id, error_text)
        self._record_proactive_send(
            applied, snapshot, context, account_id,
            kind=applied.kind, ok=False, error=error_text,
        )
        audit_id = self.record(applied, snapshot, decision,
                               status="failed", error=error_text, gate=gate)
        return OutboundResult(False, error=error_text, decision=decision,
                              audit_id=audit_id)

    async def send_sticker(self, draft: OutboundDraft,
                           snapshot: SpeechSnapshot,
                           context: Any = None,
                           defer: bool = False) -> OutboundResult:
        roster_error = self._roster_target_error(draft)
        if roster_error:
            decision = PolicyDecision(
                action=PolicyAction.BLOCK, reason=roster_error
            )
            audit_id = self.record(
                draft, snapshot, decision, status="blocked", error=roster_error
            )
            return OutboundResult(
                False, error=roster_error, decision=decision, audit_id=audit_id
            )
        owner_error = self._owner_account_error(draft, context)
        if owner_error:
            decision = PolicyDecision(
                action=PolicyAction.BLOCK, reason=owner_error
            )
            audit_id = self.record(
                draft, snapshot, decision, status="blocked", error=owner_error
            )
            return OutboundResult(
                False, error=owner_error, decision=decision, audit_id=audit_id
            )
        decision = draft.extra.get("prechecked_decision")
        if decision is None:
            decision = await self.evaluate_async(draft, snapshot)
        applied, error = self.apply(draft, decision)
        if error:
            audit_id = self.record(applied, snapshot, decision,
                                   status="blocked", error=error)
            return OutboundResult(False, error=error, decision=decision,
                                  audit_id=audit_id)

        account_id = self._resolve_account_id(applied, snapshot, context)
        applied = dataclasses.replace(applied, account_id=account_id)

        if _dry_run_send_enabled(str(applied.who or applied.target.chat_id or "")):
            audit_id = self.record(applied, snapshot, decision, status="dry_run")
            log.info(
                "dry_run_send",
                platform=applied.target.platform,
                chat_id=applied.who or applied.target.chat_id,
                kind="sticker",
                mood_id=applied.mood_id,
                emotion=applied.emotion,
            )
            return OutboundResult(True, output="dry-run（未实际发送表情包）",
                                  decision=decision, audit_id=audit_id)

        # Non-WeChat platforms may expose a native sticker API; keep it behind
        # the same gateway boundary instead of letting tools call providers.
        try:
            from core.bootstrap import get_provider_manager
            pm = get_provider_manager()
            provider = pm.get_by_account(applied.target.platform, account_id) if pm else None
            if provider is not None and applied.target.platform != "wechat":
                if "sticker" not in getattr(provider, "capabilities", ()):
                    error_text = f"{applied.target.platform} 的 Provider 不支持表情包"
                    self._record_proactive_send(
                        applied, snapshot, context, account_id,
                        kind="sticker", ok=False, error=error_text,
                    )
                    return OutboundResult(False, error=error_text, decision=decision)
                result = await provider.send_sticker(
                    applied.who or applied.target.chat_id,
                    mood_id=applied.mood_id, emotion=applied.emotion,
                )
                if result.ok:
                    self._record_proactive_send(
                        applied, snapshot, context, account_id,
                        kind="sticker", ok=True,
                    )
                    audit_id = self.record(applied, snapshot, decision, status="sent")
                    self._mark_context_sent(context, audit_id)
                    self._record_delivery_receipt(applied, result, account_id, context)
                    return OutboundResult(True, output="已发送表情包",
                                          decision=decision, audit_id=audit_id)
                self._record_proactive_send(
                    applied, snapshot, context, account_id,
                    kind="sticker", ok=False, error=result.error or "",
                )
                audit_id = self.record(applied, snapshot, decision,
                                       status="failed", error=result.error or "")
                return OutboundResult(False, error=result.error or "表情包发送失败",
                                      decision=decision, audit_id=audit_id)
        except Exception:
            warn("core/speech/outbound.py:988 Exception", err=repr(__import__("sys").exc_info()[1]))

        from core.execution.intent_bus import Intent, IntentType, get_intent_bus
        intent_extra: dict[str, Any] = {}
        if self._is_proactive_send(applied, snapshot, context):
            # Re-check after the deferred queue as well as on direct dispatch:
            # the actual send is what consumes the rate budget.
            intent_extra["_proactive"] = True
        intent = Intent(
            intent_type=IntentType.SEND_STICKER,
            platform=applied.target.platform,
            account_id=account_id,
            chat_id=applied.who or applied.target.chat_id,
            content=applied.emotion or applied.mood_id,
            mood_id=applied.mood_id,
            emotion=applied.emotion,
            extra=intent_extra,
        )

        if defer and context is not None:
            try:
                context.extra.setdefault("_pending_stickers", []).append(intent)
            except Exception:
                warn("core/speech/outbound.py:1011 Exception", err=repr(__import__("sys").exc_info()[1]))
            audit_id = self.record(applied, snapshot, decision, status="queued")
            return OutboundResult(True, output="已安排表情包，将在文字回复之后发送",
                                  decision=decision, audit_id=audit_id)

        bus = get_intent_bus()
        result = await bus.dispatch(intent)
        if result.ok:
            self._record_proactive_send(
                applied, snapshot, context, account_id,
                kind="sticker", ok=True,
            )
            audit_id = self.record(applied, snapshot, decision, status="sent")
            self._mark_context_sent(context, audit_id)
            # 表情包也要有回执：文字有、贴纸没有，等于账本只记了一半的"说过什么"。
            self._record_delivery_receipt(applied, result, account_id, context)
            desc = applied.emotion or applied.mood_id
            return OutboundResult(True, output=f"已发送表情包给 {intent.chat_id}: {desc}",
                                  decision=decision, audit_id=audit_id)
        error_text = result.error or "表情包发送失败"
        self._record_proactive_send(
            applied, snapshot, context, account_id,
            kind="sticker", ok=False, error=error_text,
        )
        audit_id = self.record(applied, snapshot, decision,
                               status="failed", error=error_text)
        return OutboundResult(False, error=error_text, decision=decision,
                              audit_id=audit_id)

    async def dispatch_pending_sticker(self, intent: Any,
                                       snapshot: SpeechSnapshot,
                                       context: Any = None) -> Any:
        """Dispatch a sticker intent queued earlier, after the text reply.

        A queued proactive sticker is re-checked here: queueing must not bypass
        quiet hours, cooldown, or hourly/daily caps, and only the real dispatch
        writes the send-log row consumed by those limits.
        """
        from core.execution.intent_bus import IntentResult, get_intent_bus

        intent_extra = getattr(intent, "extra", None) or {}
        proactive = bool(intent_extra.get("_proactive"))
        target = getattr(snapshot, "turn", None)
        target = getattr(target, "target", None)
        account_id = str(getattr(intent, "account_id", "") or "")
        draft = OutboundDraft(
            request_id=str(intent_extra.get("request_id", "")),
            kind="sticker",
            target=target or getattr(snapshot, "turn", None).target,
            account_id=account_id,
            who=getattr(intent, "chat_id", ""),
            mood_id=getattr(intent, "mood_id", ""),
            emotion=getattr(intent, "emotion", ""),
            extra={"_proactive": True} if proactive else {},
        )

        if proactive:
            service = self._proactive_service()
            if service is None:
                reason = "主动触达策略不可用，已拒绝发送"
            else:
                decision = service.evaluate(
                    platform=str(getattr(intent, "platform", "")
                                 or getattr(target, "platform", "")),
                    bot_account_id=account_id
                    or str(getattr(target, "account_id", "") or ""),
                    chat_id=str(getattr(intent, "chat_id", "") or ""),
                    chat_type=str(getattr(target, "chat_type", "") or "private"),
                    kind="sticker",
                )
                reason = (
                    "" if decision.allowed
                    else _proactive_reason_text(decision.reason)
                )
            if reason:
                block = PolicyDecision(
                    action=PolicyAction.BLOCK, reason=reason,
                )
                self.record(
                    draft, snapshot, block, status="blocked", error=reason,
                )
                self._record_proactive_send(
                    draft, snapshot, context, account_id,
                    kind="sticker", ok=False, error=reason,
                )
                return IntentResult(ok=False, intent=intent, error=reason)

        if _dry_run_send_enabled(str(getattr(intent, "chat_id", "") or "")):
            self.record(draft, snapshot, status="dry_run")
            log.info(
                "dry_run_send",
                platform=str(getattr(intent, "platform", "") or getattr(target, "platform", "")),
                chat_id=str(getattr(intent, "chat_id", "") or ""),
                kind="sticker",
                mood_id=getattr(intent, "mood_id", ""),
                emotion=getattr(intent, "emotion", ""),
            )
            return IntentResult(ok=True, intent=intent, msg_id="dry-run")

        bus = get_intent_bus()
        result = await bus.dispatch(intent)
        error = "" if result.ok else str(getattr(result, "error", "") or "")
        try:
            self.record(
                draft,
                snapshot,
                status="sent" if result.ok else "failed",
                error=error,
            )
        except Exception:
            warn("core/speech/outbound.py:1121 Exception", err=repr(__import__("sys").exc_info()[1]))
        if proactive:
            self._record_proactive_send(
                draft, snapshot, context, account_id,
                kind="sticker", ok=bool(result.ok), error=error,
            )
        if result.ok:
            self._record_delivery_receipt(draft, result, account_id, context)
            self._mark_context_sent(context)
        return result


# ---------------------------------------------------------------------------
# 直接发送入口的输出闸门（键鼠线 loop/desk.py；2026-11-16 生产切换）
# 网关（OutboundGateway）只覆盖 gateway 路径；desk 直接调 wx_sender，
# 曾完全绕过 guards。下面两个函数给那条在役发送线补同一套判定 + 审计。
# ---------------------------------------------------------------------------


def gate_direct_text(
    text: str,
    *,
    chat_type: str = "private",
    chat_id: str = "",
) -> dict:
    """同步跑一次输出闸门，返回 {text, mode, blocked, rule, action, gate}。

    ``off`` 原样返回；``shadow`` 只带回判定（不改文本）；``on`` 命中拦截项时
    ``blocked=True``（调用方必须不发），否则用闸门给出的正文。
    """
    from core import features

    payload = {
        "text": str(text or ""),
        "mode": "off",
        "blocked": False,
        "rule": "",
        "action": "allow",
        "gate": None,
    }
    try:
        mode = features.mode(features.OUTPUT_GATE, features.OUTPUT_GATE_SHADOW)
    except Exception as exc:  # noqa: BLE001 - 读不到旗标按原稿发，不能因此停发
        warn("output_gate_flag_failed", err=repr(exc))
        return payload
    payload["mode"] = mode
    if mode == features.OUTPUT_GATE_OFF:
        return payload
    try:
        from core.speech.guards import GATE_MODE_ON, apply_gate_text, run_output_gate

        result = run_output_gate(
            payload["text"], chat_type=chat_type or "private",
            chat_id=str(chat_id or ""), mode=mode,
        )
    except Exception as exc:  # noqa: BLE001 - 闸门故障不能让回复发不出去
        warn("output_gate_run_failed", err=repr(exc))
        return payload
    payload["gate"] = result
    payload["action"] = str(getattr(result, "audit_action", "") or "allow")
    if result.mode != GATE_MODE_ON or not result.enforced:
        return payload
    applied = str(apply_gate_text(payload["text"], result) or "")
    if not result.allowed or not applied.strip():
        payload["blocked"] = True
        payload["rule"] = str(getattr(result, "rule", "") or "output_gate")
        return payload
    payload["text"] = applied
    return payload


def record_direct_gate_audit(
    gate: dict,
    *,
    platform: str = "",
    account_id: str = "",
    chat_id: str = "",
    chat_type: str = "private",
    kind: str = "text",
    status: str = "sent",
    error: str = "",
    content: str = "",
    request_id: str = "",
) -> int | None:
    """把一次直接发送的闸门判定写进 ``outbound_audit``（失败只告警，不影响发送）。"""
    result = (gate or {}).get("gate")
    if result is None:
        return None
    try:
        import hashlib
        import json

        from sqlalchemy.orm import Session

        from core.speech.guards import gate_audit_fields
        from core.terminal.store_port import get_meta_repo
        from db.meta_models import OutboundAudit

        fields = gate_audit_fields(result, chat_type=chat_type, chat_id=chat_id)
        rules = fields.get("rule_ids") or []
        with Session(get_meta_repo().engine) as sess:
            row = OutboundAudit(
                request_id=str(request_id or ""),
                platform=str(platform or ""),
                account_id=str(account_id or ""),
                chat_id=str(chat_id or ""),
                chat_type=str(chat_type or ""),
                kind=str(kind or "text"),
                decision=str(fields.get("decision") or "allow"),
                rule_ids_json=json.dumps(rules, ensure_ascii=False) if rules else None,
                content_hash=hashlib.sha256(str(content or "").encode("utf-8")).hexdigest(),
                content_preview=str(fields.get("content_preview") or "")[:400] or None,
                status=str(status or ""),
                error=str(error or "")[:200] or None,
            )
            sess.add(row)
            sess.commit()
            return int(getattr(row, "id", 0) or 0)
    except Exception as exc:  # noqa: BLE001 - 审计写不进去不能挡住已经发出的消息
        warn("output_gate_audit_failed", err=repr(exc))
        return None


_GATEWAY: OutboundGateway | None = None


def get_outbound_gateway(store: SpeechStore | None = None) -> OutboundGateway:
    global _GATEWAY
    if store is not None:
        _GATEWAY = OutboundGateway(store)
        return _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = OutboundGateway(get_speech_store())
    return _GATEWAY
