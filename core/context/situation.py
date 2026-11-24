# -*- coding: utf-8 -*-
"""世界模型中枢（v2 施工图 Step 2.5）：事件溯源 + BDI 投影。

只追加事件，不直接改状态；当前状态 = project(events)。
N1 每轮提交的 world_updates 由 apply_world_updates 落成事件；
本模块纯函数、无 IO、无 LLM。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Mapping
from safelog import warn

__all__ = [
    "Belief", "EmotionState", "Intention", "MessageObservation",
    "PerceptionObservation", "Projection", "WorldEvent", "WorldModel",
    "PERCEPTION_QUOTE_MAX", "PERCEPTION_TAIL_MAX", "PERCEPTION_TEXT_MAX",
    "apply_scene_read", "apply_world_updates", "assert_unknown", "close_intention",
    "confirm_hypothesis", "contradict_hypothesis",
    "due_intentions", "note_emotion", "note_perception",
    "observe_message",
    "open_intention", "open_intentions", "note_evidence", "project", "recent_messages",
    "recent_perceptions", "render_situation",
    "Episode", "EPISODE_TAIL_MAX", "close_episode", "current_episode",
    "open_episode", "touch_episode",
    "SelfMessage", "SELF_MESSAGE_TAIL_MAX", "note_self_message",
    "Meme", "MEME_TAIL_MAX", "note_meme",
]

EVENT_KINDS = frozenset({
    "message_observed", "perception_observed", "fact_asserted",
    "hypothesis_formed", "hypothesis_confirmed", "hypothesis_contradicted",
    "fact_revoked", "intention_opened", "intention_closed", "intention_evidence",
    "goal_imported", "goal_reviewed", "goal_signal",
    "position_observed", "emotion_observed",
    "episode_opened", "episode_touched", "episode_closed",
    "self_message",
    "meme_observed",
})
SOURCES = frozenset({"chat_observed", "self_reported", "inferred", "unknown"})

#: ①感知消息账每会话尾部上限：更早的原文在终端库/⑨，②只保最近这些条。
PERCEPTION_TAIL_MAX = 200
#: 单条原文上限：正常聊天远低于此；只有病态长文才截断并标记（不丢字段）。
PERCEPTION_TEXT_MAX = 4000
#: 引用原话入账上限（引用正文只作证据，不替代原文）。
PERCEPTION_QUOTE_MAX = 200
#: ② 里保留的片段尾巴：更早的关闭片段只留在事件流/审计日志。
EPISODE_TAIL_MAX = 20
#: episode_touched 事件尾巴（同一幕的成员/条数刷新，防膨胀）。
EPISODE_TOUCH_TAIL_MAX = 200
#: ② 里她说过的话的尾巴（供"她刚说过什么"类读取；批D Step 36）。
SELF_MESSAGE_TAIL_MAX = 50
#: 单条自我消息文本上限（超限截断并带省略号）。
SELF_MESSAGE_TEXT_MAX = 500

#: ② 里群体记忆（梗）的尾部上限；圈定/命中才启用。
MEME_TAIL_MAX = 100


@dataclass(frozen=True)
class Meme:
    """群体记忆（梗）条目：同一个梗只留一条，按次数累加。"""

    key: str
    kind: str = ""
    count: int = 1
    evidence: str = ""
    first_at: str = ""
    last_at: str = ""



@dataclass(frozen=True)
class WorldEvent:
    """不可变事件；只追加，不修改。"""

    kind: str
    at: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Belief:
    """一条世界信条（事实或假说）。

    P2 起补齐方案 §6 的字段（全部带默认值，旧事件读出来不受影响）：
    ``valid_from`` / ``valid_to`` 区分「时间变化」与「纠错」——搬家了是前者
    （旧地址仍 as_of 某日为真），之前记错了是后者（旧值从未为真）。
    ``supersedes`` 指向被替代的 belief key；来源冲突两条并存，不自动覆盖。
    """

    key: str
    value: str = ""
    source: str = "unknown"
    confidence: float = 0.0
    as_of: str = ""
    status: str = "active"
    #: 谁说的（2026-10-13）：群里显示的名字；"群里还没说/不知道"为空串。
    who: str = ""
    # ---- 方案 §6 目标契约 ----
    subject_key: str = ""
    predicate: str = ""
    evidence_refs: tuple[str, ...] = ()
    scope: str = ""
    valid_from: str = ""
    valid_to: str = ""
    supersedes: str = ""


@dataclass(frozen=True)
class Intention:
    """未完成的意图：待回应 / 待观察 / 待出手。

    P3 起同时是**目标真源**：``key`` 就是 ``goal_id``（保留旧名兼容读写）。
    ``origin`` 区分授权来源（用户要求 / 自身承诺 / 自选兴趣 / 运行维护），
    不枚举开放世界里的任务类型；``state`` 是 proposed/active/waiting/paused/
    completed/abandoned，``status`` 仅为旧读侧保留 open/closed 两态。
    """

    key: str
    kind: str = "observe"
    status: str = "open"
    opened_at: str = ""
    closed_at: str = ""
    evidence: str = ""
    due_at: str = ""
    condition: str = ""
    result: str = ""
    # ---- 目标契约（方案 §8）----
    origin: str = ""
    motivation: str = ""
    desired_outcome: str = ""
    done_when: str = ""
    next_action: str = ""
    evidence_refs: tuple[str, ...] = ()
    scope: str = ""
    state: str = "active"
    review_at: str = ""
    blocked_on: str = ""
    parent_id: str = ""
    revision: int = 1

    @property
    def goal_id(self) -> str:
        return self.key


@dataclass(frozen=True)
class MessageObservation:
    """一轮消息的归属观测：谁 → 对谁（集合）→ 说了什么（Step 17）。

    ``text`` 是截断后的短文本（≤120 字），只用于"谁说的什么"，不做全文存储。
    """

    who: str = ""
    targets: tuple[str, ...] = ()
    text: str = ""
    genre: str = ""
    topic: str = ""
    at: str = ""


@dataclass(frozen=True)
class PerceptionObservation:
    """①感知的一条原始消息账（批 A4）。

    与 ③理解写出的 :class:`MessageObservation` 分账：这里是**原文 + 结构字段**
    （谁/几点/说什么/拍/撤回/媒体/父链/提及），不含模型判断的 targets/genre。
    ``text`` 保留原文（仅去首尾空白；超 ``PERCEPTION_TEXT_MAX`` 才截断并标记）。
    """

    who: str = ""
    who_id: str = ""
    text: str = ""
    at: str = ""
    kind: str = "text"          # text / pat / revoke / media / system
    msg_type: str = ""
    msg_id: str = ""
    reply_to: str = ""
    quote_user: str = ""
    quote_text: str = ""
    mentions: tuple[str, ...] = ()
    media: Mapping[str, Any] = field(default_factory=dict)
    platform: str = ""
    account_id: str = ""
    text_len: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class EmotionState:
    """②世界模型的情绪姿态投影（新架构批 A1）。

    与 ``core.contracts.affect.AffectState`` 同轴：``level`` 从 -1.0（被冷落）
    到 +1.0（被重视），0.0 是稳态基线；``reason`` 是原因键而非句子；
    衰减与积分由④情绪模块负责，②只记账。
    """

    level: float = 0.0
    updated_at: str = ""
    reason: str = ""
    #: 这份情绪属于谁（显示名 + 稳定 id；批 A2 按人记录，避免重名歧义）。
    who: str = ""
    who_id: str = ""


@dataclass(frozen=True)
class Episode:
    """② 的「这一幕」投影（批D Step 33：片段是一等对象）。

    episode_id 由片段起点（第一条消息 key）派生，片段长大时保持稳定；
    members 只记人（不含她自己）；更早的关闭片段只保留 EPISODE_TAIL_MAX 条。
    """

    episode_id: str = ""
    status: str = "open"
    members: tuple[str, ...] = ()
    start_at: str = ""
    end_at: str = ""
    message_count: int = 0
    topic: str = ""


@dataclass(frozen=True)
class SelfMessage:
    """② 的「她说过的话」投影（批D Step 36）。

    只记内容/时间/形态；语义加工留给读它的人。更早的只保留 SELF_MESSAGE_TAIL_MAX 条。
    """

    content: str = ""
    at: str = ""
    kind: str = "reply"


@dataclass(frozen=True)
class Projection:
    """事件流的当前投影。"""

    beliefs: Mapping[str, Belief]
    intentions: Mapping[str, Intention]
    desires: Mapping[str, str]
    messages: tuple[MessageObservation, ...] = ()
    perceptions: tuple[PerceptionObservation, ...] = ()
    emotion: EmotionState = field(default_factory=EmotionState)
    episodes: tuple[Episode, ...] = ()
    self_messages: tuple[SelfMessage, ...] = ()
    memes: tuple[Meme, ...] = ()


@dataclass
class WorldModel:
    """只追加的事件流。"""

    events: list[WorldEvent] = field(default_factory=list)
    #: 会话坐标（platform/account_id/chat_id/chat_type）。首次绑定后不随事件变化；
    #: 空 dict = 旧模型/未绑定，调用方不得据此写跨会话状态。
    scope: dict[str, str] = field(default_factory=dict)

    def add(self, kind: str, payload: Mapping[str, Any] | None = None, at: str = "") -> "WorldModel":
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown world event kind: {kind!r}")
        self.events.append(WorldEvent(kind=kind, at=at, payload=dict(payload or {})))
        return self


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _raw_text(value: Any, limit: int) -> str:
    """原文保真：只去首尾空白，不折叠内部换行；超限才截断并带省略号。"""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _affect_level(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(-1.0, min(1.0, float(value)))


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def apply_world_updates(
    model: WorldModel,
    updates: Mapping[str, Any] | None,
    *,
    at: str = "",
) -> WorldModel:
    """N1 的 world_updates 事务 -> 事件。

    observe -> fact_asserted；abduce -> hypothesis_formed；
    retract -> fact_revoked。缺字段 / 空 key 不落事件，不猜。
    """
    data = updates or {}
    for item in data.get("observe") or ():
        if not isinstance(item, Mapping):
            continue
        key = _text(item.get("key"))
        if not key:
            continue
        source = _text(item.get("source")) or "chat_observed"
        if source not in SOURCES:
            source = "unknown"
        assert_fact(model, key, value=_text(item.get("value")), at=at,
                    source=source, who=_text(item.get("speaker")),
                    subject_key=_text(item.get("subject_key")), predicate=_text(item.get("predicate")),
                    evidence_refs=item.get("evidence_refs") or (), scope=_text(item.get("scope")),
                    valid_from=_text(item.get("valid_from")), valid_to=_text(item.get("valid_to")),
                    supersedes=_text(item.get("supersedes")), change_kind=_text(item.get("change_kind")) or "correction")
    for item in data.get("abduce") or ():
        if not isinstance(item, Mapping):
            continue
        key = _text(item.get("key"))
        if not key:
            continue
        model.add("hypothesis_formed", {
            "key": key,
            "value": _text(item.get("value")),
            "confidence": _num(item.get("confidence")),
            "evidence": _text(item.get("evidence")),
            "who": _text(item.get("speaker")),
        }, at=at)
    for item in data.get("retract") or ():
        if not isinstance(item, Mapping):
            continue
        key = _text(item.get("key"))
        if not key:
            continue
        model.add("fact_revoked", {
            "key": key,
            "reason": _text(item.get("reason")),
        }, at=at)
    return model


def apply_scene_read(
    model: WorldModel,
    section: Mapping[str, Any] | None,
    *,
    at: str = "",
) -> WorldModel:
    """scene_read_of 的返回段（含 updates 事务）-> 世界模型事件。"""
    items = section.get("updates") if isinstance(section, Mapping) else None
    tx: dict[str, list[Any]] = {"observe": [], "abduce": [], "retract": []}
    for item in items or ():
        if not isinstance(item, Mapping):
            continue
        op = _text(item.get("op"))
        if op in tx:
            tx[op].append(item)
    return apply_world_updates(model, tx, at=at)


def assert_unknown(model: WorldModel, key: str, *, at: str = "") -> WorldModel:
    """显式登记「群里还没说」，不是缺失字段。"""
    model.add("fact_asserted", {"key": _text(key), "value": "", "source": "unknown"}, at=at)
    return model


def assert_fact(
    model: WorldModel,
    key: str,
    *,
    value: str = "",
    at: str = "",
    who: str = "",
    source: str = "",
    subject_key: str = "",
    predicate: str = "",
    evidence_refs: Iterable[str] = (),
    scope: str = "",
    valid_from: str = "",
    valid_to: str = "",
    supersedes: str = "",
    change_kind: str = "correction",
) -> WorldModel:
    """断言一条事实；带 supersedes 时旧条目标为 superseded（不是删除）。

    「搬家了」和「之前记错了」走同一条路但语义不同：前者给 ``valid_from``
    （旧地址在某个日期前仍为真），后者给 ``supersedes``（旧值从未为真）。
    投影里被 supersede 的旧条目保留 ``status="superseded"``，历史可查。
    """
    for boundary in (valid_from, valid_to, (valid_from or at) if change_kind == "time_change" else ""):
        if boundary:
            parsed = datetime.fromisoformat(boundary)
            if parsed.tzinfo is not None:
                raise ValueError("belief_time_requires_local_clock")
    if change_kind not in {"correction", "time_change"}:
        raise ValueError("unknown_belief_change_kind")
    if supersedes and supersedes != key:
        prior = project(model.events).beliefs.get(supersedes)
        if prior is None:
            raise ValueError("belief_supersedes_missing")
        if any(left and right and left != right for left, right in ((prior.subject_key, subject_key), (prior.predicate, predicate))):
            raise ValueError("belief_supersedes_subject_mismatch")
    payload: dict[str, Any] = {
        "key": _text(key),
        "value": _text(value),
        "who": _text(who),
    }
    if _text(source) and _text(source) in SOURCES:
        payload["source"] = _text(source)
    optional = {
        "subject_key": _text(subject_key),
        "predicate": _text(predicate),
        "scope": _text(scope),
        "valid_from": _text(valid_from),
        "change_kind": change_kind,
        "valid_to": _text(valid_to),
        # 自己 supersede 自己等于没做——写进去只会让投影自引用。
        "supersedes": _text(supersedes) if _text(supersedes) != _text(key) else "",
    }
    payload.update({name: item for name, item in optional.items() if item})
    refs = [_text(item) for item in evidence_refs if _text(item)]
    if refs:
        payload["evidence_refs"] = refs
    model.add("fact_asserted", payload, at=at)
    return model


def confirm_hypothesis(model: WorldModel, key: str, *, at: str = "", evidence: str = "") -> WorldModel:
    model.add("hypothesis_confirmed", {"key": _text(key), "evidence": _text(evidence)}, at=at)
    return model


def contradict_hypothesis(model: WorldModel, key: str, *, at: str = "", reason: str = "") -> WorldModel:
    model.add("hypothesis_contradicted", {"key": _text(key), "reason": _text(reason)}, at=at)
    return model


def note_meme(model: WorldModel, *, key: str, kind: str = "", count: int = 1, evidence: str = "", at: str = "") -> WorldModel:
    """群体记忆（梗）入账：同 key 由投影合并计数；纯事实，无 LLM。"""
    model.add("meme_observed", {
        "key": _text(key),
        "kind": _text(kind),
        "count": int(count or 1),
        "evidence": _text(evidence),
    }, at=at)
    return model


def note_position(model: WorldModel, key: str, value: str, *, at: str = "") -> WorldModel:
    """关系证据进 Position/Desires 投影（谁维护过/谁起哄过）。"""
    model.add("position_observed", {"key": _text(key), "value": _text(value)}, at=at)
    return model


def open_intention(
    model: WorldModel,
    key: str,
    *,
    kind: str = "observe",
    at: str = "",
    evidence: str = "",
    due_at: str = "",
    condition: str = "",
    origin: str = "",
    motivation: str = "",
    desired_outcome: str = "",
    done_when: str = "",
    next_action: str = "",
    evidence_refs: Iterable[str] = (),
    scope: str = "",
    review_at: str = "",
    blocked_on: str = "",
    parent_id: str = "",
    state: str = "",
) -> WorldModel:
    payload: dict[str, Any] = {
        "key": _text(key),
        "kind": _text(kind) or "observe",
        "evidence": _text(evidence),
        "due_at": _text(due_at),
        "condition": _text(condition),
    }
    optional = {
        "origin": _text(origin),
        "motivation": _text(motivation),
        "desired_outcome": _text(desired_outcome),
        "done_when": _text(done_when),
        "next_action": _text(next_action),
        "scope": _text(scope),
        "review_at": _text(review_at),
        "blocked_on": _text(blocked_on),
        "parent_id": _text(parent_id),
    }
    payload.update({name: value for name, value in optional.items() if value})
    refs = [_text(item) for item in evidence_refs if _text(item)]
    if refs:
        payload["evidence_refs"] = refs
    if _text(state):
        payload["state"] = _text(state)
    model.add("intention_opened", payload, at=at)
    return model


#: 一件办着的事最多留多少条过程证据（先到先留，超了丢最旧的）。
_MAX_EVIDENCE_REFS = 32


def note_evidence(
    model: WorldModel,
    key: str,
    ref: str,
    *,
    at: str = "",
    source: str = "",
    detail: str = "",
) -> bool:
    """给一件还在办的事补一条**过程证据**（产物路径 / 队列号 / 回执号）。

    2026-11-18 D-3：证据引用原先只在 :func:`open_intention` 那一刻写死，运行期
    真拿到的 ``artifact:`` / ``queue:`` / ``ack:`` 无处可去，最终报告于是永远只会
    说「尚未取得证据：产物保存、投递动作、本机观察、平台送达」。这里只追加引用，
    不改目标别的字段；同一条引用重复上报返回 False（幂等，重试不会把台账撑大）。
    """
    goal = _text(key)
    reference = _text(ref)
    if not goal or not reference:
        return False
    current = project(model.events).intentions.get(goal)
    if current is None or str(current.status) != "open":
        return False
    refs = [item for item in (current.evidence_refs or ()) if item]
    if reference in refs:
        return False
    refs.append(reference)
    if len(refs) > _MAX_EVIDENCE_REFS:
        refs = refs[-_MAX_EVIDENCE_REFS:]
    model.add(
        "intention_evidence",
        {
            "key": goal,
            "ref": reference,
            "evidence_refs": refs,
            "source": _text(source),
            "detail": _raw_text(detail, 200),
        },
        at=at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    return True


def close_intention(
    model: WorldModel,
    key: str,
    *,
    at: str = "",
    result: str = "",
    state: str = "",
) -> WorldModel:
    payload: dict[str, Any] = {"key": _text(key), "result": _text(result)}
    if _text(state):
        payload["state"] = _text(state)
    model.add("intention_closed", payload, at=at)
    return model


def open_intentions(model: WorldModel, *, kind: str = "") -> tuple[Intention, ...]:
    """当前未关闭的意图（可按 kind 过滤）。"""
    view = project(model.events)
    items = [item for item in view.intentions.values() if item.status == "open"]
    if kind:
        items = [item for item in items if item.kind == kind]
    return tuple(items)


def due_intentions(model: WorldModel, now: str) -> tuple[Intention, ...]:
    """到点、未关闭的延时意图（remind/notify/act）。"""
    now_text = _text(now)
    if not now_text:
        return ()
    out = []
    for item in open_intentions(model):
        if item.kind not in ("remind", "notify", "act"):
            continue
        if item.due_at and item.due_at <= now_text:
            out.append(item)
    return tuple(out)


def project(events: Iterable[WorldEvent]) -> Projection:
    """事件流 -> Beliefs / Intentions 当前投影。"""
    beliefs: dict[str, Belief] = {}
    intentions: dict[str, Intention] = {}
    desires: dict[str, str] = {}
    messages: list[MessageObservation] = []
    perceptions: list[PerceptionObservation] = []
    emotion = EmotionState()
    episodes: list[Episode] = []
    self_messages: list[SelfMessage] = []
    memes: dict[str, Meme] = {}
    for event in events or ():
        kind = event.kind
        payload = event.payload or {}
        key = _text(payload.get("key"))
        if kind == "fact_asserted" and key:
            source = _text(payload.get("source")) or "chat_observed"
            raw_refs = payload.get("evidence_refs") or ()
            if isinstance(raw_refs, str):
                raw_refs = (raw_refs,)
            beliefs[key] = Belief(
                key=key, value=_text(payload.get("value")), source=source,
                confidence=0.0 if source == "inferred" else 1.0,
                as_of=event.at,
                status="unknown" if source == "unknown" else "active",
                who=_text(payload.get("who")),
                subject_key=_text(payload.get("subject_key")),
                predicate=_text(payload.get("predicate")),
                evidence_refs=tuple(
                    item for item in (_text(entry) for entry in raw_refs) if item
                ),
                scope=_text(payload.get("scope")),
                valid_from=_text(payload.get("valid_from")),
                valid_to=_text(payload.get("valid_to")),
                supersedes=_text(payload.get("supersedes")),
            )
            # 显式更正：旧 belief 保留（可查历史），新条目指回被替代者。
            prior_key = _text(payload.get("supersedes"))
            if prior_key and prior_key in beliefs and prior_key != key:
                prior = beliefs[prior_key]
                beliefs[prior_key] = replace(
                    prior, status="superseded", valid_to=event.at,
                )
        elif kind == "hypothesis_formed" and key:
            beliefs[key] = Belief(
                key=key, value=_text(payload.get("value")), source="inferred",
                confidence=_num(payload.get("confidence")), as_of=event.at,
                status="pending",
                who=_text(payload.get("who")),
            )
        elif kind == "hypothesis_confirmed" and key and key in beliefs:
            old = beliefs[key]
            beliefs[key] = replace(
                old, status="active", source="inferred",
                confidence=max(old.confidence, 0.5), as_of=event.at,
            )
        elif kind == "hypothesis_contradicted" and key and key in beliefs:
            beliefs[key] = replace(
                beliefs[key], status="revoked", as_of=event.at, valid_to=event.at,
            )
        elif kind == "fact_revoked" and key and key in beliefs:
            beliefs[key] = replace(
                beliefs[key], status="revoked", as_of=event.at, valid_to=event.at,
            )
        elif kind == "intention_opened" and key:
            raw_refs = payload.get("evidence_refs") or ()
            if isinstance(raw_refs, str):
                raw_refs = (raw_refs,)
            intentions[key] = Intention(
                key=key, kind=_text(payload.get("kind")) or "observe",
                status="open", opened_at=event.at,
                evidence=_text(payload.get("evidence")),
                due_at=_text(payload.get("due_at")),
                condition=_text(payload.get("condition")),
                origin=_text(payload.get("origin")),
                motivation=_text(payload.get("motivation")),
                desired_outcome=_text(payload.get("desired_outcome")),
                done_when=_text(payload.get("done_when")),
                next_action=_text(payload.get("next_action")),
                evidence_refs=tuple(
                    item for item in (_text(entry) for entry in raw_refs) if item
                ),
                scope=_text(payload.get("scope")),
                state=_text(payload.get("state")) or "active",
                review_at=_text(payload.get("review_at")),
                blocked_on=_text(payload.get("blocked_on")),
                parent_id=_text(payload.get("parent_id")),
                revision=1,
            )
        elif kind == "intention_evidence" and key and key in intentions:
            current = intentions[key]
            raw_refs = payload.get("evidence_refs") or ()
            if isinstance(raw_refs, str):
                raw_refs = (raw_refs,)
            refs = tuple(item for item in (_text(entry) for entry in raw_refs) if item)
            if refs:
                intentions[key] = replace(
                    current,
                    evidence_refs=refs,
                    revision=int(current.revision or 0) + 1,
                )
        elif kind == "intention_closed" and key and key in intentions:
            current = intentions[key]
            result = _text(payload.get("result"))
            terminal = _text(payload.get("state"))
            if not terminal:
                terminal = (
                    "abandoned"
                    if result.startswith(("expired", "abandoned", "gave_up"))
                    else "completed"
                )
            intentions[key] = replace(
                current, status="closed", closed_at=event.at,
                result=result, state=terminal,
                revision=int(current.revision or 0) + 1,
            )
        elif kind == "position_observed" and key:
            desires[key] = _text(payload.get("value"))
        elif kind == "emotion_observed":
            emotion = EmotionState(
                level=_affect_level(payload.get("level")),
                updated_at=event.at,
                reason=_text(payload.get("reason")),
                who=_text(payload.get("who")),
                who_id=_text(payload.get("who_id")),
            )
        elif kind in ("episode_opened", "episode_touched", "episode_closed"):
            episode_key = _text(payload.get("episode_id"))
            raw_members = payload.get("members") or ()
            if isinstance(raw_members, str):
                raw_members = (raw_members,)
            member_names = tuple(
                name for name in (_text(item) for item in raw_members) if name
            )
            try:
                count = int(payload.get("message_count") or 0)
            except (TypeError, ValueError):
                warn("core/context/situation.py:542 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
                count = 0
            topic = _text(payload.get("topic"))
            if kind == "episode_opened" and episode_key:
                episodes.append(Episode(
                    episode_id=episode_key, status="open", members=member_names,
                    start_at=event.at, end_at=event.at,
                    message_count=count or 1, topic=topic,
                ))
            elif kind in ("episode_touched", "episode_closed") and episode_key:
                for index in range(len(episodes) - 1, -1, -1):
                    item = episodes[index]
                    if item.episode_id != episode_key or item.status != "open":
                        continue
                    episodes[index] = replace(
                        item,
                        status="closed" if kind == "episode_closed" else item.status,
                        members=member_names or item.members,
                        end_at=event.at,
                        message_count=count or item.message_count,
                        topic=topic or item.topic,
                    )
                    break
        elif kind == "perception_observed":
            raw_mentions = payload.get("mentions") or ()
            if isinstance(raw_mentions, str):
                raw_mentions = (raw_mentions,)
            mentions = tuple(
                item for item in (_text(x) for x in raw_mentions) if item
            )
            raw_media = payload.get("media")
            perceptions.append(PerceptionObservation(
                who=_text(payload.get("who")),
                who_id=_text(payload.get("who_id")),
                text=_raw_text(payload.get("text"), PERCEPTION_TEXT_MAX),
                at=event.at,
                kind=_text(payload.get("kind")) or "text",
                msg_type=_text(payload.get("msg_type")),
                msg_id=_text(payload.get("msg_id")),
                reply_to=_text(payload.get("reply_to")),
                quote_user=_text(payload.get("quote_user")),
                quote_text=_raw_text(payload.get("quote_text"), PERCEPTION_QUOTE_MAX),
                mentions=mentions,
                media=dict(raw_media) if isinstance(raw_media, Mapping) else {},
                platform=_text(payload.get("platform")),
                account_id=_text(payload.get("account_id")),
                text_len=int(payload.get("text_len") or 0),
                truncated=bool(payload.get("truncated")),
            ))
        elif kind == "message_observed":
            raw_targets = payload.get("targets") or ()
            if isinstance(raw_targets, str):
                raw_targets = (raw_targets,)
            targets = tuple(
                name for name in (_text(item) for item in raw_targets) if name
            )
            messages.append(MessageObservation(
                who=_text(payload.get("who")),
                targets=targets,
                text=_text(payload.get("text")),
                genre=_text(payload.get("genre")),
                topic=_text(payload.get("topic")),
                at=event.at,
            ))
        elif kind == "self_message":
            content = _raw_text(payload.get("content"), SELF_MESSAGE_TEXT_MAX)
            if content:
                self_messages.append(SelfMessage(
                    content=content,
                    at=event.at,
                    kind=_text(payload.get("kind")) or "reply",
                ))
        elif kind == "meme_observed" and key:
            memes[key] = Meme(
                key=key,
                kind=_text(payload.get("kind")),
                count=(memes[key].count if key in memes else 0) + int(payload.get("count") or 1),
                evidence=_text(payload.get("evidence")) or (memes[key].evidence if key in memes else ""),
                first_at=(memes[key].first_at if key in memes else event.at),
                last_at=event.at,
            )
    return Projection(
        beliefs=beliefs, intentions=intentions, desires=desires,
        messages=tuple(messages), perceptions=tuple(perceptions),
        emotion=emotion,
        episodes=tuple(episodes[-EPISODE_TAIL_MAX:]),
        self_messages=tuple(self_messages[-SELF_MESSAGE_TAIL_MAX:]),
        memes=tuple(list(memes.values())[-MEME_TAIL_MAX:]),
    )


def render_situation(
    model: WorldModel, *, max_lines: int = 6, now: datetime | None = None,
) -> str:
    """把当前投影渲染成 <= max_lines 行；unknown 写「群里还没说」。

    ``now`` 给了就按真实时间衰减情绪（只读）；缺省用存储值，旧调用逐字不变。
    """
    view = project(model.events)
    lines: list[str] = []
    from core.memory.beliefs import effective_beliefs
    current = effective_beliefs(model, at=now)
    fact_keys = {event.payload.get("key") for event in model.events if event.kind == "fact_asserted"}
    claims = {}
    for item in current:
        claims.setdefault((item.subject_key or item.key, item.predicate), set()).add(item.value)
    extras = tuple(item for item in view.beliefs.values() if item.status in {"pending", "unknown"} or (item.status == "active" and item.key not in fact_keys))
    for belief in (*current, *extras):
        who = f"{belief.who} 说的，" if belief.who else ""
        if belief.status == "active":
            conflict = len(claims.get((belief.subject_key or belief.key, belief.predicate), ())) > 1
            lines.append(("[冲突待核对] " if conflict else "") + f"{belief.key} = {belief.value}（{who}{belief.source}）")
        elif belief.status == "pending":
            lines.append(
                f"{belief.key} 待观察：{belief.value}（{who}置信度 {belief.confidence:.1f}）"
            )
        elif belief.status == "unknown":
            lines.append(f"{belief.key}：群里还没说")
    if view.emotion.updated_at:
        level = view.emotion.level
        reason = view.emotion.reason
        if now is not None:
            try:
                from core.context.emotion import current_emotion

                current = current_emotion(model, now=now)
                level, reason = current.level, current.reason
            except Exception:  # noqa: BLE001 - 衰减读不到就用存储值
                warn("core/context/situation.py:663 Exception", err=repr(__import__("sys").exc_info()[1]))
        who = f"（{view.emotion.who}）" if view.emotion.who else ""
        lines.append(f"情绪{who}：{level:+.1f}（{reason or '稳态'}）")
        try:
            from core.context.emotion import (
                attitude_toward, current_mood, layers_mode,
            )

            if layers_mode() == "on":
                mood = current_mood(model, now=now)
                att = attitude_toward(
                    model, who_id=view.emotion.who_id,
                    who=view.emotion.who, now=now,
                )
                who_name = view.emotion.who or "他"
                lines.append(
                    f"心境：{mood.label}（{mood.level:+.1f}）；"
                    f"对{who_name}态度：{att.label}（{att.level:+.1f}）"
                )
        except Exception:  # noqa: BLE001 - 分层读数失败就少一行
            warn("core/context/situation.py:683 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        from core.context import salience as salience_mod

        if salience_mod.salience_mode() == "on":
            items = salience_mod.top_salient(model.events, now=now)
            if items:
                lines.append(
                    "要紧：" + "；".join(
                        str(item.label or item.kind) for item in items[:3]
                    )
                )
    except Exception:  # noqa: BLE001 - 显著性读数失败就少一行
        warn("core/context/situation.py:696 Exception", err=repr(__import__("sys").exc_info()[1]))
    for intention in view.intentions.values():
        if intention.status == "open":
            label = intention.next_action or intention.desired_outcome or intention.key
            lines.append(f"待办：{intention.kind} {label}")
    return "\n".join(lines[:max_lines])


def note_emotion(
    model: WorldModel,
    *,
    level: float = 0.0,
    reason: str = "",
    who: str = "",
    who_id: str = "",
    at: str = "",
) -> WorldModel:
    """把一轮情绪观测记进②（批 A1/A2；④情绪模块后续会据此做积分/衰减）。

    ``level`` 只接受 -1..+1；坏值按 0（稳态）处理，绝不猜。
    ``who`` 是显示名、``who_id`` 是稳定 id（群聊里同名的人也要分得开）。
    """
    model.add("emotion_observed", {
        "level": _affect_level(level),
        "reason": _text(reason),
        "who": _text(who),
        "who_id": _text(who_id),
    }, at=at)
    return model


def open_episode(
    model: WorldModel,
    *,
    episode_id: str = "",
    at: str = "",
    members: Iterable[Any] = (),
    topic: str = "",
    message_count: int = 1,
) -> WorldModel:
    """② 开一幕（批D Step 33）。episode_id 由片段 owner 派生；② 只记账。"""
    key = _text(episode_id)
    if not key:
        return model
    model.add("episode_opened", {
        "episode_id": key,
        "members": [name for name in (_text(item) for item in members or ()) if name],
        "topic": _text(topic),
        "message_count": int(message_count or 1),
    }, at=at)
    _trim_events(model, "episode_opened", EPISODE_TAIL_MAX * 4)
    return model


def touch_episode(
    model: WorldModel,
    *,
    episode_id: str = "",
    at: str = "",
    members: Iterable[Any] = (),
    message_count: int = 0,
) -> WorldModel:
    """② 刷新当前一幕的成员/条数（只对已开的同一幕生效）。"""
    key = _text(episode_id)
    if not key:
        return model
    model.add("episode_touched", {
        "episode_id": key,
        "members": [name for name in (_text(item) for item in members or ()) if name],
        "message_count": int(message_count or 0),
    }, at=at)
    _trim_events(model, "episode_touched", EPISODE_TOUCH_TAIL_MAX)
    return model


def close_episode(
    model: WorldModel,
    *,
    episode_id: str = "",
    at: str = "",
    members: Iterable[Any] = (),
    topic: str = "",
    message_count: int = 0,
) -> WorldModel:
    """② 关一幕（换幕/静默封口时由片段 owner 调用）。"""
    key = _text(episode_id)
    if not key:
        return model
    model.add("episode_closed", {
        "episode_id": key,
        "members": [name for name in (_text(item) for item in members or ()) if name],
        "topic": _text(topic),
        "message_count": int(message_count or 0),
    }, at=at)
    _trim_events(model, "episode_closed", EPISODE_TAIL_MAX * 4)
    return model


def current_episode(model: WorldModel) -> Episode | None:
    """当前打开的一幕（没有 -> None）。"""
    for item in reversed(project(model.events).episodes):
        if item.status == "open":
            return item
    return None


def note_self_message(
    model: WorldModel,
    *,
    content: str = "",
    at: str = "",
    kind: str = "reply",
) -> WorldModel:
    """② 记一条「她说过的话」（批D Step 36）。空内容不写；超限截断。"""
    text = _raw_text(content, SELF_MESSAGE_TEXT_MAX)
    if not text:
        return model
    model.add("self_message", {
        "content": text,
        "kind": _text(kind) or "reply",
    }, at=at)
    _trim_events(model, "self_message", SELF_MESSAGE_TAIL_MAX)
    return model


def note_perception(
    model: WorldModel,
    *,
    who: str = "",
    who_id: str = "",
    text: str = "",
    kind: str = "text",
    msg_type: str = "",
    msg_id: str = "",
    reply_to: str = "",
    quote_user: str = "",
    quote_text: str = "",
    mentions: Iterable[Any] = (),
    media: Mapping[str, Any] | None = None,
    platform: str = "",
    account_id: str = "",
    at: str = "",
) -> WorldModel:
    """①→②：落一条原始感知账（批 A4）。

    同一 ``msg_id`` 幂等（重复投递/重放不落第二条）；正文按
    :data:`PERCEPTION_TEXT_MAX` 截断并保留 ``text_len``/``truncated``；
    每会话只保最近 :data:`PERCEPTION_TAIL_MAX` 条感知事件（更早的原文在终端库）。
    """
    key = _text(msg_id)
    if key and any(
        event.kind == "perception_observed"
        and _text((event.payload or {}).get("msg_id")) == key
        for event in model.events
    ):
        return model
    original = str(text or "").strip()
    names: list[str] = []
    for item in mentions or ():
        name = _text(item)
        if name and name not in names:
            names.append(name)
    payload = {
        "who": _text(who),
        "who_id": _text(who_id),
        "text": _raw_text(original, PERCEPTION_TEXT_MAX),
        "text_len": len(original),
        "truncated": len(original) > PERCEPTION_TEXT_MAX,
        "kind": _text(kind) or "text",
        "msg_type": _text(msg_type),
        "msg_id": key,
        "reply_to": _text(reply_to),
        "quote_user": _text(quote_user),
        "quote_text": _raw_text(quote_text, PERCEPTION_QUOTE_MAX),
        "mentions": tuple(names),
        "media": dict(media) if isinstance(media, Mapping) else {},
        "platform": _text(platform),
        "account_id": _text(account_id),
    }
    model.add("perception_observed", payload, at=at)
    _trim_perceptions(model)
    return model


def _trim_perceptions(model: WorldModel) -> None:
    """尾部环形：只清最老的感知事件，其余事件与其相对顺序一律不动。"""
    seen = [event for event in model.events if event.kind == "perception_observed"]
    overflow = len(seen) - PERCEPTION_TAIL_MAX
    if overflow <= 0:
        return
    drop = {id(event) for event in seen[:overflow]}
    model.events[:] = [
        event for event in model.events
        if event.kind != "perception_observed" or id(event) not in drop
    ]


def recent_perceptions(
    model: WorldModel, limit: int = 6,
) -> tuple[PerceptionObservation, ...]:
    """最近 ``limit`` 条原始感知账（老 -> 新）；只读。"""
    if limit <= 0:
        return ()
    return project(model.events).perceptions[-limit:]


def observe_message(
    model: WorldModel,
    *,
    who: str = "",
    targets: Iterable[Any] = (),
    text: str = "",
    genre: str = "",
    topic: str = "",
    at: str = "",
) -> WorldModel:
    """把"这一轮谁对谁说了什么"记进中枢（Step 17）。

    ``targets`` 是**集合**（Step 16 契约，去重保序）；``text`` 截断到 120 字，
    避免事件流无界膨胀（只服务归属，不替代消息窗口）。
    """
    names: list[str] = []
    for item in targets or ():
        name = _text(item)
        if name and name not in names:
            names.append(name)
    body = _text(text)
    if len(body) > 120:
        body = body[:119] + "…"
    model.add("message_observed", {
        "who": _text(who),
        "targets": names,
        "text": body,
        "genre": _text(genre),
        "topic": _text(topic),
    }, at=at)
    return model


def recent_messages(
    model: WorldModel, *, limit: int = 3, max_chars: int = 80,
) -> tuple[str, ...]:
    """最近几轮的「谁 → 对谁：说了什么」短行（旧→新；targets 是集合）。"""
    view = project(model.events)
    out: list[str] = []
    for message in list(view.messages)[-max(1, int(limit)):]:
        text = message.text
        if len(text) > max_chars:
            text = text[:max_chars - 1] + "…"
        audience = "、".join(message.targets) if message.targets else "未判出"
        out.append(f"{message.who or '未知'}→{audience}：{text}")
    return tuple(out)


def position_lines(model: WorldModel, *, max_lines: int = 3) -> tuple[str, ...]:
    """Position 投影里的关系证据 -> 可注入 [位置] 的短行。"""
    view = project(model.events)
    lines = [f"- {value}" for value in view.desires.values() if str(value).strip()]
    return tuple(lines[:max_lines])


def _trim_events(model: WorldModel, kind: str, limit: int) -> None:
    """Tail ring for one event kind; other events keep their order untouched."""
    seen = [event for event in model.events if event.kind == kind]
    overflow = len(seen) - max(0, int(limit))
    if overflow <= 0:
        return
    drop = {id(event) for event in seen[:overflow]}
    model.events[:] = [
        event for event in model.events
        if event.kind != kind or id(event) not in drop
    ]
