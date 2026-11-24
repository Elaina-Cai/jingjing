"""L7 N5 参数收集状态机（``GATHERING→VALIDATING→CONFIRMING→EXECUTING``）。

权威依据
--------
* ``docs/decisions/架构评估.md`` §3.5（``:269-286``，N5 参数收集状态机）——**字段级契约以它为准**：
  ``SlotFill{state, slots, asked_batch, confirm_message_ref, confirm_quoted, confirm_word,
  nudges, last_nudge_at, created_at, expires_at}`` /
  ``Slot{name, expected_type, expected_example, value, valid, invalid_reason, ttl_seconds,
  closed}`` / ``SlotFillInput{chat_id, requester_person_id, action_id, spec_kind, incoming,
  open_slots}``；条款 R-P1..R-P8 见同文 §2.10（``:165-172``）。
* ``docs/全链路架构审计与目标架构.md`` §2.2 L7（``:349``）+ 乱点 #22（``:358``）：本层原先
  **完全不存在**，只有一次「我准备这样做：…可以继续吗？」（不记 pending 状态、无引用校验）。
* 本期**收编对象**：``agents/conversational.py`` 的 ``_DESTRUCTIVE_TOOLS`` 确认分支
  （现 ``:2183-2192``；文档写的是旧口径行号 ``:1708-1717``）——只收编那一段，主循环不动。

九条条款落点（R-P1..R-P8）
--------------------------
* **R-P1 首问全量**：`:func:`SlotFillMachine.advance`` 在 ``GATHERING`` 里一次问**全部**
  缺的必需槽位，硬上限 :data:`MAX_ASK_BATCH` = 3（超出只取前 3 个并留痕），同一条消息里
  **复述已知信息**（:func:`_ask_text` 的"我记着的是…"一句）。
* **R-P2 收齐才进 VALIDATING**：缺参**不前进**（:meth:`SlotFillMachine._complete` 为假就
  留在 ``GATHERING``）。
* **R-P3 逐参数校验 / 一个不匹配就退回**：:meth:`SlotFillMachine._validate` 逐槽位判，
  只要一个 ``valid=False`` 就 ``VALIDATING→GATHERING``，且**只问那一个**
  （``invalid_reason`` 渲染成"我需要 X 格式，例如…"，R-P6）。
* **R-P4 引用 + 确认才算确认**：参数齐 → ``CONFIRMING`` + 一条列出全部参数的待确认消息 →
  记 :attr:`SlotFill.confirm_message_ref` / :attr:`SlotFill.confirm_token` →
  ``confirm_quoted and confirm_word`` 才进 ``EXECUTING``；**无引用证据一律不算确认**，
  原状态重新问一句（:func:`reference_evidence`）。
* **R-P5 自然语言纠正**：``CONFIRMING`` 里收到已闭合槽位的新值 → 回 ``VALIDATING`` 重判。
* **R-P6 节奏**：``nudges`` 计数 + 每次催问都带 ``expected_type`` / ``expected_example``
  渲染的**明确具体**提示（本模块**零 LLM 调用**：全是模板渲染）。
* **R-P7 超时**：过 ``expires_at`` → ``ABORTED``，不再跟踪；**归档回调不在本期**
  （见文末「本期边界」第 5 条）。
* **R-P8 多槽位匹配**：四信号 = ①引用/编号（最强）②时间窗（``expires_at``）③同线程
  （``thread_id``）④内容类型匹配 ``expected_type``；同一会话里多个未闭合槽位且**信号拉不开**
  → **不猜**，直接问「你是在回 X 还是 Y？」（:meth:`SlotFillMachine._ambiguity_text`）。

字段读法（契约名 ⇄ 任务书里的口语名）
------------------------------------
契约的 ``SlotFill.confirm_message_ref`` 就是任务书说的 ``confirm_ref``：``confirm_ref``
是**属性别名**，优先返回"我们自己那条待确认消息的 id"（发送侧回填），还没有就返回
``confirm_token``（印在待确认文案里的短编号）。``SlotFill.pending_slots`` 是 ``slots`` 里
未闭合槽位的**派生视图**（:attr:`SlotFill.pending_slots`），``SlotFill.ask_text`` 是最近一次
问话的原文（留痕/对账用，不是第二份状态：状态只有 ``state`` + ``slots`` + ``transitions``）。

引用判定**复用 owner**：``core.conversation.addressing`` 的 ``quote_of`` /
``strip_quote_block``（``docs/decisions/架构评估.md:278`` 要求复用 ``resolve_attribution`` 的
``quoted.resolvable`` 口径，**不许新写引用正则**——本模块一行引用正则都没有）。
「无引用」的兜底是**显式设计**的：待确认文案里印一个自己生成的短编号
（``confirm_token``，形如 ``#A1B2C3``），正文里原样带回它即视为"指回同一条"
（契约 ``R-T0a`` 已承认现实引用稀疏，所以这条兜底必须存在）。

台账（复用，不新建表）
----------------------
状态与槽位落 :func:`core.tasks.store.record_slot_fill` 写的 ``work_items`` 行
（``kind="act"`` / ``created_by="slot_fill"`` / ``spec_json.slot_fill`` = 本状态机的
``to_dict()``），执行事件落 ``work_item_runs``（复用 ``store.record_run_started``）。
**一行 fill = 一行 work_items**，``work_item_id = "slotfill:<fill_id>"`` 是**确定性身份**，
所以跨轮 upsert 不会长第二行（幂等键 = 同一个值）。

flag
----
``YINYUE_FEATURE_SLOT_FILL`` 三态（:func:`core.features.mode`，**唯一读点** :func:`slot_fill_mode`）：

* ``off``    ← **默认**：一行都不走（连本模块都不会被 import），与接线前**逐字节一致**；
* ``shadow`` → 判定 + 落库 + 留痕，但**对外文本一个字不改**（调用方仍拿旧文案），
               用来先跑读数（缺槽位率 / 一次问齐率 / 引用命中率）；
* ``on``     → 状态机文本对外生效：缺槽位就问、补齐就确认、**引用对得上才执行**。

回滚 = ``YINYUE_FEATURE_SLOT_FILL=off``（每次读环境，不用重启）。

本期边界（诚实清单）
--------------------
1. **槽位定义是内置表**（:data:`ACTION_SLOT_SPECS`，只覆盖 ``_DESTRUCTIVE_TOOLS`` 三个工具）。
   R-P1 说的"槽位定义由 UI/记忆侧给"没有落地：``SlotFillInput.open_slots`` 已经留了入口，
   认得出来的工具才接管，认不出来 → :func:`gate_plan_confirmation` 返回 ``None``
   （= 调用方走旧文案，不猜参数）。
2. **kind 级 schema 校验（B5）不是本期的**：§3.5 把它列为硬前置，这里只做**类型/必填**级
   校验（``expected_type`` + 空值 + 可选的调用方 parser），不假装做了 B5。
3. **"执行"的边界**：本模块只把状态推到 ``EXECUTING`` 并把 ``work_item_run_started`` 落库，
   真正的工具执行仍在 ``agents/conversational.py`` 的既有循环里（主循环一行不改）。
4. ``EXECUTING→DONE`` 由 :meth:`SlotFillMachine.complete` 显式收口；**生产接线还没有人调它**
   ——§5.5 的 ``claim_owner`` / ``claimed_at`` / ``lease_deadline`` 就是为这种"等回执"留的
   （``core/tasks/store.py`` 的自述：A1-d 执行回执接线不在 P1 范围内）。**待办**：A1-d 落地时
   在交付回执处调 ``complete(outcome=...)``。
5. **R-P7 的长期记忆归档**（五字段 + 衰减）不在本期：这里只把状态推到 ``ABORTED`` 并留下
   ``archive`` 留痕字典，记忆写入仍是 ``core/quality/memory_sinking.py`` 的活。
6. **进程内回退缓存**（:data:`_PENDING`）只在**台账不可用**时读（LEDGER 关 / 取不到 meta
   engine）——台账可用时它一次都不参与判定，所以"同一个 fill 两个说法"不成立；两条路
   由 :func:`_load_or_none` 的 ``authoritative`` 一处切换。
7. **执行登记走 store 的包装函数**（``record_slot_fill_run`` / ``finish_slot_fill_run``）：
   ``tests/test_work_item_p1.py::test_chat_paths_create_no_rows`` 静态锁住
   ``record_*(`` 的**调用点**只剩「创建 + 投递」两处，所以 N5 侧不许出现第三处调用点。
   那两个包装是**同一个实现**的转调（模块级别名），不是第二套 run 逻辑；**若 Lead 认为
   N5 应当进那份白名单，改的是那个守卫的期望集合**（不在本子代理的写入域内）。

状态之外还留了两条痕（都在 ``spec_json.slot_fill`` 里）：``transitions`` = **状态迁移**
（可 :func:`replay` 回放），``notes`` = **判了但状态没动**的事实（引用对不上 / 歧义 /
纠正 / 重复确认）——R-P4 的"拒绝"必须查得到，否则台账里看起来像什么都没发生。

Run the checks directly::

    python tests/test_slot_fill.py
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from core import features
from core.conversation.addressing import quote_of, strip_quote_block
from core.tasks import store as task_store
from safelog import warn

log = structlog.get_logger()

# --------------------------------------------------------------------- 状态集
#: 契约 §3.5 的六个状态（``Literal`` 的口径，这里用元组 + 模块常量暴露）。
GATHERING = "GATHERING"
VALIDATING = "VALIDATING"
CONFIRMING = "CONFIRMING"
EXECUTING = "EXECUTING"
DONE = "DONE"
ABORTED = "ABORTED"
STATES: tuple[str, ...] = (GATHERING, VALIDATING, CONFIRMING, EXECUTING, DONE, ABORTED)

#: R-P1 的硬上限：**一次最多问 3 项**（`docs/decisions/架构评估.md:165`「上限 3 是硬值」）。
MAX_ASK_BATCH = 3
#: 槽位默认 TTL（秒）与整条 fill 的默认存活窗口：R-P8「槽位带 TTL」/ R-P7「超时不再跟踪」。
DEFAULT_SLOT_TTL_SECONDS = 900
#: 状态 → ``work_items.status``（§5.5 的状态集，见 ``core.tasks.store``）。
STATE_TO_STATUS: dict[str, str] = {
    GATHERING: "waiting_user",
    VALIDATING: "waiting_user",
    CONFIRMING: "waiting_user",
    EXECUTING: "running",
    DONE: "done",
    ABORTED: "cancelled",
}
#: 台账里的来源标签（``work_items.created_by``）：一眼能分清"这行不是提醒投影"。
CREATED_BY = "slot_fill"
#: 执行事件的 claim_owner（§5.5）。
CLAIM_OWNER = "slot_fill"
#: 验收判据：本条与 §4.3 的投递判据不同——"动作"的完成判据是**引用对上的确认**
#: （唯一常量定义在台账侧 ``core.tasks.store.ACCEPTANCE_SLOT_FILL``，这里只别名读）。
ACCEPTANCE_CONFIRMED = task_store.ACCEPTANCE_SLOT_FILL
#: **回执的"成事"取值**（A1-d）：只有它算"办好了"。台账侧 ``RUN_OUTCOMES`` 的另外三个
#: （``silent`` / ``failed`` / ``blocked``）都不是完成——计划层不变式读的就是这个集合。
COMPLETED_OUTCOMES: tuple[str, ...] = ("sent",)
#: **承诺型工具**（"要替主人做事"的那些，= ``agents/conversational.py:46 _DESTRUCTIVE_TOOLS``
#: 与 :data:`ACTION_SLOT_SPECS` 的键集，守卫 ``tests/test_exec_receipt.py`` 钉住三者相等）。
#: 计划里出现这些工具的步骤 = **承诺型步骤**：只有在拿到执行回执之后才允许被当作已完成
#: （`docs/decisions/架构评估.md:233` R-C5「不许静默放弃、不许假承诺」；`docs/archive/任务清单_2026-10-06.md:43` A1-d）。
#: 集合**写在代码里、不落数据行**（同 §3.4 的硬禁区口径）：放宽它 = 放宽"答应了必须有回执"。
PROMISE_TOOLS: frozenset[str] = frozenset({
    "create_reminder", "notify_user", "send_message",
})
#: 回执 blob 的 schema 版本（``work_items.spec_json.slot_fill.receipt``）。
RECEIPT_SCHEMA_VERSION = 1

#: ``confirm_word`` 的词表（R-P4 的第二个布尔量）。**只做整句等值 + 短句包含**，
#: 并且否词优先（"先不用" / "别" / "取消" 一律不算确认）——这不是引用正则，
#: 引用判定一律走 owner（``quote_of``）。
CONFIRM_WORDS: tuple[str, ...] = (
    "确认", "确定", "可以", "好的", "好", "行", "嗯", "对", "是", "同意",
    "没问题", "继续", "来吧", "干吧", "就这么办", "ok", "okay", "yes", "嗯嗯", "行吧",
)
#: 否词：命中即**不算确认**（先判否、再判是）。
NEGATION_WORDS: tuple[str, ...] = (
    "不", "别", "等等", "等一下", "先不", "取消", "算了", "不用",
)
#: 确认编号的前缀（兜底引用的显式设计，见模块 docstring）。
TOKEN_PREFIX = "#"

#: ``_DESTRUCTIVE_TOOLS``（``agents/conversational.py:46``）的三个工具的槽位定义。
#: 别的毁灭性工具**不接管**（返回 ``None`` = 走旧文案），不猜它的参数。
ACTION_SLOT_SPECS: dict[str, tuple["SlotSpec", ...]] = {}


# ------------------------------------------------------------------- 数据契约
@dataclass(frozen=True)
class SlotSpec:
    """一个槽位的**定义**（"要问什么"），不含任何运行时值。"""

    name: str
    expected_type: str
    expected_example: str
    aliases: tuple[str, ...] = ()
    required: bool = True
    ttl_seconds: int = DEFAULT_SLOT_TTL_SECONDS


@dataclass
class Slot:
    """契约 §3.5 的 ``Slot``（``value`` / ``valid`` / ``invalid_reason`` 是运行态）。"""

    name: str
    expected_type: str = "text"
    expected_example: str = ""
    required: bool = True
    value: Any = None
    valid: bool = False
    invalid_reason: str = ""
    ttl_seconds: int = DEFAULT_SLOT_TTL_SECONDS
    closed: bool = False
    # ---- 扩展（契约的 "…" 允许的留痕字段：可回放、可对账）----
    aliases: tuple[str, ...] = ()
    source: str = ""          # 这个值从哪来：plan / user / correction
    evidence: str = ""        # 值来自哪条消息（message_ref）或哪个计划字段

    def is_filled(self) -> bool:
        return self.value is not None and str(self.value).strip() != ""

    def is_open(self) -> bool:
        """还没闭合（= 还在等对方给值 / 刚被判无效）。"""
        return not self.closed

    def hint(self) -> str:
        """R-P6 的"明确具体"提示：``我需要 X 格式，例如…``。"""
        kind = self.expected_type or "文本"
        if self.expected_example:
            return f"我需要「{self.name}」（{kind}），例如「{self.expected_example}」"
        return f"我需要「{self.name}」（{kind}）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expected_type": self.expected_type,
            "expected_example": self.expected_example,
            "required": bool(self.required),
            "value": self.value,
            "valid": bool(self.valid),
            "invalid_reason": self.invalid_reason,
            "ttl_seconds": int(self.ttl_seconds),
            "closed": bool(self.closed),
            "aliases": list(self.aliases),
            "source": self.source,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Slot":
        return cls(
            name=str(data.get("name") or ""),
            expected_type=str(data.get("expected_type") or "text"),
            expected_example=str(data.get("expected_example") or ""),
            required=bool(data.get("required", True)),
            value=data.get("value"),
            valid=bool(data.get("valid", False)),
            invalid_reason=str(data.get("invalid_reason") or ""),
            ttl_seconds=int(data.get("ttl_seconds") or DEFAULT_SLOT_TTL_SECONDS),
            closed=bool(data.get("closed", False)),
            aliases=tuple(str(a) for a in (data.get("aliases") or ())),
            source=str(data.get("source") or ""),
            evidence=str(data.get("evidence") or ""),
        )


@dataclass
class Transition:
    """一次显式状态迁移（**可回放的留痕**：``from`` / ``to`` / 为什么 / 什么证据 / 何时）。"""

    from_state: str
    to_state: str
    reason: str
    at: str
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_state,
            "to": self.to_state,
            "reason": self.reason,
            "at": self.at,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Transition":
        return cls(
            from_state=str(data.get("from") or ""),
            to_state=str(data.get("to") or ""),
            reason=str(data.get("reason") or ""),
            at=str(data.get("at") or ""),
            evidence=str(data.get("evidence") or ""),
        )


@dataclass
class SlotFillInput:
    """契约 §3.5 的输入面：这一轮是谁、要做什么、对方说了什么、已知哪些槽位。"""

    chat_id: str = ""
    requester_person_id: str = ""
    action_id: str = ""
    spec_kind: str = "act"
    incoming: str = ""
    open_slots: Sequence[Slot] = ()
    thread_id: str = ""
    goal: str = ""
    ttl_seconds: int = DEFAULT_SLOT_TTL_SECONDS


@dataclass
class Incoming:
    """这一轮对方说的话 + 归属侧已经判定出的引用事实（**引用判定不在这里做**）。"""

    text: str = ""
    message_ref: str = ""
    quoted_msg_id: str = ""
    quoted_resolvable: bool = False
    quoted_present: bool = False
    quoted_author: str = ""
    thread_id: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)
    at: str = ""


@dataclass
class SlotFill:
    """契约 §3.5 的 ``SlotFill``（字段名逐条对齐；扩展字段都写在"扩展"一节）。"""

    fill_id: str = ""
    chat_id: str = ""
    requester_person_id: str = ""
    action_id: str = ""
    spec_kind: str = "act"
    state: str = GATHERING
    slots: list[Slot] = field(default_factory=list)
    asked_batch: list[str] = field(default_factory=list)
    confirm_message_ref: str = ""
    confirm_quoted: bool = False
    confirm_word: bool = False
    nudges: int = 0
    last_nudge_at: str = ""
    created_at: str = ""
    expires_at: str = ""
    # ---- 扩展 ----
    schema_version: int = 1
    goal: str = ""
    thread_id: str = ""
    ask_text: str = ""
    confirm_token: str = ""
    transitions: list[Transition] = field(default_factory=list)
    #: **非状态迁移**的判定留痕（引用对不上 / 歧义 / 纠正…）：``transitions`` 只记状态变化，
    #: 这些"判了但状态没动"的事实另存一处，否则 R-P4 的拒绝在台账里看不见。
    notes: list[dict[str, Any]] = field(default_factory=list)
    executed: bool = False
    executed_at: str = ""
    execution_ref: str = ""
    work_item_id: str = ""
    archive: dict[str, Any] = field(default_factory=dict)
    #: **A1-d 执行回执**（``EXECUTING`` 之后的真实结果）。空 dict = 还没有回执。
    #: 键：``outcome`` / ``ok`` / ``reason`` / ``duration_ms`` / ``external_msg_id`` /
    #: ``run_id`` / ``trace_ref`` / ``at`` / ``steps``（逐步骤明细）。
    #: **只有它是"办好了"的证据**——``executed=True`` 只表示"确认对上了、执行已发起"
    #: （见模块 docstring「执行回执」一节）。
    receipt: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- 别名读法
    @property
    def pending_slots(self) -> tuple[str, ...]:
        """还没闭合的槽位名（= "还差什么"）。纯派生，不另存一份状态。"""
        return tuple(slot.name for slot in self.slots if slot.is_open())

    @property
    def confirm_ref(self) -> str:
        """**对外承诺的"待确认事项"身份**：已知自己那条消息 id 就用它，否则用短编号。"""
        return self.confirm_message_ref or self.confirm_token

    def slot(self, name: str) -> Slot | None:
        for item in self.slots:
            if item.name == name:
                return item
        return None

    def missing(self) -> list[Slot]:
        """必需但还没值的槽位（R-P1 要问的就是它们）。"""
        return [s for s in self.slots if s.required and not s.is_filled()]

    def is_expired(self, now: datetime) -> bool:
        stamp = _parse_iso(self.expires_at)
        return stamp is not None and now >= stamp

    def has_receipt(self) -> bool:
        """**有没有回执**（A1-d 的唯一判据）：空 dict = 没有。

        ``executed`` 说的是"执行已发起"，``has_receipt()`` 说的才是"执行有结果"。
        计划层不变式（:func:`plan_completion`）读的是后者。
        """
        return bool(self.receipt) and bool(str(self.receipt.get("outcome") or ""))

    def receipt_ok(self) -> bool:
        """回执是不是"成事"的那一种（``ok`` 且 ``outcome`` 在完成集里）。"""
        return bool(
            self.has_receipt()
            and self.receipt.get("ok")
            and str(self.receipt.get("outcome")) in COMPLETED_OUTCOMES
        )

    def work_item_status(self) -> str:
        """``work_items.status`` 的取值（状态机是状态 owner，翻译只此一处）。

        ``ABORTED`` 有两种成因，不能混成一个词：**执行失败** → ``failed``（有失败回执
        与原因，是终态失败）；**过期 / 用户放弃** → ``cancelled``（R-P7"不再跟踪"）。
        把失败写成 ``cancelled`` 会让人以为"是对方取消的"，那是把失败静默掉。
        """
        if self.state == ABORTED and str(self.receipt.get("outcome") or "") == "failed":
            return "failed"
        return STATE_TO_STATUS.get(self.state, "waiting_user")

    # ------------------------------------------------------------- 序列化
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fill_id": self.fill_id,
            "chat_id": self.chat_id,
            "requester_person_id": self.requester_person_id,
            "action_id": self.action_id,
            "spec_kind": self.spec_kind,
            "state": self.state,
            "slots": [slot.to_dict() for slot in self.slots],
            "asked_batch": list(self.asked_batch),
            "confirm_message_ref": self.confirm_message_ref,
            "confirm_token": self.confirm_token,
            "confirm_ref": self.confirm_ref,
            "confirm_quoted": bool(self.confirm_quoted),
            "confirm_word": bool(self.confirm_word),
            "nudges": int(self.nudges),
            "last_nudge_at": self.last_nudge_at,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "goal": self.goal,
            "thread_id": self.thread_id,
            "ask_text": self.ask_text,
            "pending_slots": list(self.pending_slots),
            "transitions": [item.to_dict() for item in self.transitions],
            "notes": [dict(item) for item in self.notes],
            "executed": bool(self.executed),
            "executed_at": self.executed_at,
            "execution_ref": self.execution_ref,
            "work_item_id": self.work_item_id,
            "archive": dict(self.archive),
            "receipt": dict(self.receipt),
            # 台账投影列：状态机是状态的 owner，`work_items.status` 的翻译在这里做一次
            # （`core.tasks.store` 只消费，不再各写一份映射）。
            "work_item_status": self.work_item_status(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SlotFill":
        return cls(
            fill_id=str(data.get("fill_id") or ""),
            chat_id=str(data.get("chat_id") or ""),
            requester_person_id=str(data.get("requester_person_id") or ""),
            action_id=str(data.get("action_id") or ""),
            spec_kind=str(data.get("spec_kind") or "act"),
            state=str(data.get("state") or GATHERING),
            slots=[Slot.from_dict(item) for item in (data.get("slots") or ())],
            asked_batch=[str(name) for name in (data.get("asked_batch") or ())],
            # 别名读法：老的/手写的 blob 只带 ``confirm_ref`` 时也能读回来。
            confirm_message_ref=str(
                data.get("confirm_message_ref") or data.get("confirm_ref") or ""
            ),
            confirm_quoted=bool(data.get("confirm_quoted", False)),
            confirm_word=bool(data.get("confirm_word", False)),
            nudges=int(data.get("nudges") or 0),
            last_nudge_at=str(data.get("last_nudge_at") or ""),
            created_at=str(data.get("created_at") or ""),
            expires_at=str(data.get("expires_at") or ""),
            schema_version=int(data.get("schema_version") or 1),
            goal=str(data.get("goal") or ""),
            thread_id=str(data.get("thread_id") or ""),
            ask_text=str(data.get("ask_text") or ""),
            confirm_token=str(data.get("confirm_token") or ""),
            transitions=[
                Transition.from_dict(item) for item in (data.get("transitions") or ())
            ],
            notes=[dict(item) for item in (data.get("notes") or ())],
            executed=bool(data.get("executed", False)),
            executed_at=str(data.get("executed_at") or ""),
            execution_ref=str(data.get("execution_ref") or ""),
            work_item_id=str(data.get("work_item_id") or ""),
            archive=dict(data.get("archive") or {}),
            receipt=dict(data.get("receipt") or {}),
        )


@dataclass
class SlotFillOutcome:
    """一次推进的结论：调用方只读 ``action`` / ``text``，判定全在本模块。"""

    action: str = "ask"      # ask / confirm / reask / execute / already / expired
    state: str = GATHERING
    text: str = ""
    reason: str = ""
    executed: bool = False
    confirm_matched: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "state": self.state,
            "text": self.text,
            "reason": self.reason,
            "executed": bool(self.executed),
            "confirm_matched": bool(self.confirm_matched),
        }


# ------------------------------------------------------------------- flag
def slot_fill_mode() -> str:
    """``YINYUE_FEATURE_SLOT_FILL`` 的**唯一读点**（三态，默认 ``off``）。"""
    try:
        return features.mode(features.SLOT_FILL, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到时按 off（回滚位，不猜）
        warn("core/tasks/slot_fill.py:524 Exception", err=repr(__import__("sys").exc_info()[1]))
        return features.MODE_OFF


def tracing() -> bool:
    """``shadow`` / ``on``：状态机要跑（判定 + 落库 + 留痕）。"""
    return slot_fill_mode() != features.MODE_OFF


def enforcing() -> bool:
    """``on``：状态机的话**对外生效**（``shadow`` 只判定、不改文本）。"""
    return slot_fill_mode() == features.MODE_ON


def exec_receipt_mode() -> str:
    """``YINYUE_FEATURE_EXEC_RECEIPT`` 的**唯一读点**（三态，默认 ``off``）。

    这是 A1-d（执行回执接线）的开关：``off`` 一行都不走（回执段根本不进），
    ``shadow`` 只收口 + 留痕，``on`` 才把"没有回执 → 计划不得当完成"这条结论
    用在这一轮对外说的话上。

    **前置**：``YINYUE_FEATURE_SLOT_FILL`` 非 ``off``（次序关系，不是同一个布尔
    表达式——所以它在 ``tests/test_env_flag_isolation.py`` 里是**单独成组**的开关）。
    没有 N5 状态机就没有「待收口的 ``EXECUTING``」，回执无处可挂 → 按 ``off`` 处理
    （= 逐字节退回现状，含"旧确认路径 + 旧出话"）。
    """
    try:
        mode = features.mode(features.EXEC_RECEIPT, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到时按 off（回滚位，不猜）
        warn("core/tasks/slot_fill.py:552 Exception", err=repr(__import__("sys").exc_info()[1]))
        return features.MODE_OFF
    if mode == features.MODE_OFF:
        return features.MODE_OFF
    if not tracing():
        return features.MODE_OFF
    return mode


def receipt_tracing() -> bool:
    """``shadow`` / ``on``：收口 + 落库 + 留痕（回执要能查得到）。"""
    return exec_receipt_mode() != features.MODE_OFF


def receipt_enforcing() -> bool:
    """``on``：回执缺失时**这一轮的结论**改口径（计划不得当完成 → 走上报）。

    ``shadow`` 收口、落库、留痕，但**不拿结论改这一轮对外说的话**（读数模式）。
    """
    return exec_receipt_mode() == features.MODE_ON


# ------------------------------------------------------------------- 槽位定义
def _register(action: str, specs: Iterable[SlotSpec]) -> None:
    ACTION_SLOT_SPECS[action] = tuple(specs)


_register("create_reminder", (
    SlotSpec("时间", "时间", "明天早上8点",
             ("remind_at", "time", "when", "at", "时间")),
    SlotSpec("内容", "要提醒什么", "提醒我吃药",
             ("content", "text", "message", "what", "内容")),
))
_register("notify_user", (
    SlotSpec("发给谁", "收件人", "群里的某人",
             ("target", "to", "who", "user", "mention_name", "发给谁")),
    SlotSpec("说什么", "内容", "该出门了", ("content", "text", "message", "说什么")),
))
_register("send_message", (
    SlotSpec("发给谁", "收件人", "某某",
             ("target", "to", "chat_id", "who", "发给谁")),
    SlotSpec("说什么", "内容", "路上小心", ("content", "text", "message", "说什么")),
))


def slot_specs_for(action: str) -> tuple[SlotSpec, ...]:
    return ACTION_SLOT_SPECS.get(str(action or ""), ())


def _slot_from_spec(spec: SlotSpec) -> Slot:
    return Slot(
        name=spec.name,
        expected_type=spec.expected_type,
        expected_example=spec.expected_example,
        required=spec.required,
        ttl_seconds=int(spec.ttl_seconds),
        aliases=spec.aliases,
    )


def slots_for_tools(tools: Iterable[str], *, args: Mapping[str, Any] | None = None) -> list[Slot]:
    """毁灭性工具集 → 槽位表；**任何一个工具没有槽位定义就返回空**（不接管，不猜）。

    ``args`` 是计划里已经给出的工具参数（``SubTask.tool_args``）：别名对得上的先填进去
    （``source="plan"``），所以"计划里已经带全参数"的那种情况会直接走到 ``CONFIRMING``。
    """
    names = [str(name or "") for name in tools if str(name or "")]
    if not names:
        return []
    specs: list[SlotSpec] = []
    for name in names:
        found = slot_specs_for(name)
        if not found:
            log.info("slot_fill_action_unmapped", action=name)
            return []
        specs.extend(found)
    slots: list[Slot] = []
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            continue
        seen.add(spec.name)
        slots.append(_slot_from_spec(spec))
    if len(slots) > MAX_ASK_BATCH:
        # R-P1 的硬上限：一次最多问 3 项。多出来的槽位**留痕不静默丢**。
        log.warning("slot_fill_slots_over_cap", count=len(slots), cap=MAX_ASK_BATCH)
        slots = slots[:MAX_ASK_BATCH]
    plan_args = dict(args or {})
    for slot in slots:
        for key in (slot.name, *slot.aliases):
            raw = plan_args.get(key)
            if raw is None or str(raw).strip() == "" or str(raw).startswith("$"):
                continue
            slot.value = raw
            slot.source = "plan"
            slot.evidence = f"tool_args.{key}"
            break
    return slots


def fill_id_for(chat_id: Any, action_id: Any) -> str:
    """确定性 fill 身份（跨轮 upsert 的键；同一 ``(会话, 动作)`` 永远同一个 id）。"""
    seed = f"{str(chat_id or '')}|{str(action_id or '')}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def new_fill(data: SlotFillInput, *, now: datetime | None = None) -> SlotFill:
    """``SlotFillInput`` → 一条新 ``SlotFill``（``GATHERING`` 起步）。"""
    stamp = now or datetime.now()
    slots = [Slot.from_dict(slot.to_dict()) for slot in data.open_slots]
    ttl = int(data.ttl_seconds or DEFAULT_SLOT_TTL_SECONDS)
    for slot in slots:
        slot.ttl_seconds = int(slot.ttl_seconds or ttl)
    return SlotFill(
        fill_id=fill_id_for(data.chat_id, data.action_id),
        chat_id=str(data.chat_id or ""),
        requester_person_id=str(data.requester_person_id or ""),
        action_id=str(data.action_id or ""),
        spec_kind=str(data.spec_kind or "act"),
        state=GATHERING,
        slots=slots,
        goal=str(data.goal or ""),
        thread_id=str(data.thread_id or ""),
        created_at=_iso(stamp),
        expires_at=_iso(stamp + timedelta(seconds=ttl)),
    )


# ------------------------------------------------------------------- 引用校验
def incoming_from_text(
    text: str,
    *,
    message_ref: str = "",
    thread_id: str = "",
    quoted_msg_id: str = "",
    values: Mapping[str, Any] | None = None,
    at: str = "",
) -> Incoming:
    """从这一轮的消息文本抽"归属事实"：引用部分**一律走 owner**（``quote_of``）。"""
    quoted = quote_of(text)
    return Incoming(
        text=str(text or ""),
        message_ref=str(message_ref or ""),
        quoted_msg_id=str(quoted_msg_id or quoted.msg_id or ""),
        quoted_resolvable=bool(quoted.resolvable),
        # 上下文给了引用目标（结构化事实）也算"有引用"，哪怕正文里没有 ``[引用`` 块。
        quoted_present=bool(quoted.present or quoted_msg_id),
        quoted_author=str(quoted.author_name or ""),
        thread_id=str(thread_id or ""),
        values=dict(values or {}),
        at=str(at or ""),
    )


def clean_body(text: Any) -> str:
    """去掉引用块后的正文（引用的解析器是 owner 的 ``strip_quote_block``）。"""
    return str(strip_quote_block(str(text or ""))).strip()


def is_confirm_word(text: Any) -> bool:
    """``confirm_word``：这条正文是不是一句"确认"（否词优先；**不是**引用判定）。"""
    body = clean_body(text)
    if not body:
        return False
    flat = _strip_punct(body).lower()
    if not flat:
        return False
    if any(word in flat for word in NEGATION_WORDS):
        return False
    if flat in CONFIRM_WORDS:
        return True
    return len(flat) <= 8 and any(word in flat for word in CONFIRM_WORDS)


def confirm_word_in(text: Any, token: str = "") -> bool:
    """``confirm_word`` 的运行时读法：先把**我们印出去的编号**抹掉再判词。

    「确认 #A1B2C3」这种写法里，编号是引用证据、不是语气词；不抹掉它就会因为长度
    超阈值被判成"不是确认"（第一版踩过）。
    """
    body = str(text or "")
    if token:
        body = body.replace(str(token), " ")
    return is_confirm_word(body)


def reference_evidence(fill: SlotFill, incoming: Incoming) -> tuple[bool, str]:
    """这条消息**指向的是不是同一条待确认事项**（R-P4 的 ``confirm_quoted``）。

    三条证据，从强到弱：

    1. 引用的消息 id == :attr:`SlotFill.confirm_message_ref`（**最强**：真引用到了那一条）；
    2. 正文里原样带回 :attr:`SlotFill.confirm_token`（我们印在待确认文案里的短编号，
       现实引用稀疏时的**显式兜底**，契约 ``R-T0a``）；
    3. 正文里原样带回 ``confirm_message_ref``。

    都对不上 → ``(False, "no_reference")``。**没有证据就不许当确认**（R-P4 硬条款）。
    """
    ref = str(fill.confirm_message_ref or "")
    token = str(fill.confirm_token or "")
    if incoming.quoted_msg_id and ref and incoming.quoted_msg_id == ref:
        return True, f"quoted_msg_id={incoming.quoted_msg_id}"
    if token and token in incoming.text:
        return True, f"token={token}"
    if ref and ref in incoming.text:
        return True, f"ref={ref}"
    if incoming.quoted_present:
        # 有引用、但引的不是这条（或认不出引用对象）：**明确判否**，不是"证据不足"。
        return False, "quoted_other"
    return False, "no_reference"


# ------------------------------------------------------------------- 可回放
def replay(transitions: Sequence[Transition | Mapping[str, Any]],
           start: str = GATHERING) -> str:
    """按留痕把状态重放一遍（纯函数）：返回重放出来的终态。"""
    state = start
    for item in transitions:
        step = item if isinstance(item, Transition) else Transition.from_dict(item)
        state = step.to_state
    return state


def verify_chain(
    transitions: Sequence[Transition | Mapping[str, Any]], start: str = GATHERING
) -> tuple[str, list[str]]:
    """链完整性：每一步的 ``from`` 必须等于上一步的 ``to``；返回 ``(终态, 问题列表)``。"""
    state = start
    problems: list[str] = []
    for index, item in enumerate(transitions):
        step = item if isinstance(item, Transition) else Transition.from_dict(item)
        if step.from_state != state:
            problems.append(
                f"#{index} 的 from={step.from_state} 与当前状态 {state} 对不上"
            )
        if step.to_state not in STATES:
            problems.append(f"#{index} 的 to={step.to_state} 不是合法状态")
        state = step.to_state
    return state, problems


# ------------------------------------------------------------------- 状态机
class SlotFillMachine:
    """一条 ``SlotFill`` 的推进器。**所有判定都在这里**，调用方只消费结论。

    ``advance`` 一次 = 一个真实回合。状态迁移一律经 :meth:`_transition`，所以
    ``fill.transitions`` 是一条可回放的链（:func:`replay` / :func:`verify_chain`）。
    """

    def __init__(
        self,
        fill: SlotFill,
        *,
        open_fills: Sequence[Mapping[str, Any]] = (),
        repo: Any = None,
        anchor: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.fill = fill
        self.open_fills = [dict(item) for item in (open_fills or ())]
        self.repo = repo
        self.anchor = dict(anchor or {})
        self.now = now or datetime.now()

    # ------------------------------------------------------------- 对外入口
    def advance(self, incoming: Incoming) -> SlotFillOutcome:
        """推进一个回合。**这是唯一入口**（``off`` 时调用方根本不会构造它）。"""
        self._expire_if_due()
        state = self.fill.state
        if state == ABORTED:
            if str(self.fill.receipt.get("outcome") or "") == "failed":
                # 失败回执之后的同一件事：**如实说没办成**，不许退回"搁太久了"那种含糊话。
                self._note("failed_repeat", incoming.message_ref)
                self._persist()
                return self._outcome("failed", self._failed_text(), "execution_failed")
            return self._outcome("expired", self._expired_text(), "fill_expired")
        if state == DONE or (state == EXECUTING and self.fill.executed):
            # **幂等**：确认过的那件事只执行一次；这条确认再来一律不再执行。
            self._note("already_executed", incoming.message_ref)
            self._persist()
            return self._outcome("already", self._already_text(), "already_executed")
        if state == EXECUTING:
            # 已确认、执行已登记（等回执）。同一条确认重复来 → 仍然不重复执行。
            self._note("execution_in_flight", incoming.message_ref)
            self._persist()
            return self._outcome("already", self._already_text(), "execution_in_flight")
        if state == GATHERING:
            return self._advance_gathering(incoming)
        if state == VALIDATING:
            return self._advance_validating(incoming)
        if state == CONFIRMING:
            return self._advance_confirming(incoming)
        log.warning("slot_fill_state_unknown", state=state, fill_id=self.fill.fill_id)
        return self._outcome("reask", self._reask_text(), f"unknown_state:{state}")

    def complete(self, *, outcome: str = "sent", reply_text: str = "",
                 delivered_external_msg_id: str = "", steps: Sequence[Mapping[str, Any]] = (),
                 now: datetime | None = None) -> SlotFillOutcome:
        """``EXECUTING→DONE`` 的显式收口（A1-d 执行回执接线；见模块 docstring）。

        三条纪律：

        1. **只有执行真的成事才写 DONE**：``outcome`` 不在
           :data:`COMPLETED_OUTCOMES` 里 → 转 :meth:`fail`（失败态 + 原因），
           这里**绝不**写完成；
        2. **不许无中生有**：还没进 ``EXECUTING``（= 执行根本没发起）时，一条"回执"
           不能把这条待办点成完成——记一条 ``receipt_without_executing`` 后原样返回；
        3. **幂等**：同一条回执重复来 → 只记一次、状态不回退、不重复写 run。
        """
        stamp = now or self.now
        state = self.fill.state
        raw = str(outcome or "sent")
        if raw not in COMPLETED_OUTCOMES:
            return self.fail(
                outcome=raw,
                reason=str(reply_text or ""),
                steps=steps,
                now=stamp,
            )
        if self.fill.has_receipt() or state in (DONE, ABORTED):
            self._note("receipt_duplicate", f"state={state}")
            self._persist()
            return self._outcome("already", "", f"receipt_recorded:{state}")
        if state != EXECUTING:
            # 没有执行就没有回执：不许凭一条"完成"把计划点成完成。
            self._note("receipt_without_executing", f"state={state}")
            self._persist()
            log.warning(
                "slot_fill_receipt_without_executing",
                fill_id=self.fill.fill_id,
                state=state,
            )
            return self._outcome("refused", "", f"not_executing:{state}")
        self._transition(DONE, "execution_completed", raw)
        self.fill.executed = True
        self.fill.executed_at = _iso(stamp)
        run = task_store.finish_slot_fill_run(
            self.repo,
            self._run_handle(),
            outcome=raw,
            reply_text=reply_text,
            delivered_external_msg_id=delivered_external_msg_id,
            now=stamp,
        )
        if run:
            self.fill.execution_ref = str(run.get("trace_ref") or self.fill.execution_ref)
        self._record_receipt(
            outcome=raw, ok=True, reason="", steps=steps, at=stamp,
            external_msg_id=delivered_external_msg_id,
        )
        self._persist()
        return self._outcome("done", "", "execution_completed")

    def fail(self, *, outcome: str = "failed", reason: str = "",
             error_class: str = "", steps: Sequence[Mapping[str, Any]] = (),
             now: datetime | None = None) -> SlotFillOutcome:
        """**失败的回执**：``EXECUTING→ABORTED``（``reason="execution_failed"``）。

        失败必须留下**原因**（``reason`` 空则明说"没拿到失败原因"，不编），并且
        ``work_items.status`` 落 ``failed``（:meth:`SlotFill.work_item_status`）——
        这条链上**绝不允许**出现 ``DONE``：没办成的事不许当办好了（R-C5）。
        """
        stamp = now or self.now
        state = self.fill.state
        if self.fill.has_receipt() or state in (DONE, ABORTED):
            self._note("receipt_duplicate", f"state={state}")
            self._persist()
            return self._outcome("already", "", f"receipt_recorded:{state}")
        text = str(reason or "").strip() or "执行没成功（也没拿到失败原因）"
        if state != EXECUTING:
            self._note("receipt_without_executing", f"state={state}")
            self._persist()
            return self._outcome("refused", "", f"not_executing:{state}")
        self._transition(ABORTED, "execution_failed", text)
        self.fill.executed = True
        self.fill.executed_at = _iso(stamp)
        run = task_store.finish_slot_fill_run(
            self.repo,
            self._run_handle(),
            outcome="failed",
            error_class=str(error_class or task_store.DEFAULT_ERROR_CLASS),
            error_message=text,
            # 终态：N5 的执行没有重试方（见 store 的 finish_slot_fill_run 自述），
            # 留 open + next_retry_at 就是造一条没人重试的僵尸行。
            attempts=int(task_store.SLOT_FILL_TERMINAL_ATTEMPTS),
            now=stamp,
        )
        if run:
            self.fill.execution_ref = str(run.get("trace_ref") or self.fill.execution_ref)
        self._record_receipt(
            outcome="failed", ok=False, reason=text, steps=steps, at=stamp,
        )
        self._persist()
        log.warning(
            "slot_fill_execution_failed",
            fill_id=self.fill.fill_id,
            work_item_id=self.fill.work_item_id,
            reason=text[:160],
        )
        return self._outcome("failed", "", "execution_failed")

    # ------------------------------------------------------------- 各状态
    def _advance_gathering(self, incoming: Incoming) -> SlotFillOutcome:
        # 第一回合是**请求本身**，不是对我们提问的回答：没问过就不吸收
        # （除非调用方显式给了值——那是陈述，不是推断）。
        asked_before = bool(self.fill.asked_batch) or bool(incoming.values)
        changed = self._absorb(incoming) if asked_before else False
        if self._complete():
            self._transition(
                VALIDATING, "slots_complete",
                ",".join(sorted(slot.name for slot in self.fill.slots)),
            )
            return self._advance_validating(incoming)
        self.fill.nudges += 1
        self.fill.last_nudge_at = _iso(self.now)
        text = self._ask_text(self._missing_names())
        self.fill.ask_text = text
        self._persist()
        return self._outcome("ask", text, "missing_slots")

    def _advance_validating(self, incoming: Incoming) -> SlotFillOutcome:
        invalid = self._validate()
        if not invalid:
            self.fill.confirm_token = self._new_token()
            self.fill.confirm_word = False
            self.fill.confirm_quoted = False
            self._transition(
                CONFIRMING, "slots_valid",
                ",".join(sorted(slot.name for slot in self.fill.slots)),
            )
            text = self._confirm_text()
            self.fill.ask_text = text
            self._persist()
            return self._outcome("confirm", text, "await_confirm")
        # R-P3：只要一个不匹配就退回 GATHERING，且**只问那一个**。
        self._transition(
            GATHERING, "invalid_slot",
            ",".join(slot.name for slot in invalid),
        )
        names = [slot.name for slot in invalid]
        text = self._ask_text(names, only_invalid=True)
        self.fill.ask_text = text
        self.fill.nudges += 1
        self.fill.last_nudge_at = _iso(self.now)
        self._persist()
        return self._outcome("ask", text, "invalid:" + ",".join(names))

    def _advance_confirming(self, incoming: Incoming) -> SlotFillOutcome:
        corrections = self._corrections(incoming)
        if corrections:
            # R-P5：自然语言纠正 → 回 VALIDATING 重判（不把纠正当确认）。
            self._note("user_correction", ",".join(sorted(corrections)))
            for name, value in corrections.items():
                slot = self.fill.slot(name)
                if slot is None:
                    continue
                slot.value = value
                slot.closed = False
                slot.valid = False
                slot.invalid_reason = ""
                slot.source = "correction"
                slot.evidence = incoming.message_ref
            self._transition(
                VALIDATING, "user_correction", ",".join(sorted(corrections)),
            )
            return self._advance_validating(incoming)

        word = confirm_word_in(incoming.text, self.fill.confirm_token)
        quoted, evidence = reference_evidence(self.fill, incoming)
        self.fill.confirm_word = bool(word)
        self.fill.confirm_quoted = bool(quoted)
        if word and quoted:
            self._transition(
                EXECUTING, "confirm_ref_matched", evidence,
            )
            self.fill.executed = True
            self.fill.executed_at = _iso(self.now)
            self._start_run()
            self.fill.ask_text = ""
            self._persist()
            log.info(
                "slot_fill_confirmed",
                fill_id=self.fill.fill_id,
                evidence=evidence,
                ref=self.fill.confirm_ref,
            )
            return self._outcome("execute", "", "confirm_ref_matched",
                                 executed=True, confirm_matched=True)
        if word and not quoted:
            # **关键格**：说了"好/可以"但指向对不上 → 不许当确认，重新问一句。
            self.fill.nudges += 1
            self.fill.last_nudge_at = _iso(self.now)
            self._note("confirm_rejected", evidence)
            text = self._reask_text(evidence=evidence)
            self.fill.ask_text = text
            self._persist()
            log.info(
                "slot_fill_confirm_rejected",
                fill_id=self.fill.fill_id,
                evidence=evidence,
                ref=self.fill.confirm_ref,
            )
            return self._outcome("reask", text, f"reference_mismatch:{evidence}")

        if self._ambiguous():
            self._note("ambiguous_slot", ",".join(
                str(item.get("fill_id") or "") for item in self.open_fills
            ))
            text = self._ambiguity_text()
            self.fill.ask_text = text
            self._persist()
            return self._outcome("reask", text, "ambiguous_slot")

        if self._absorb(incoming):
            self._transition(VALIDATING, "values_updated", incoming.message_ref)
            return self._advance_validating(incoming)
        self.fill.nudges += 1
        self.fill.last_nudge_at = _iso(self.now)
        text = self._reask_text(evidence="not_a_confirm")
        self.fill.ask_text = text
        self._persist()
        return self._outcome("reask", text, "not_a_confirm")

    # ------------------------------------------------------------- 槽位操作
    def _absorb(self, incoming: Incoming) -> bool:
        """把这一轮的话落进槽位。返回"有没有变"。**不解析时间**（那是工具侧的活）。"""
        body = clean_body(incoming.text)
        if not body:
            return False
        changed = False
        # ① 显式给值（调用方已解析 / 用户写了「时间：明天早上8点」）
        for name, value in self._explicit_values(incoming).items():
            slot = self.fill.slot(name)
            if slot is None or not slot.is_open():
                continue
            slot.value = value
            slot.source = "user"
            slot.evidence = incoming.message_ref
            slot.valid = False
            slot.invalid_reason = ""
            changed = True
        open_slots = [slot for slot in self.fill.slots if slot.is_open() and not slot.is_filled()]
        if not open_slots:
            return changed
        segments = _split_segments(body)
        # ② 只剩一个缺口 → 整句就是它的值（最自然的一问一答）
        if len(open_slots) == 1:
            slot = open_slots[0]
            if not slot.is_filled():
                slot.value = body
                slot.source = "user"
                slot.evidence = incoming.message_ref
                changed = True
            return changed
        # ③ 多个缺口 → 按问的顺序逐段分配（一次问齐、按序回答）
        for slot, segment in zip(open_slots, segments):
            if not segment or slot.is_filled():
                continue
            slot.value = segment
            slot.source = "user"
            slot.evidence = incoming.message_ref
            changed = True
        return changed

    def _explicit_values(self, incoming: Incoming) -> dict[str, str]:
        """槽位名/别名 + 分隔符 的显式赋值（``时间：明天早上8点``）+ 调用方给的值。"""
        found: dict[str, str] = {}
        for slot in self.fill.slots:
            keys = (slot.name, *slot.aliases)
            for key in keys:
                if key in incoming.values and str(incoming.values[key]).strip():
                    found[slot.name] = str(incoming.values[key]).strip()
                    break
        body = clean_body(incoming.text)
        if not body:
            return found
        for slot in self.fill.slots:
            if slot.name in found:
                continue
            for key in (slot.name, *slot.aliases):
                value = _labelled_value(body, key)
                if value:
                    found[slot.name] = value
                    break
        return found

    def _corrections(self, incoming: Incoming) -> dict[str, str]:
        """``CONFIRMING`` 里的纠正：只认**显式赋值**，不把随口一句话当纠正。"""
        explicit = self._explicit_values(incoming)
        return {
            name: value for name, value in explicit.items()
            if (slot := self.fill.slot(name)) is not None and slot.closed
            and str(slot.value) != str(value)
        }

    def _validate(self) -> list[Slot]:
        """逐槽位校验（R-P3）：返回**没通过**的那些。"""
        invalid: list[Slot] = []
        for slot in self.fill.slots:
            slot.valid = False
            slot.invalid_reason = ""
            if not slot.is_filled():
                if slot.required:
                    slot.invalid_reason = f"还没给我「{slot.name}」"
                    invalid.append(slot)
                slot.closed = False
                continue
            problem = _type_problem(slot)
            if problem:
                slot.invalid_reason = problem
                slot.closed = False
                invalid.append(slot)
                continue
            slot.valid = True
            slot.closed = True
        return invalid

    def _complete(self) -> bool:
        """R-P2：所有必需槽位都有值才允许前进（**缺参不得进 VALIDATING**）。"""
        return all(slot.is_filled() for slot in self.fill.slots if slot.required)

    def _missing_names(self) -> list[str]:
        return [slot.name for slot in self.fill.missing()][:MAX_ASK_BATCH]

    # ------------------------------------------------------------- 文本渲染
    def _ask_text(self, names: Sequence[str], *, only_invalid: bool = False) -> str:
        """R-P1 / R-P6：一次问齐（≤3）+ 复述已知 + 每个缺口带"例如…"。"""
        self.fill.asked_batch = [str(name) for name in names][:MAX_ASK_BATCH]
        lines: list[str] = []
        if self.fill.goal:
            lines.append(f"我记着的是：{self.fill.goal}（要是我理解错了，直接说）")
        head = "还差一项" if len(self.fill.asked_batch) == 1 else f"还差 {len(self.fill.asked_batch)} 项"
        if only_invalid:
            head = "这项我没对上"
        lines.append(f"{head}，一次说完就行：")
        for index, name in enumerate(self.fill.asked_batch, 1):
            slot = self.fill.slot(name)
            if slot is None:
                continue
            reason = f"（{slot.invalid_reason}）" if slot.invalid_reason else ""
            lines.append(f"{index}. {slot.hint()}{reason}")
        return "\n".join(lines)

    def _confirm_text(self) -> str:
        """R-P4：一条**列出全部参数**的待确认消息 + 引用/编号要求。"""
        lines = ["我准备这样做："]
        for index, slot in enumerate(self.fill.slots, 1):
            lines.append(f"{index}. {slot.name}：{slot.value}")
        lines.append("")
        lines.append(
            f"要我照做的话，**引用我这条**、或者说一句「确认 {self.fill.confirm_token}」，"
            "我就动手。"
        )
        return "\n".join(lines)

    def _reask_text(self, *, evidence: str = "") -> str:
        """引用对不上时的重问（**不执行、不装作收到确认**）。"""
        if evidence == "quoted_other":
            head = "你引的不是我那条待确认消息"
        elif evidence == "no_reference":
            head = "我这边对不上号（没看到你引用我那条、也没看到编号）"
        else:
            head = "这句我当不了确认"
        return (
            f"{head}。{self._confirm_text()}"
        )

    def _ambiguity_text(self) -> str:
        """R-P8：多个未闭合槽位且信号拉不开 → **不猜**，直接问。"""
        others = [
            str(item.get("goal") or item.get("fill_id") or "另一件事")
            for item in self.open_fills
            if str(item.get("fill_id") or "") != self.fill.fill_id
        ]
        mine = self.fill.goal or "这件事"
        options = " 还是 ".join([f"「{mine}」", *[f"「{name}」" for name in others[:2]]])
        return f"你是在回 {options}？点明一下我照办。"

    def _already_text(self) -> str:
        return "这件事我已经确认过了、正在办，不用再说一遍。"

    def _expired_text(self) -> str:
        return "刚才那件事搁太久了，我先放下了；想办的话再说一声。"

    def _failed_text(self) -> str:
        """**失败回执之后**的回话（不许说成"办好了"，也不许含糊成"搁久了"）。"""
        reason = str(self.fill.receipt.get("reason") or "").strip()
        head = "刚才那件事我没办成"
        tail = "要我再试一次就说一声。"
        return f"{head}（{reason}）。{tail}" if reason else f"{head}。{tail}"

    # ------------------------------------------------------------- 内部
    def _ambiguous(self) -> bool:
        others = [
            item for item in self.open_fills
            if str(item.get("fill_id") or "") != self.fill.fill_id
        ]
        if not others:
            return False
        # 四信号里只有"同线程"能在这里判；引用信号在调用点已判否。
        return any(
            str(item.get("thread_id") or "") == str(self.fill.thread_id or "")
            for item in others
        )

    def _new_token(self) -> str:
        if self.fill.confirm_token:
            return self.fill.confirm_token
        return TOKEN_PREFIX + uuid.uuid4().hex[:6].upper()

    def _expire_if_due(self) -> None:
        if self.fill.state in (DONE, ABORTED):
            return
        if not self.fill.is_expired(self.now):
            return
        self.fill.archive = {
            "who": self.fill.requester_person_id,
            "when": _iso(self.now),
            "where": self.fill.chat_id,
            "what": self.fill.goal or self.fill.action_id,
            "succeeded": bool(self.fill.executed),
        }
        self._transition(ABORTED, "expired", self.fill.expires_at)
        self.fill.ask_text = ""
        self._persist()

    def _transition(self, to_state: str, reason: str, evidence: str = "") -> None:
        if to_state not in STATES:
            raise ValueError(f"unknown slot-fill state: {to_state}")
        step = Transition(
            from_state=self.fill.state,
            to_state=to_state,
            reason=str(reason or ""),
            at=_iso(self.now),
            evidence=str(evidence or ""),
        )
        self.fill.transitions.append(step)
        self.fill.state = to_state
        log.info(
            "slot_fill_transition",
            fill_id=self.fill.fill_id,
            from_state=step.from_state,
            to_state=to_state,
            reason=step.reason,
        )

    def _note(self, kind: str, detail: str = "") -> None:
        """记一条**非迁移**判定（引用对不上 / 歧义 / 纠正）：状态没动也要留痕。"""
        self.fill.notes.append({
            "at": _iso(self.now),
            "kind": str(kind),
            "detail": str(detail),
            "state": self.fill.state,
        })
        while len(self.fill.notes) > 32:
            self.fill.notes.pop(0)

    def _start_run(self) -> None:
        """``EXECUTING`` 时落 ``work_item_run_started``（§4.2：一次 run = 一次执行）。"""
        if not self.fill.work_item_id:
            self.fill.work_item_id = task_store.slot_fill_work_item_id(self.fill.fill_id)
        self._run = task_store.record_slot_fill_run(
            self.repo,
            work_item_id=self.fill.work_item_id,
            claim_owner=CLAIM_OWNER,
            now=self.now,
        )
        if self._run:
            self.fill.execution_ref = str(self._run.get("run_id") or "")

    def _run_handle(self) -> dict[str, Any]:
        """这一次 run 的句柄（收口时交给台账）。

        跨进程收口（状态机是按 fill_id 重建的）拿不到 :attr:`_run`，就从台账把
        **还没结束的那一次 run** 读回来——没有它 ``started_at`` 只能填 0，
        耗时字段就成了假数（回执四要素之一，A1-d 的验收项）。
        """
        run = getattr(self, "_run", None) or {}
        if not run and self.repo is not None:
            run = task_store.latest_slot_fill_run(
                self.repo, fill_id=self.fill.fill_id, unfinished_only=True
            ) or {}
        started = run.get("started_at")
        if not isinstance(started, datetime):
            started = _parse_iso(self.fill.created_at) or self.now
        return {
            "work_item_id": self.fill.work_item_id
            or task_store.slot_fill_work_item_id(self.fill.fill_id),
            "run_id": str(run.get("run_id") or self.fill.execution_ref or ""),
            "run_seq": int(run.get("run_seq") or 1),
            "trigger_source": "message",
            "started_at": started,
        }

    def _record_receipt(
        self,
        *,
        outcome: str,
        ok: bool,
        reason: str,
        steps: Sequence[Mapping[str, Any]],
        at: datetime,
        external_msg_id: str = "",
    ) -> None:
        """把回执写进 fill blob（**只有它算"办好了"的证据**）。

        耗时与 trace_ref 优先取台账写回的那一行（``latest_slot_fill_run``）：台账
        才是权威；台账关着时按 ``started_at`` 与 ``at`` 现算。外部消息 id 只认工具
        返回里真有的事实，**认不出就空着**（不编）。
        """
        run = task_store.latest_slot_fill_run(self.repo, fill_id=self.fill.fill_id)
        facts = dict(run or {})
        handle = self._run_handle()
        duration = int(facts.get("duration_ms") or 0)
        if not duration:
            started = handle.get("started_at")
            if isinstance(started, datetime):
                duration = max(0, int((at - started).total_seconds() * 1000))
        self.fill.receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "outcome": str(outcome or ""),
            "ok": bool(ok),
            "reason": str(reason or ""),
            "at": _iso(at),
            "state": self.fill.state,
            "run_id": str(facts.get("run_id") or handle.get("run_id") or ""),
            "run_seq": int(facts.get("run_seq") or handle.get("run_seq") or 1),
            "trace_ref": str(facts.get("trace_ref") or ""),
            "duration_ms": int(duration),
            "external_msg_id": str(
                facts.get("delivered_external_msg_id") or external_msg_id or ""
            ),
            "steps": [dict(item) for item in (steps or ())],
            "acceptance": ACCEPTANCE_CONFIRMED,
        }

    def _persist(self) -> None:
        """状态与槽位落 ``work_items``（复用台账，见模块 docstring）。"""
        item_id = task_store.record_slot_fill(
            self.repo,
            self.fill,
            anchor=self.anchor,
            now=self.now,
        )
        if item_id:
            self.fill.work_item_id = item_id

    def _outcome(self, action: str, text: str, reason: str,
                 *, executed: bool = False, confirm_matched: bool = False
                 ) -> SlotFillOutcome:
        return SlotFillOutcome(
            action=action,
            state=self.fill.state,
            text=text,
            reason=reason,
            executed=executed,
            confirm_matched=confirm_matched,
        )


# ------------------------------------------------- 计划层不变式 + 执行回执（A1-d）
#: 「答应了就必须有回执」这条不变式的**判定层**在这里，不在出站口。
#: 出话时查承诺词再拦/改写是 prompt 级补丁（主人已明确拒绝）；本层做的是结构约束：
#: **承诺型步骤（:data:`PROMISE_TOOLS`）只有在拿到执行回执之后才允许被当作已完成**，
#: 否则该计划不得视为完成，只能走 R-C5 的上报（问用户 / 明说没做到）。
def _field(source: Any, name: str) -> Any:
    """鸭子类型取字段（``Plan`` / ``PlanResult`` / ``SubTask`` / dict 都能判）。"""
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _steps_of(source: Any) -> tuple[Any, ...]:
    """取步骤序列：``PlanResult.steps`` 或 ``Plan.subtasks``（两种载体同一口径）。"""
    if source is None:
        return ()
    for name in ("steps", "subtasks"):
        value = _field(source, name)
        if value is None:
            continue
        if isinstance(value, (str, bytes)):
            continue
        try:
            return tuple(value)
        except TypeError:
            warn("core/tasks/slot_fill.py:1436 TypeError", err=repr(__import__("sys").exc_info()[1]))
            continue
    return ()


def step_tool(step: Any) -> str:
    """这一步用什么工具（契约口径 ``method.tool``；``SubTask`` 的旧字段 ``tool`` 兜底）。"""
    method = _field(step, "method")
    tool = str(_field(method, "tool") or "") if method is not None else ""
    return tool or str(_field(step, "tool") or "")


def promise_steps(source: Any) -> tuple[Any, ...]:
    """计划里的**承诺型步骤**（"要替主人做事"的那些，:data:`PROMISE_TOOLS`）。"""
    return tuple(step for step in _steps_of(source) if step_tool(step) in PROMISE_TOOLS)


def plan_action_id(plan: Any) -> str:
    """计划 → 台账 ``action_id``（**唯一**身份来源，:func:`fill_from_plan` 与回执共用）。

    与 ``fill_from_plan`` 的旧口径逐字一致：优先 ``plan_id``，没有就按工具集合拼一个
    （否则没有计划身份就没有"这条承诺的回执行"）。
    """
    plan_id = str(_field(plan, "plan_id") or "")
    if plan_id:
        return plan_id
    tools = sorted({step_tool(step) for step in _steps_of(plan) if step_tool(step)})
    return "|".join(tools)


@dataclass(frozen=True)
class ExecutionReceipt:
    """**一条执行回执**：某个计划（= 某条 fill）的执行到底有结果没有、结果是什么。

    这是 A1-d 的产物，也是计划层不变式唯一的证据类型。``ok=True`` 只认
    ``outcome ∈ :data:`COMPLETED_OUTCOMES```——"跑过了"不是"办成了"。
    """

    fill_id: str = ""
    work_item_id: str = ""
    state: str = ""
    outcome: str = ""
    ok: bool = False
    reason: str = ""
    duration_ms: int = 0
    external_msg_id: str = ""
    run_id: str = ""
    trace_ref: str = ""
    step_ids: tuple[int, ...] = ()
    tools: tuple[str, ...] = ()
    details: tuple[dict[str, Any], ...] = ()
    source: str = ""          # ledger / memory
    at: str = ""

    def covers(self, step: Any) -> bool:
        """这条回执覆盖不覆盖这一步（先按 ``step_id``，再按工具名）。"""
        step_id = _int_or_zero(_field(step, "id"))
        tool = step_tool(step)
        if step_id and step_id in self.step_ids:
            return True
        return bool(tool) and tool in self.tools

    def step_ok(self, step: Any) -> bool:
        """这一步在回执里是不是"成事"（有逐步骤明细就按明细判，否则按整条回执判）。"""
        if not (self.ok and self.covers(step)):
            return False
        step_id = _int_or_zero(_field(step, "id"))
        tool = step_tool(step)
        for item in self.details:
            if int(item.get("step_id") or 0) == step_id or (
                tool and str(item.get("tool") or "") == tool
            ):
                return bool(item.get("ok"))
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "work_item_id": self.work_item_id,
            "state": self.state,
            "outcome": self.outcome,
            "ok": bool(self.ok),
            "reason": self.reason,
            "duration_ms": int(self.duration_ms),
            "external_msg_id": self.external_msg_id,
            "run_id": self.run_id,
            "trace_ref": self.trace_ref,
            "step_ids": [int(item) for item in self.step_ids],
            "tools": list(self.tools),
            "details": [dict(item) for item in self.details],
            "source": self.source,
            "at": self.at,
        }


@dataclass(frozen=True)
class PlanCompletion:
    """**"这个计划算不算完成"的唯一结论**（计划层不变式 + R-C5 的上报）。

    ``complete=False`` 时 ``report`` 一定非空（复用
    ``core.contracts.content_plan.budget_report`` 的措辞，不许各调用点自己拼一句），
    且 ``asked_user=True``：这类事要么问用户，要么明说做不到，**不许沉默当办完**。

    两条不变式由**同一个 owner** 一次判出来（不是两套判定）：

    * ``missing`` / ``missing_tools`` —— **没有执行回执**的承诺型步骤（A1-d）；
    * ``missing_evidence`` —— **没有来源**的那些步骤（声明型断言必须有取数步骤，
      主人标注 No.10/11；判据在 ``core.contracts.content_plan.evidence_verdict``）。
    """

    complete: bool = True
    missing: tuple[int, ...] = ()
    missing_tools: tuple[str, ...] = ()
    over_budget: bool = False
    asked_user: bool = False
    report: str = ""
    receipts: tuple[ExecutionReceipt, ...] = ()
    #: 证据不变式点名的那几步（``evidence_verdict`` 给的 id；空元组 = 这条不成立）。
    missing_evidence: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": bool(self.complete),
            "missing": [int(item) for item in self.missing],
            "missing_tools": list(self.missing_tools),
            "over_budget": bool(self.over_budget),
            "asked_user": bool(self.asked_user),
            "report": self.report,
            "receipts": [item.to_dict() for item in self.receipts],
            "missing_evidence": [int(item) for item in self.missing_evidence],
        }


def receipt_from_fill(
    fill: Any,
    *,
    run: Mapping[str, Any] | None = None,
    source: str = "memory",
) -> ExecutionReceipt | None:
    """``SlotFill.receipt`` blob → :class:`ExecutionReceipt`（没有回执 → ``None``）。"""
    payload = dict(_field(fill, "receipt") or {})
    if not payload:
        return None
    facts = dict(run or {})
    details = tuple(dict(item) for item in (payload.get("steps") or ()))
    step_ids = tuple(
        int(item.get("step_id"))
        for item in details
        if _int_or_zero(item.get("step_id"))
    )
    tools = tuple(
        str(item.get("tool")) for item in details if str(item.get("tool") or "")
    )
    outcome = str(payload.get("outcome") or "")
    return ExecutionReceipt(
        fill_id=str(_field(fill, "fill_id") or ""),
        work_item_id=str(_field(fill, "work_item_id") or ""),
        state=str(_field(fill, "state") or ""),
        outcome=outcome,
        ok=bool(payload.get("ok")) and outcome in COMPLETED_OUTCOMES,
        reason=str(payload.get("reason") or ""),
        duration_ms=int(payload.get("duration_ms") or facts.get("duration_ms") or 0),
        external_msg_id=str(
            payload.get("external_msg_id")
            or facts.get("delivered_external_msg_id")
            or ""
        ),
        run_id=str(payload.get("run_id") or facts.get("run_id") or ""),
        trace_ref=str(payload.get("trace_ref") or facts.get("trace_ref") or ""),
        step_ids=step_ids,
        tools=tools,
        details=details,
        source=str(source or ""),
        at=str(payload.get("at") or ""),
    )


def load_receipt(repo: Any, fill_id: Any) -> ExecutionReceipt | None:
    """从**台账**读回一条回执（``spec_json.slot_fill.receipt`` + 最近一次 run）。

    返回 ``None`` = 台账里**查不到回执**（flag 关 / 没这一行 / 没写过回执）——调用方
    一律按"没有回执"处理，**绝不**读成"默认已完成"（`docs/archive/任务清单_2026-10-06.md:43` A1-d 的判据
    就是"真实流量上查得到"）。
    """
    data = task_store.load_slot_fill(repo, fill_id)
    if not data:
        return None
    stored = task_store.load_slot_fill_receipt(repo, fill_id) or {}
    return receipt_from_fill(
        SlotFill.from_dict(data), run=stored.get("run") or {}, source="ledger"
    )


def unreceipted_promises(
    source: Any, receipts: Iterable[ExecutionReceipt | Mapping[str, Any]] = ()
) -> tuple[Any, ...]:
    """**没有回执的承诺型步骤**（空元组 = 每一件答应的事都有可查的执行回执）。"""
    items = tuple(_as_receipt(item) for item in (receipts or ()))
    items = tuple(item for item in items if item is not None)
    return tuple(
        step
        for step in promise_steps(source)
        if not any(item.step_ok(step) for item in items)
    )


def plan_completion(
    source: Any,
    receipts: Iterable[ExecutionReceipt | Mapping[str, Any]] = (),
    *,
    budget: Any = None,
) -> PlanCompletion:
    """**计划层不变式**（唯一 owner）：这个计划凭什么算完成？

    两条判据，一次判出、一个结论（**不是两套判定**）：

    ① **执行回执**（A1-d）：这些承诺型步骤都有回执了吗？没有 → 不得视为完成；
    ② **证据不变式**（主人标注 No.10/11）：这个计划里有取数步骤吗？一步都没有 → 不得
       视为完成（"要断言具体的人、作品或事实，得先有来源"）。判据在
       ``core.contracts.content_plan.evidence_verdict``，本函数只读它的结论。

    ``source`` 是鸭子类型的 ``PlanResult`` / ``Plan``（``steps`` 或 ``subtasks``）。
    ``budget`` 是 ``core.contracts.content_plan.StepBudget``（缺省用它自己的默认值：
    单步尝试 ≤ 3 = §7 拍板项 5 的口径）。某一个缺失步骤``attempts`` 到顶 →
    ``over_budget=True``（R-C5：超预算**必须上报**）。②点名的步骤到顶同样算超预算。

    结论里 ``complete=False`` 时 ``report`` 一定非空、``asked_user=True``——这条不变式
    的出口是**上报**，不是沉默，也不是"先说办好了"。
    """
    from core.contracts.content_plan import (
        budget_report,
        evidence_verdict,
        is_over_budget,
    )

    items = tuple(
        item for item in (_as_receipt(raw) for raw in (receipts or ())) if item is not None
    )
    promises = promise_steps(source)
    missing = tuple(
        step
        for step in promises
        if not any(item.step_ok(step) for item in items)
    )
    if missing:
        over_budget = any(bool(is_over_budget(step, budget)) for step in missing)
        first = missing[0]
        reason = _missing_reason(first, items)
        head = (
            f"第 {_int_or_zero(_field(first, 'id'))} 步"
            f"「{str(_field(first, 'description') or '')}」"
        )
        question = (
            f"{head}我还没拿到执行回执（这类事没回执我不当办好了）"
            f"{'：' + reason if reason else ''}"
            "。要我再试一次，还是先放着？"
        )
        return PlanCompletion(
            complete=False,
            missing=tuple(_int_or_zero(_field(step, "id")) for step in missing),
            missing_tools=tuple(step_tool(step) for step in missing),
            over_budget=bool(over_budget),
            asked_user=True,
            report=budget_report(step=first, asks_user=True, question=question),
            receipts=items,
        )
    # ② 证据不变式：没有取数步骤 → 不得视为完成（**不是**"静默放过"）。
    evidence = evidence_verdict(source)
    if not bool(evidence.get("ok")):
        missing_evidence = tuple(
            int(item) for item in (evidence.get("steps") or ())
        )
        first_step = _step_by_id(source, missing_evidence[0] if missing_evidence else 0)
        over_evidence = bool(
            first_step is not None and is_over_budget(first_step, budget)
        )
        return PlanCompletion(
            complete=False,
            missing_evidence=missing_evidence,
            over_budget=over_evidence,
            asked_user=True,
            report=budget_report(
                step=first_step, asks_user=True,
                question=f"{str(evidence.get('reason') or '').strip()}。"
                         "要我先去查一下，还是你直接告诉我？",
            ),            receipts=items,
        )
    return PlanCompletion(complete=True, receipts=items)


def _step_by_id(source: Any, step_id: int) -> Any | None:
    """按 ``id`` 找那一步（报告要点名是哪一步；找不到就 ``None``，不编）。"""
    for step in _steps_of(source):
        if _int_or_zero(_field(step, "id")) == int(step_id):
            return step
    return None


def _as_receipt(raw: Any) -> ExecutionReceipt | None:
    """把入参收敛成 :class:`ExecutionReceipt`（Mapping 也能进，便于离线读数）。"""
    if raw is None:
        return None
    if isinstance(raw, ExecutionReceipt):
        return raw
    if isinstance(raw, Mapping):
        data = dict(raw)
        return ExecutionReceipt(
            fill_id=str(data.get("fill_id") or ""),
            work_item_id=str(data.get("work_item_id") or ""),
            state=str(data.get("state") or ""),
            outcome=str(data.get("outcome") or ""),
            ok=bool(data.get("ok")),
            reason=str(data.get("reason") or ""),
            duration_ms=int(data.get("duration_ms") or 0),
            external_msg_id=str(data.get("external_msg_id") or ""),
            run_id=str(data.get("run_id") or ""),
            trace_ref=str(data.get("trace_ref") or ""),
            step_ids=tuple(int(item) for item in (data.get("step_ids") or ())),
            tools=tuple(str(item) for item in (data.get("tools") or ())),
            details=tuple(dict(item) for item in (data.get("details") or ())),
            source=str(data.get("source") or ""),
            at=str(data.get("at") or ""),
        )
    if hasattr(raw, "receipt"):
        return receipt_from_fill(raw)
    return None


def _missing_reason(step: Any, receipts: Sequence[ExecutionReceipt]) -> str:
    """这一步为什么没回执：有失败回执就给原因，否则空串（**不编**）。"""
    step_id = _int_or_zero(_field(step, "id"))
    tool = step_tool(step)
    for item in receipts:
        if not item.covers(step):
            continue
        for detail in item.details:
            if int(detail.get("step_id") or 0) == step_id or (
                tool and str(detail.get("tool") or "") == tool
            ):
                return str(detail.get("reason") or item.reason or "")
        if item.reason:
            return item.reason
    return ""


def _step_result(step: Any) -> dict[str, Any]:
    """一个承诺型步骤的**执行结果**（计划里的执行态是唯一事实：status + result + data）。"""
    status = _status_text(_field(step, "status"))
    ok = status == "done"
    result = str(_field(step, "result") or "")
    return {
        "step_id": _int_or_zero(_field(step, "id")),
        "tool": step_tool(step),
        "ok": bool(ok),
        "status": status,
        "reason": "" if ok else (result or f"这一步的状态是 {status or 'unknown'}"),
        "external_msg_id": _external_msg_id(_field(step, "data")),
    }


def _external_msg_id(data: Any) -> str:
    """工具返回里的**外部消息 / 记录 id**（``ToolResult.data``）；认不出就空串（不猜）。"""
    if not isinstance(data, Mapping):
        return ""
    for key in (
        "external_msg_id", "message_id", "msg_id", "send_id", "reminder_id", "id",
    ):
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _status_text(status: Any) -> str:
    """``TaskStatus`` / ``StepStatus`` / 裸字符串 → 小写文本（两种枚举都吃）。"""
    value = getattr(status, "value", status)
    return str(value or "").strip().lower()


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        warn("core/tasks/slot_fill.py:1821 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return 0


def _ledger_authoritative(repo: Any) -> bool:
    """台账可用？（与 :func:`_load_or_none` 同一判据：LEDGER 开 + 取得到 meta engine。）"""
    return bool(task_store.ledger_enabled()) and task_store.resolve_engine(repo) is not None


def settle_plan_execution(
    *,
    plan: Any,
    context: Mapping[str, Any] | None = None,
    repo: Any = None,
    cache: MutableMapping[str, SlotFill] | None = None,
    now: datetime | None = None,
) -> PlanCompletion | None:
    """**A1-d 接线入口**：把这一轮的真实执行结果收口成回执，并给出计划层结论。

    调用点（唯一）：``agents/conversational.py::_react_with_planner`` 在**执行循环之后、
    选回复之前**调一次。返回值：

    * ``None`` —— 回执层没接管（``off`` / 前置不满足 / 这一轮**没有承诺型步骤** /
      ``shadow`` 只读数）→ 调用方照旧说它原来那句话（**逐字节退回现状**）；
    * :class:`PlanCompletion` —— ``on`` 位：``complete=False`` 时调用方**不得**把这一轮
      当办好了，只能回 ``report``（上报）。

    收口规则（复用 :class:`SlotFillMachine`，不新建表）：

    * 每个承诺型步骤按计划里的执行态（``status`` / ``result`` / ``data``）判成败；
    * **全成** → ``EXECUTING→DONE`` + ``work_item_runs.outcome="sent"`` + 耗时 + 外部 id；
    * **有败** → 失败回执（``outcome="failed"`` + 原因），状态 ``ABORTED``、
      ``work_items.status="failed"``——**绝不写 DONE**；
    * 台账可用时**以台账为权威**读回执（落库失败 = 没有回执，不退回内存说法）；
      台账不可用（LEDGER/RUNS 关）时读进程内那一条（同一份 blob，不并存两套状态）。

    另一条不变式（证据）也过**同一个** owner：没有承诺型步骤、只有出话步骤的计划，
    同样要过 :func:`plan_completion` 的 :func:`~core.contracts.content_plan.evidence_verdict`
    ——"一步取数都没有"时不得视为完成（主人标注 No.10/11），出口同样是上报。
    """
    mode = exec_receipt_mode()
    if mode == features.MODE_OFF:
        return None
    ctx = dict(context or {})
    promises = promise_steps(plan)
    # **证据不变式先算**（主人标注 No.10/11）：它判的是"这个计划有没有来源"，
    # 与"有没有承诺"是两件事——一个全是出话步骤的计划（没有承诺型步骤）恰恰是最需要
    # 问一句"你凭什么这么说"的那一格，不能被下面那个 `没有承诺 → return None` 短路掉。
    # 判据仍只有一处：``plan_completion``（同一个 owner，两条不变式一次判出）。
    if not _steps_of(plan):
        # 一步都没有 = 没有计划：不做判定（既有出口逐字节退回）。
        return None
    if not promises:
        verdict = plan_completion(plan, ())
        if verdict.complete:
            # 没有"要替主人做事"的步骤：回执层不管（off 与 on 行为一致）。
            return None
        log.warning(
            "plan_completion_denied",
            exit="settle_plan_execution",
            reason="no_evidence_step",
            missing_evidence=list(verdict.missing_evidence),
        )
        return verdict if receipt_enforcing() else None
    chat_id = str(ctx.get("chat_id") or ctx.get("session_id") or "")
    if not chat_id:
        log.info("plan_receipt_skipped", reason="no_chat_id")
        return None
    fill_id = fill_id_for(chat_id, plan_action_id(plan))
    authoritative = _ledger_authoritative(repo)
    if authoritative:
        fill = load(repo, fill_id)
    else:
        pending = _PENDING if cache is None else cache
        fill = pending.get(fill_id)
    if fill is None:
        # 承诺型步骤 + 没有任何 N5 待办行 = **这条承诺根本没有回执行**
        # （状态机没接管 / 台账没落库 / 跨进程丢了）→ 不许当完成（下面按"没有回执"判）。
        log.warning(
            "plan_receipt_missing_fill",
            fill_id=fill_id,
            chat_id=chat_id,
            promises=[step_tool(step) for step in promises],
        )
    else:
        results = [_step_result(step) for step in promises]
        if fill.state == EXECUTING and not fill.has_receipt():
            machine = SlotFillMachine(
                fill,
                repo=repo,
                anchor=_anchor_from_context(ctx, fill),
                now=now,
            )
            failures = [item for item in results if not item["ok"]]
            if failures:
                machine.fail(
                    reason="；".join(
                        f"{item['tool']}：{item['reason']}" for item in failures
                    ),
                    steps=results,
                )
            else:
                machine.complete(
                    outcome="sent",
                    delivered_external_msg_id=next(
                        (
                            item["external_msg_id"]
                            for item in results
                            if item["external_msg_id"]
                        ),
                        "",
                    ),
                    steps=results,
                )
            fill = machine.fill
    receipts: tuple[ExecutionReceipt, ...] = ()
    if authoritative:
        receipt = load_receipt(repo, fill_id)          # 台账 = 权威（查不到就是没有）
    else:
        receipt = receipt_from_fill(fill) if fill is not None else None
    if receipt is not None:
        receipts = (receipt,)
    verdict = plan_completion(plan, receipts)
    log.info(
        "plan_completion_checked",
        mode=mode,
        fill_id=fill_id,
        state=str(getattr(fill, "state", "") or ""),
        has_receipt=bool(receipt),
        receipt_outcome=str(getattr(receipt, "outcome", "") or ""),
        complete=verdict.complete,
        missing=list(verdict.missing),
        missing_tools=list(verdict.missing_tools),
        over_budget=bool(verdict.over_budget),
    )
    if not receipt_enforcing():
        # shadow：收口 + 落库 + 留痕，但**不拿结论改这一轮对外说的话**（读数模式）。
        return None
    return verdict


# ------------------------------------------------------------------- 接线入口
def fill_from_plan(plan: Any, *, chat_id: str = "", requester: str = "",
                   thread_id: str = "", now: datetime | None = None) -> SlotFill | None:
    """计划 → 一条 ``SlotFill``（``None`` = 这个计划不接管：没有可定义的槽位）。"""
    subtasks = list(getattr(plan, "subtasks", None) or [])
    tools: list[str] = []
    args: dict[str, Any] = {}
    goals: list[str] = []
    for task in subtasks:
        tool = str(getattr(task, "tool", "") or "")
        if not tool:
            continue
        tools.append(tool)
        raw_args = getattr(task, "tool_args", None) or {}
        if isinstance(raw_args, Mapping):
            for key, value in raw_args.items():
                args.setdefault(key, value)
        description = str(getattr(task, "description", "") or "").strip()
        if description:
            goals.append(description)
    slots = slots_for_tools(tools, args=args)
    if not slots:
        return None
    data = SlotFillInput(
        chat_id=str(chat_id or ""),
        requester_person_id=str(requester or ""),
        # 计划身份只此一处推导（`plan_action_id`）：回执侧按同一个 id 找得到这条待办。
        action_id=plan_action_id(plan),
        spec_kind="act",
        goal="；".join(goals)[:200],
        open_slots=slots,
        thread_id=str(thread_id or ""),
    )
    return new_fill(data, now=now)


def load(repo: Any, fill_id: str) -> SlotFill | None:
    """从台账读回一条 fill（跨轮 / 跨进程的**权威**来源）。"""
    data = task_store.load_slot_fill(repo, fill_id)
    if not data:
        return None
    return SlotFill.from_dict(data)


def gate_plan_confirmation(
    *,
    plan: Any,
    incoming_text: str,
    context: Mapping[str, Any] | None = None,
    repo: Any = None,
    cache: MutableMapping[str, SlotFill] | None = None,
    now: datetime | None = None,
) -> str | None:
    """``agents/conversational.py`` 那一格确认逻辑的唯一入口。

    返回值（调用方契约，见 :data:`GATE_RETURN_DOC`）：

    * ``None`` —— 状态机**没接管**（``off`` / ``shadow`` / 没有槽位定义 / 台账不可用），
      调用方照旧说「我准备这样做：…可以继续吗？」；
    * ``""`` —— **确认通过**（引用对得上），调用方**落到既有执行循环**（只执行一次）；
    * 非空文本 —— 问 / 重新问 / 已经办过 / 过期作废：调用方**只回这句、不执行**。
    """
    mode = slot_fill_mode()
    if mode == features.MODE_OFF:
        return None          # 回滚位：连本模块都不往下走
    ctx = dict(context or {})
    chat_id = str(ctx.get("chat_id") or ctx.get("session_id") or "")
    thread_id = str(ctx.get("thread_id") or "")
    if plan is None or not chat_id:
        return None
    pending = _PENDING if cache is None else cache
    fill = _load_or_none(repo, pending, plan, chat_id=chat_id,
                         thread_id=thread_id, now=now)
    if fill is None:
        fill = fill_from_plan(
            plan,
            chat_id=chat_id,
            requester=str(
                ctx.get("sender_person_id") or ctx.get("requester_person_id") or ""
            ),
            thread_id=thread_id,
            now=now,
        )
    if fill is None:
        # 认不出来的毁灭性工具：不接管（**不猜参数**）。
        log.info("slot_fill_gate_skipped", reason="no_slot_definition", chat_id=chat_id)
        return None
    if not fill.work_item_id:
        fill.work_item_id = task_store.slot_fill_work_item_id(fill.fill_id)
    incoming = incoming_from_text(
        incoming_text,
        message_ref=str(ctx.get("msg_id") or ctx.get("event_id") or ""),
        thread_id=thread_id,
        quoted_msg_id=str(ctx.get("quoted_msg_id") or ""),
        values=dict(ctx.get("slot_values") or {}),
        at=_iso(now or datetime.now()),
    )
    machine = SlotFillMachine(
        fill,
        open_fills=task_store.list_slot_fills(repo, chat_id=chat_id),
        repo=repo,
        anchor=_anchor_from_context(ctx, fill),
        now=now,
    )
    outcome = machine.advance(incoming)
    _remember(pending, fill)
    log.info(
        "slot_fill_gate",
        mode=mode,
        fill_id=fill.fill_id,
        state=fill.state,
        action=outcome.action,
        reason=outcome.reason,
        nudges=fill.nudges,
    )
    if not enforcing():
        # shadow：判定 + 落库 + 留痕，**对外文本一个字不改**（行为与 off 一致）。
        return None
    if outcome.action == "execute":
        return ""
    return outcome.text or None


#: 调用方契约（``agents/conversational.py`` 的那一格按这个写）。
GATE_RETURN_DOC = "None=不接管 / ''=确认通过（落到执行循环）/ 非空=只回这句"


#: 进程内待办缓存（**只在台账不可用时兜底**：LEDGER 关 / 取不到 meta engine）。
#: 台账可用时读回一律以台账行为权威（:func:`_load_or_none` 的次序），所以这里不会
#: 长成"第二个库"；有界 64 条，测试用 :func:`clear_pending` 清场。
_PENDING: dict[str, SlotFill] = {}


def clear_pending() -> None:
    """清空进程内待办缓存（测试与诊断用；台账行不受影响）。"""
    _PENDING.clear()


def _load_or_none(
    repo: Any,
    cache: MutableMapping[str, SlotFill] | None,
    plan: Any,
    *,
    chat_id: str,
    thread_id: str,
    now: datetime | None,
) -> SlotFill | None:
    """跨轮状态读回：**台账优先**（权威）；台账不可用（LEDGER 关 / 取不到 engine）
    才退回进程内缓存——两条路**不并存**，免得同一个 fill 有两个说法。"""
    if plan is None:
        return None
    fresh = fill_from_plan(plan, chat_id=chat_id, thread_id=thread_id, now=now)
    if fresh is None:
        return None
    authoritative = bool(task_store.ledger_enabled()) and (
        task_store.resolve_engine(repo) is not None
    )
    if not authoritative:
        return cache[fresh.fill_id] if cache and fresh.fill_id in cache else fresh
    stored = load(repo, fresh.fill_id)
    if stored is not None:
        # 计划里新带全的参数补进去（计划变了、但同一条待办仍在收集）
        for slot in fresh.slots:
            target = stored.slot(slot.name)
            if target is None:
                stored.slots.append(slot)
                continue
            if not target.is_filled() and slot.is_filled() and slot.source == "plan":
                target.value = slot.value
                target.source = "plan"
                target.evidence = slot.evidence
        if not stored.goal:
            stored.goal = fresh.goal
        return stored
    return fresh


def _remember(cache: MutableMapping[str, SlotFill] | None, fill: SlotFill) -> None:
    target = _PENDING if cache is None else cache
    target[fill.fill_id] = fill
    while len(target) > 64:                    # 有界缓存，别长成第二个库
        target.pop(next(iter(target)))


def _anchor_from_context(ctx: Mapping[str, Any], fill: SlotFill) -> dict[str, Any]:
    """台账头锚点（复用 ``store._creation_anchors`` 的同一套键名）。"""
    return {
        "chat_id": str(ctx.get("chat_id") or ""),
        "platform": str(ctx.get("platform") or ""),
        "account_id": str(ctx.get("account_id") or ""),
        "session_id": str(ctx.get("session_id") or ""),
        "thread_id": str(ctx.get("thread_id") or fill.thread_id or ""),
        "turn_id": str(ctx.get("turn_id") or ""),
        "sender_account_id": str(ctx.get("sender_account_id") or ""),
        "external_msg_id": str(ctx.get("msg_id") or ""),
        "requester_person_id": fill.requester_person_id,
        "is_group": bool(ctx.get("is_group")),
        "is_owner": bool(ctx.get("is_owner") or ctx.get("requester_is_owner")),
        "owner_only": bool(ctx.get("owner_only")),
    }


# ------------------------------------------------------------------- 小工具
def _type_problem(slot: Slot) -> str:
    """按 ``expected_type`` 的**粗粒度**类型判据（B5 的 kind 级 schema 不在本期）。"""
    value = slot.value
    kind = str(slot.expected_type or "").lower()
    if kind in ("时间", "time"):
        return "" if str(value).strip() else slot.hint()
    if kind in ("int", "整数", "数字", "count"):
        try:
            int(str(value).strip())
        except (TypeError, ValueError):
            warn("core/tasks/slot_fill.py:2174 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            return f"「{slot.name}」得是个数字，例如「{slot.expected_example or '3'}」"
        return ""
    if kind in ("收件人", "target"):
        return "" if str(value).strip() else slot.hint()
    return ""


def _labelled_value(body: str, key: str) -> str:
    """``时间：明天早上8点`` → ``明天早上8点``（**不是**引用正则，就是字段分隔）。"""
    for separator in ("：", ":", "="):
        marker = f"{key}{separator}"
        index = body.find(marker)
        if index < 0:
            continue
        tail = body[index + len(marker):]
        for stop in ("\n", "，", ",", "；", ";"):
            cut = tail.find(stop)
            if cut >= 0:
                tail = tail[:cut]
        value = tail.strip()
        if value:
            return value
    return ""


def _split_segments(body: str) -> list[str]:
    """按中文/英文分隔符切段（"一次问齐、按序回答"的分发单位）。"""
    chunks: list[str] = []
    current = ""
    for char in str(body or ""):
        if char in "，,、;；\n":
            if current.strip():
                chunks.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _strip_punct(text: str) -> str:
    return "".join(
        char for char in str(text or "")
        if not char.isspace()
        and char not in "。，、！？!?,.;:：；~～…\"'“”‘’（）()[]【】#"
    )


def _iso(stamp: datetime | None) -> str:
    return stamp.isoformat() if isinstance(stamp, datetime) else ""


def _parse_iso(text: Any) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        warn("core/tasks/slot_fill.py:2234 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return None


def describe_fill(fill: SlotFill) -> str:
    """诊断用的一行摘要（不参与判定）。"""
    return json.dumps(
        {
            "fill_id": fill.fill_id,
            "state": fill.state,
            "pending": list(fill.pending_slots),
            "asked": list(fill.asked_batch),
            "confirm_ref": fill.confirm_ref,
            "nudges": fill.nudges,
            "transitions": len(fill.transitions),
        },
        ensure_ascii=False,
    )
