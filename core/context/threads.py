"""N3 话题簇与锚点：**canonical owner**（``FocusInput`` → ``FocusResult``）。

规格：``docs/话题簇与锚点规格.md``（14 条已定决策，§四 锚点判定 / §五 不换人规则）；
落点：``docs/decisions/架构评估.md`` §3.3（N3 焦点状态）+ §6.1 P1（注入 ``[话题簇与锚点]`` 块）；
层位：``docs/全链路架构审计与目标架构.md`` §2.2 **L4**（:346）。

它回答一个问题（现状全系统没人回答）：**这一轮在聊哪件事、主角是谁**。
缺了这层，消费方只能退回"最近说话人"——那正是 2026-09-30 21:18 串线事故的成因。

纪律（照 ``resolve_attribution`` 的先例，规格 §七 收编清单）
------------------------------------------------------------
* **读时派生、纯函数优先**：聚类/锚点全部当轮算，只有"焦点状态一行"落库
  （``db/repo_focus.py`` 的 ``session_focus``，规格已定决策 1 经 R-T5 修订为
  「**除焦点状态外仍不落库**」）；
* **聚类内核只有一套**：2-gram 取键（``room_state._bigram_key``）与
  「按代表折叠」（``room_state.fold_groups`` / ``share_key``）**从 [局况] 升格**
  到这里复用，本文件**不许**再写第二套聚类（规格 §七 铁律②「唯一归属」）；
* **归属边只认 owner**：``@`` / ``[引用 …]`` / 轻拍一律走
  ``core.conversation.addressing``；本文件**不写**任何归属正则；
* **禁止条款（规格 §4.3，写死）**：① 不得用「最近说话人」当锚点依据；
  ② 不得用「我刚回答过谁」当锚点依据；③ 不得把媒体占位名当人；④ 同名多人不消歧；
  ⑤ ``unresolved`` 时不得输出历史断言。
* **诚实失败**：判据不足 → ``anchor=None`` + ``anchor_confidence="unresolved"``，
  **不猜、不回落**（规格 §4.2⑥）。

三态开关
--------
``YINYUE_FEATURE_THREADS``（``core/features.py``，默认 **off**）：
``off`` = 本模块一行都不走（不落库、不注入块，与接线前**逐字节一致**，回滚位）；
``shadow`` = **只算**：判定 + 写 ``session_focus`` + 留痕，**不注入**块
（``.env`` 明文；注入另见 :func:`threads_inject_enabled`）；
``on`` = 判定/落库/留痕全走，且把 ``[话题簇与锚点]`` 块注入 prompt。

生产接线（第二期）
------------------
``core/routing/router.py`` 的两处装配点（直回 ``_invoke_conversational_inner``、
触发 ``_on_group_trigger``）各调一次 :func:`attach_focus`，把这一轮的**输入**
（``focus_rows`` / ``focus_current`` / ``focus_repo``）写进 ``context.extra``；
判定、渲染、落库仍全在本 owner 里（:func:`focus_block_from_context`，装配器入列时调）。
接线失败 → 一个键都不写 + ``focus_wiring_failed`` 留痕，**不打断这一轮**。

封口预取（S2.5，**增量暴露**：接受 / 返回已算好的结果，焦点语义一个字不改）
----------------------------------------------------------------------------
"群内回合归属"的第①问（已随归属族退役）要一份**已算好的**焦点结论，
而封口（``core.group_state``）发生在回合装配**之前** —— 所以这一份由
:func:`prefetch_focus` 在封口那一刻算一次（读窗口由调用方给，**本函数不读窗口**），
返回值随 ``PendingQuestion.focus_result`` 一路交给同一回合的 prompt 装配
（:func:`attach_focus` 的 ``result=`` → ``extra["focus_result"]`` →
:func:`focus_block_from_context` **只渲染、不重算**）。

三条纪律：

* **算法只有一套**：装配与预取都经过 :func:`compute_focus`（同一个
  ``load_prev_focus`` → ``calculate_focus`` → ``save_focus`` 序列），所以
  "同一回合算了几次"就是 :func:`focus_compute_count` 的读数（**≤1**）；
* **off 一个字节都不走**：``THREADS=off`` 时 :func:`prefetch_focus` 直接返回
  ``None``（**不读窗口、不读焦点行、不落库**）；
* **判据不在本文件**：预取只把结论交出去，谁信它、信到什么程度由消费方说了算。
"""

from __future__ import annotations

import os
import time
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Mapping, Sequence

import structlog

# 聚类内核（**升格**自 [局况]，唯一一套；本文件不新写聚类）
from core.context.room_state import (
    _bigram_key,
    _is_media_placeholder,
    share_key,
)
from safelog import warn

log = structlog.get_logger()


def _timing_qa_gap() -> float:
    from core.timing import get as _timing_get

    return _timing_get().qa_gap


def _duplicate_window_seconds() -> float:
    from core.timing import get as _timing_get

    return _timing_get().duplicate_window

#: 锚点置信度取值（规格 §4.2 六序的强度映射 + 诚实失败）。
AnchorConfidence = Literal["high", "medium", "low", "unresolved"]

CONF_HIGH: AnchorConfidence = "high"
CONF_MEDIUM: AnchorConfidence = "medium"
CONF_LOW: AnchorConfidence = "low"
CONF_UNRESOLVED: AnchorConfidence = "unresolved"

#: 每条消息的置信度排序（比较用；``unresolved`` 最低）。
_CONFIDENCE_RANK = {CONF_UNRESOLVED: 0, CONF_LOW: 1, CONF_MEDIUM: 2, CONF_HIGH: 3}

#: 槽位名（FocusSlot.slot）。
SLOT_CURRENT = "current"
SLOT_PREVIOUS = "previous"
SLOT_SUSPENDED = "suspended"

#: 集群内回看继承锚点的上限（防"整簇回溯"变成事实上的"最近说话人"）。
_INHERIT_LOOKBACK = 6

#: 槽位过期时间（规格 §3.3「读时、不落库」+ R-G1 的 24 h 口径）。
SLOT_TTL = timedelta(hours=24)

#: ``[话题簇与锚点]`` 块上限（规格 §八【已定 7】：独立常驻块、**≤6 行**）。
BLOCK_MAX_LINES = 6

#: 块头（**唯一**字面量；``core/context/assembler.py::_BLOCK_POLICY`` 逐字登记）。
BLOCK_HEADER = "[话题簇与锚点]"


# ===========================================================================
# 契约（字段级见 docs/decisions/架构评估.md §3.3）
# ===========================================================================
@dataclass(frozen=True)
class FocusSlot:
    """焦点栈里的一格（当前 / 上一个 / 挂起的话题簇）。"""

    slot: str = SLOT_CURRENT
    thread_id: str = ""
    anchor: Any | None = None                     # Person｜None（unresolved）
    anchor_confidence: AnchorConfidence = CONF_UNRESOLVED
    keywords: tuple[str, ...] = ()
    topic: str = ""
    evidence: tuple[str, ...] = ()
    updated_at: datetime | None = None
    expires_at: datetime | None = None

    def expired(self, now: datetime | None = None) -> bool:
        """``expires_at`` 已过（``None`` = 不过期）。"""
        if self.expires_at is None:
            return False
        return (now or datetime.now()) > self.expires_at

    def anchor_label(self) -> str:
        """锚点显示名（``None`` → ``""``）。"""
        return _person_label(self.anchor)


@dataclass(frozen=True)
class FocusInput:
    """一轮的输入：**当前这条** + 同一会话的窗口（旧→新）+ 上一轮焦点行。

    与 ``docs/decisions/架构评估.md:245`` 的字段级契约一致；``prev_focus`` 是
    ``db.repo_focus.FocusState`` 那一行（读不到就是 ``None`` → 当轮判定）。
    """

    chat_id: str = ""
    platform: str = ""
    account_id: str = ""
    current: Any | None = None
    window_rows: Sequence[Any] = ()
    prev_focus: Any | None = None
    self_name: str = "鲸鲸"
    self_person_id: str = ""
    now: datetime | None = None


@dataclass(frozen=True)
class FocusResult:
    """一轮的输出：话题簇 + 锚点 + 焦点栈 + ``primary_thread``。"""

    focus_id: str = ""
    thread_id: str = ""
    anchor: Any | None = None
    anchor_confidence: AnchorConfidence = CONF_UNRESOLVED
    anchor_evidence: tuple[str, ...] = ()
    stack: tuple[FocusSlot, ...] = ()
    primary_thread: str = ""
    thread_ambiguous: bool = False
    switched: bool = False
    switch_evidence: tuple[str, ...] = ()
    #: 变化检测：本轮最终锚点与上一轮焦点是否不同人（旧 switched 语义，保留）。
    anchor_changed: bool = False
    #: 允许谓词：本轮是否带显式接管证据（引用对方原话 / @ 新对象 / 拍目标 / 被点名 / 自称接管）。
    takeover_allowed: bool = False
    takeover_evidence: tuple[str, ...] = ()
    #: 锚点为空时的默认主语（1A：无证据默认说话人；anchor 本身保持可空）。
    about: Any | None = None
    about_is_speaker: bool = False
    topic: str = ""
    #: 本轮的**全部**话题簇（规格 §3.2【已定 3】：一条消息可属多簇）。
    threads: tuple[FocusSlot, ...] = ()
    addressee: tuple[str, ...] = ()
    #: 这是不是"当轮判定"（读不到焦点行 / 表缺失 → 降级；规格 §六「失败即不注入」）。
    degraded: bool = False

    @property
    def resolved(self) -> bool:
        return self.anchor_confidence != CONF_UNRESOLVED

    @property
    def primary_slot(self) -> FocusSlot | None:
        for slot in self.threads:
            if slot.thread_id and slot.thread_id == self.primary_thread:
                return slot
        return None

    def anchor_label(self) -> str:
        anchor = self.anchor
        if anchor is None:
            return ""
        label = getattr(anchor, "label", None)
        if callable(label):
            return str(label())
        return str(getattr(anchor, "display_name", "") or anchor)


# ===========================================================================
# 开关（三态；唯一读点是 threads_mode，别处只读布尔）
# ===========================================================================
def threads_mode() -> str:
    """``YINYUE_FEATURE_THREADS`` 的三态读数（``off`` / ``shadow`` / ``on``）。

    **默认 off**（规格【已定 11 ★】：先 shadow 攒分，评测达标后再决定是否默认开）。
    读法走 ``core.features.mode``（**唯一**三态读数），不用 ``enabled``——后者会把
    ``shadow`` 读成 ``False``（"影子门悄悄变成不做事"的现成通道）。
    """
    try:
        from core import features

        return str(features.mode(features.THREADS, features.MODE_OFF))
    except Exception:  # noqa: BLE001 - 读不到开关 = 关（回滚位，绝不半开半关）
        warn("core/context/threads.py:233 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def threads_enabled() -> bool:
    """判定 / 落库 / 留痕用：``shadow`` 与 ``on`` 都做事，只有 ``off`` 不做事。

    注入是另一个问题，见 :func:`threads_inject_enabled`（只有 ``on``）。
    """
    return threads_mode() != "off"


def threads_inject_enabled() -> bool:
    """只有 ``on`` 才把 ``[话题簇与锚点]`` 块注入 prompt。

    ``shadow`` 的合同是"只算 + 写 session_focus + 留痕，**不注入**"
    （``.env:324-327`` 明文；未达标：锚点 0.500 / 门槛 0.85，
    换人误判 0.113 / 门槛 0.05 ⇒ 开 on 会认错人）。判定与落库仍走
    :func:`threads_enabled`（``mode != off``），两者**不是同一个问题**。
    """
    return threads_mode() == "on"


# ===========================================================================
# 行读取（与 room_state / addressing 同一批读取口径；不新造第二套）
# ===========================================================================
def _get(row: Any, name: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _text(row: Any) -> str:
    return str(_get(row, "content", "") or "")


def _speaker(row: Any) -> str:
    # WP-03：说话人解析的唯一 owner = core/terminal/speaker.resolve_speaker_from_row。
    from core.terminal.speaker import resolve_speaker_from_row

    return resolve_speaker_from_row(row, default="")


def _is_self(row: Any) -> bool:
    return bool(_get(row, "is_self", False))


def _stamp(row: Any) -> datetime | None:
    for key in ("created_at", "occurred_at", "at"):
        value = _get(row, key)
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                warn("core/context/threads.py:290 ValueError", err=repr(__import__("sys").exc_info()[1]))
                continue
    return None


def _ref(row: Any) -> str:
    for key in ("event_id", "message_ref", "msg_id", "id"):
        value = str(_get(row, key, "") or "")
        if value:
            return value
    stamp = _stamp(row)
    return f"{stamp.isoformat() if stamp else '?'}:{_speaker(row)}"


def _is_pat(row: Any) -> bool:
    if str(_get(row, "event_type", "") or "") == "group_pat":
        return True
    if bool(_get(row, "is_pat", False)):
        return True
    text = _text(row)
    if "拍了拍" not in text:
        return False
    try:
        from core.conversation.addressing import _PAT_TITLE_RE  # noqa: PLC0415

        return bool(_PAT_TITLE_RE.match(text))
    except Exception:  # noqa: BLE001 - 判据库缺失时不猜
        warn("core/context/threads.py:316 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def _is_revoke(row: Any) -> bool:
    if bool(_get(row, "is_revoke", False)):
        return True
    return "<sysmsg type=\"revokemsg\">" in _text(row)


def _person_label(person: Any) -> str:
    if person is None:
        return ""
    label = getattr(person, "label", None)
    if callable(label):
        return str(label())
    return str(getattr(person, "display_name", "") or getattr(person, "person_id", "") or "")


def _person_id(person: Any, *, fallback_name: bool = False) -> str:
    """``person_id``；``fallback_name=True`` 时退回显示名（**只用于落库**）。"""
    if person is None:
        return ""
    value = str(getattr(person, "person_id", "") or "")
    if value:
        return value
    return _person_label(person) if fallback_name else ""


def _same_person(left: Any, right: Any) -> bool:
    """两个人是不是同一个（**身份判等走 person_id，名字只作显示**）。"""
    if left is None or right is None:
        return False
    left_id, right_id = _person_id(left), _person_id(right)
    if left_id and right_id:
        return left_id == right_id
    return _person_label(left) == _person_label(right) and bool(_person_label(left))


def _identity_maps(rows: Sequence[Any]) -> tuple[dict[str, str], dict[str, str]]:
    """从窗口行里取"名字 → person_id / display_name"两表（不新造身份源）。

    ``resolve_attribution`` 的 ``identities`` / ``display_names`` 是**输入**，
    这里只把窗口里**已经出现过的人**（行的说话人/actor/收件人）填进去——
    没出现过的人一律不给 id（"认不出是谁" → unresolved，规格 §4.3.4 不消歧）。
    """
    identities: dict[str, str] = {}
    display: dict[str, str] = {}
    for row in rows:
        name = _speaker(row)
        if not name:
            continue
        display.setdefault(name, name)
        person_id = str(
            _get(row, "actor_person_id", "")
            or _get(row, "sender_id", "")
            or ""
        )
        if person_id:
            identities.setdefault(name, person_id)
        for recipient in _get(row, "recipients", ()) or ():
            if recipient:
                display.setdefault(str(recipient), str(recipient))
    return identities, display


def _attribution_of(
    row: Any,
    *,
    self_name: str,
    self_person_id: str,
    identities: Mapping[str, str] | None = None,
    display_names: Mapping[str, str] | None = None,
):
    """一行的 canonical 归属（``None`` = 这行没有可用信息）。失败不抛。"""
    from core.conversation.addressing import resolve_attribution

    try:
        return resolve_attribution(
            row,
            self_name=str(self_name or ""),
            self_person_id=str(self_person_id or ""),
            identities=identities,
            display_names=display_names,
        )
    except Exception:  # noqa: BLE001 - 归属库坏了 → 这一行没有边，不猜
        warn("core/context/threads.py:401 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


# ===========================================================================
# 锚点判定（规格 §4.2 六序；只吃确定性证据）
# ===========================================================================
@dataclass(frozen=True)
class _Anchor:
    """一条消息的锚点候选（``person`` 为 ``None`` = 明确判不出来）。"""

    person: Any | None = None
    confidence: AnchorConfidence = CONF_UNRESOLVED
    evidence: tuple[str, ...] = ()
    #: 有指向信号却认不出人（规格 §六「诚实失败」：可区分"确实没有"与"内部挂了"）。
    unresolved: bool = False


def _anchor_of_row(
    row: Any,
    attribution: Any,
    *,
    self_name: str,
    self_person_id: str,
    known: frozenset[str] = frozenset(),
) -> _Anchor:
    """§4.2 的 ①②③ 三条**当轮**判据（④ 继承由 :func:`calculate_focus` 负责）。

    * ① 引用了某人的话 → 主角 = 被引用者（强）。**引用了她自己**不算：
      那只是"接着她的话说"，主角仍是簇内的人（t0019/t0026/t0048 的形态）。
    * ② 代词绑定（``referent.bind_pronouns``，要有动作证据；强）。
    * ③ 正文点名别人（``addressing.named_targets``）→ **中**（不要求代词）。

    ``known`` = 会话里**已经出现过的人名**（说话人/收件人）。只有它是人名的
    证据来源——正文里冒出来的陌生名字不会因此变成"某个人"（§4.3.4 同名不消歧）。
    ``@X 你把他闹钟关了`` 的形态由①/③都覆盖不到 → ``unresolved``（不得指派给 X，
    规格 §4.1【已定 2】的结构性决定）。
    """
    from core.conversation.addressing import named_targets

    text = _text(row)
    evidence: list[str] = []

    # ① 引用（强）
    quoted = getattr(attribution, "quoted", None)
    if quoted is not None and getattr(quoted, "present", False):
        if getattr(quoted, "resolvable", False) and getattr(quoted, "author_name", ""):
            author = getattr(quoted, "author_name", "")
            if _is_self_name(author, self_name, self_person_id):
                # 引用的**是她自己**：那只是"接着她的话说"，不换主角（§4.2 ① 的
                # 反面形态；t0018/t0019/t0026/t0048 实测）。继续往下走 ②③，
                # 都不中就走继承（④）——**不许**因此判成 unresolved。
                pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）
            else:
                evidence.append(f"引用 {author}")
                person = _person_for(author, attribution, known=known)
                if person is None:
                    return _Anchor(
                        person=None,
                        confidence=CONF_UNRESOLVED,
                        evidence=tuple(evidence),
                        unresolved=True,
                    )
                return _Anchor(person=person, confidence=CONF_HIGH, evidence=tuple(evidence))
        elif text.strip() and not _is_media_placeholder(text):
            # 有引用却认不出作者（媒体占位名/〔作者不明〕）→ 诚实失败（§4.3.3）。
            return _Anchor(
                person=None,
                confidence=CONF_UNRESOLVED,
                evidence=("引用认不出作者",),
                unresolved=True,
            )

    # ② 代词绑定（强，需动作证据；确定性、无 IO）
    if self_name or self_person_id:
        try:
            from core.context.referent import bind_pronouns

            bindings = bind_pronouns(
                [row], speaker=_speaker(row), text=text, self_name=str(self_name or "")
            )
            for binding in bindings:
                target = getattr(binding, "target", None)
                if target is not None and not bool(getattr(target, "is_self", False)):
                    evidence.append(f"代词指代 {_person_label(target)}")
                    return _Anchor(
                        person=target, confidence=CONF_HIGH, evidence=tuple(evidence)
                    )
        except Exception:  # noqa: BLE001 - 绑定库缺失 → 退到 ③，不猜
            warn("core/context/threads.py:489 Exception", err=repr(__import__("sys").exc_info()[1]))

    # ③ 正文**谈论**某个人（中）：与 ② 的差别是不要求代词。
    #
    # 「@X」有两个语义（§4.1 结构性决定）：**说给** X ≠ **在说** X 的事。
    # 判据落地：X 出现在**去掉 @ 之后的正文**里 → ③ 成立；X 只是 @ 的对象而
    # 正文另外用了第三人称代词（「@X 你把他闹钟关了」）→ **不成立**，宁可 unresolved。
    body = _body_without_mentions(text)
    for name in named_targets(text):
        if not name or _is_self_name(name, self_name, self_person_id):
            continue
        discussed = bool(body and name in body)
        if not discussed and _has_unbound_pronoun(body):
            evidence.append(f"被 @ 的 {name} 不是话题主角（正文另有代词）")
            return _Anchor(
                person=None,
                confidence=CONF_UNRESOLVED,
                evidence=tuple(evidence),
                unresolved=True,
            )
        evidence.append(f"正文点名 {name}" if discussed else f"@ 的对象 {name}")
        person = _person_for(name, attribution, known=known)
        if person is None:
            return _Anchor(
                person=None,
                confidence=CONF_UNRESOLVED,
                evidence=tuple(evidence),
                unresolved=True,
            )
        if bool(getattr(person, "is_self", False)):
            # 点的是她自己：那是"对她说"，不是"在说她的事"（§4.1 两个语义分开）。
            evidence.pop()
            continue
        return _Anchor(person=person, confidence=CONF_MEDIUM, evidence=tuple(evidence))

    # ③b 拍一拍目标（中）：拍一拍语法的目标 = 这条在说谁的事（t0005/t0008/t0022/t0030 实测）。
    try:
        from core.conversation.addressing import pat_targets

        for name in pat_targets(text, (self_name,)):
            if not name or _is_self_name(name, self_name, self_person_id):
                continue
            person = _person_for(name, attribution, known=known)
            if person is None or bool(getattr(person, "is_self", False)):
                continue
            evidence.append(f"拍一拍 {name}")
            return _Anchor(person=person, confidence=CONF_MEDIUM, evidence=tuple(evidence))
    except Exception:  # noqa: BLE001 - 拍解析失败退回 ⑤/继承，不猜
        warn("core/context/threads.py:537 Exception", err=repr(__import__("sys").exc_info()[1]))

    # ⑤ 有指向信号却认不出人：正文用了第三人称代词（"他/她"）而没有可绑定的对象
    # —— §4.1 反例现场版：主角是"他"，**不许**指派给被 @ 的人，
    # 也**不许**靠继承把上一轮的主角按过来（§六 诚实失败）。
    if _has_unbound_pronoun(text) or _has_unbound_pronoun(body):
        return _Anchor(
            person=None,
            confidence=CONF_UNRESOLVED,
            evidence=("第三人称代词无法绑定",),
            unresolved=True,
        )

    # ④/⑥ 无当轮证据 → 交给"继承"或"诚实失败"（不在这里回落任何人）。
    return _Anchor()


def _body_without_mentions(text: Any) -> str:
    """去掉 ``@某人`` 之后的正文（判"在说谁的事"只能看正文，不能看 @ 的称呼）。

    与 canonical 的口径一致：剥 @ 之后**只看引用块之后**新写的那段
    （引用里的 @ 是上下文，不是本轮目标）。
    """
    try:
        from core.conversation.addressing import (
            mentioned_names,
            strip_mentions,
            strip_quote_block,
        )

        body = strip_quote_block(str(text or ""))
        for name in mentioned_names(body):
            body = body.replace(f"@{name}", " ")
        # 兜底：正文里可能还有 `@名字` 的写法没被上面的循环覆盖（截断/标点差异）。
        return strip_mentions(body).strip()
    except Exception:  # noqa: BLE001 - 判据库缺失 → 拿不到正文，按"没谈"处理
        warn("core/context/threads.py:573 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _has_unbound_pronoun(text: Any) -> bool:
    """正文里有没有第三人称代词（"他/她/它"）——判据走 canonical 的提取器。"""
    try:
        from core.context.referent import extract_pronouns

        return bool(extract_pronouns(text))
    except Exception:  # noqa: BLE001 - 判据库缺失 → 没有这条证据
        warn("core/context/threads.py:583 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def _is_self_name(name: str, self_name: str, self_person_id: str) -> bool:
    value = str(name or "").strip()
    if not value:
        return False
    return bool(
        (self_name and value == str(self_name).strip())
        or (self_person_id and value == str(self_person_id).strip())
    )


def _person_for(
    name: str,
    attribution: Any,
    *,
    known: frozenset[str] = frozenset(),
) -> Any | None:
    """正文里的名字 → ``Person``（**不猜身份、不消歧**）。

    名字**是不是人**只有两个证据来源：① 它是这条消息的说话人/收件人/被引用作者
    （canonical 已经判过）；② 它在 ``known``（会话里出现过的人名集合）里。
    两个都不是 → ``None`` → 上游按「诚实失败」处理（规格 §4.3.3/§4.3.4：
    媒体占位名不是人、同名多人不消歧）。
    """
    from core.conversation.addressing import Person

    value = str(name or "").strip()
    if not value:
        return None
    if attribution is not None:
        speaker = getattr(attribution, "speaker", None)
        if speaker is not None and _person_label(speaker) == value:
            return Person(
                person_id=_person_id(speaker),
                display_name=value,
                is_self=bool(getattr(speaker, "is_self", False)),
            )
        for addressee in getattr(attribution, "addressees", ()) or ():
            if _person_label(addressee) == value:
                return Person(
                    person_id=_person_id(addressee),
                    display_name=value,
                    source=str(getattr(addressee, "source", "") or ""),
                    is_self=bool(getattr(addressee, "is_self", False)),
                )
        quoted = getattr(attribution, "quoted", None)
        if quoted is not None and getattr(quoted, "author_name", "") == value:
            return Person(
                person_id=str(getattr(quoted, "author_person_id", "") or ""),
                display_name=value,
            )
    if value in known:
        # 会话里出现过这个名字（别人是这么叫他的）：显示名是事实，id 留空。
        return Person(person_id="", display_name=value)
    return None

# ===========================================================================
# 聚类（时间窗断句 + 按代表折叠；内核来自 room_state）
# ===========================================================================
def _content_text(rows: Sequence[Any], group: Sequence[int]) -> str:
    """簇里最新一条**有内容**的行的原文（不做 40 字裁剪：要做词面比较）。"""
    for index in reversed(list(group)):
        text = _text(rows[index])
        if text.strip() and not _is_media_placeholder(text):
            return " ".join(text.split())
    return ""


def _addresses_self(row: Any, *, self_name: str, self_person_id: str) -> bool:
    """这一簇里有没有"**对她说**"的证据（@她 / 引用她 / 正文直呼她）。

    这是"新簇能不能顺延上一簇主角"的**第二条证据**（第一条是词面连接）：
    明确对她说的话，接在上面那条后面 = 同一场对话；纯粹的自言自语/新话题不算
    （t0016「主人格出现了」、t0084「大家生活我的梦」实测为 unresolved）。
    """
    text = _text(row)
    if not text.strip():
        return False
    try:
        from core.conversation.addressing import (
            named_targets,
            quote_of,
            split_quote_line,
        )

        quoted = quote_of(text)
        if quoted.present and quoted.resolvable and quoted.author_name:
            if _is_self_name(quoted.author_name, self_name, self_person_id):
                return True
        elif quoted.present:
            _, body = split_quote_line(text)
            if _is_self_name(body.strip(" ：:。"), self_name, self_person_id):
                return True
        for name in named_targets(text):
            if _is_self_name(name, self_name, self_person_id):
                return True
    except Exception:  # noqa: BLE001 - 判据库缺失 → 没有这条证据
        warn("core/context/threads.py:682 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False
    return False


#: 一个话题簇的滚动状态（**跨轮累计**：这一轮只看"当前这条挂在哪个簇上"）。
@dataclass
class _Cluster:
    thread_id: str = ""
    members: list[int] = field(default_factory=list)
    rep_key: frozenset[str] = frozenset()
    content: str = ""
    keywords: tuple[str, ...] = ()
    anchor: Any | None = None
    confidence: AnchorConfidence = CONF_UNRESOLVED
    evidence: tuple[str, ...] = ()
    last_at: datetime | None = None
    updated_at: datetime | None = None


def _register_grams(
    postings: dict[str, list[int]],
    pos_of: dict[int, int],
    cluster: Any,
    key: frozenset[str],
) -> None:
    """Index a cluster's representative 2-grams -> cluster position.

    The cluster scan used to be ``for cluster in reversed(clusters)``, i.e.
    O(rows x clusters) set intersections. Only clusters sharing a 2-gram with
    the row can ever become a merge target (the loop's other branches can only
    ``continue``), so an inverted index keeps the same winner (newest linked
    cluster inside the gap) at O(rows x grams).
    """
    pos = pos_of.get(id(cluster))
    if pos is None:
        return
    for gram in key:
        bucket = postings.get(gram)
        if bucket is None:
            postings[gram] = [pos]
        elif bucket[-1] != pos:
            bucket.append(pos)


def _candidate_clusters(
    clusters: Sequence[Any], postings: dict[str, list[int]], key: frozenset[str]
) -> list[Any]:
    """Clusters sharing a 2-gram with ``key``, newest first (same winner)."""
    if not key or not postings:
        return []
    positions: set[int] = set()
    for gram in key:
        bucket = postings.get(gram)
        if bucket:
            positions.update(bucket)
    limit = len(clusters)
    return [clusters[pos] for pos in sorted(positions, reverse=True) if pos < limit]


def _rollup(
    rows: Sequence[Any],
    keys: Sequence[frozenset[str]],
    current_index: int,
    explicit: Sequence[Any] = (),
    *,
    gap_seconds: float | None = None,
    deadline: float | None = None,
) -> tuple[list[_Cluster], int]:
    """把窗口 + 当前这条滚成话题簇；返回 ``(簇列表, 当前这条所在的簇下标)``。

    **跨轮累计**（这是"周期稳定性"的关键，也是实测口径）：上一轮已经判过的消息
    只用来"重建它当时进的簇"，**不重新判锚点**——否则同一个 180s 窗口在不同轮里
    会长出不同的簇（实测：整窗重算的簇数 62 vs 真值 31，漏合严重）。

    判据（规格 §3.2，阈值一个都不新造）：
    * 时间窗断句：与**本簇最新一条**间隔 > ``QA_GAP_LIMIT_SECONDS``（60s）→ 另起一簇；
    * 关键词重叠：与**簇代表**（本簇最新一条有内容的行）共享 ≥1 实义 2-gram → 入该簇；
      从最近一簇往前找**第一个**命中的（只比代表、不做链式合并）；
    * 图片附件/轻拍/系统消息 → **归线**到当前簇（不改代表、不改锚点，【已定 5】）；
    * ``explicit[i]`` = 第 i 条**自己**判出来的显式锚点：与上一簇的锚点不是同一个人
      → 另起一簇（"换锚 = 换话题"；实测 t0035「引用 P4: 上车」这种没有词面重合的
      换人轮会被并进旧簇，锚点就丢了）。
    """
    clusters: list[_Cluster] = []
    # Hoist the timing read out of the O(rows x clusters) scan (see _beyond_gap).
    gap = _timing_qa_gap() if gap_seconds is None else float(gap_seconds)
    postings: dict[str, list[int]] = {}
    pos_of: dict[int, int] = {}
    for index, row in enumerate(rows):
        if deadline is not None and index % 32 == 0 and time.monotonic() > deadline:
            # Budget guard: losing a cluster costs one confirmation line, while a
            # pinned event loop costs the whole turn (2026-10-17 incident).
            log.info(
                "focus_budget_exceeded",
                rows=len(rows),
                scanned=index,
                clusters=len(clusters),
            )
            break
        text = _text(row)
        stamp = _stamp(row)
        key = keys[index]
        target: _Cluster | None = None
        explicit_person = explicit[index] if index < len(explicit) else None

        if _is_media_placeholder(text) or not text.strip():
            # 无独立话题内容：归线到当前簇（时间窗内才归；超窗就另起）。
            candidate = clusters[-1] if clusters else None
            if candidate is not None and not _beyond_gap(
                candidate, stamp, gap
            ):
                target = candidate
        else:
            for cluster in _candidate_clusters(clusters, postings, key):
                if _beyond_gap(cluster, stamp, gap):
                    continue
                linked = bool(cluster.rep_key and share_key(cluster.rep_key, key))
                if (
                    index != current_index
                    and explicit_person is not None
                    and cluster.anchor is not None
                    and not _same_person(cluster.anchor, explicit_person)
                    and not linked
                ):
                    # 显式换人（**只看窗口里的历史条**）：换锚 = 在说另一件事/另一个人
                    # （实测 t0035「引用 P4: 上车」这种没词面重合的换人轮会被并进旧簇，
                    # 锚点就丢了）。**与旧簇有词面连接**时不拆——那正是"@了别人、
                    # 但还在说同一件事"（t0081「@P2 那就好好玩，别又走错站台」）。
                    #
                    # **当前这条也不拆**：事故窗的形态就是"@ 了另一个人、还在说同一件
                    # 事"，它换不换锚由锚点链（§4.2 ④ 继承 / §五 显式转移）决定。
                    #
                    # N3 第三期 [6] 实测（**归因修正**：三种去掉法各开新进程跑一次）：
                    #   · 只去掉 `index != current_index`（其余原样）→ 读数**一个字节都没动**
                    #     （0.500 / 0.113 / 0.611 / 0.126）→ **第二期把这一格记成
                    #     "当前条参与分簇"是记错了杠杆**：改动点落在**历史条**上，而且
                    #     `not linked` 在场时它本来就与下面那行 `linked` 分支等价；
                    #   · 再去掉 `not linked`（= 显式换人就拆簇）→ 错合率 0.389→0.302、
                    #     link_recall 0.126→0.115，**但换人误判 0.113→0.145**（安全项变差）；
                    #   · 把整个 `if` 等值改写成 `(…) or linked` → 同样 0.145 / 0.698 / 0.115。
                    # 三条都不优于现状 → **[6] 整条回退**（详情见第三期报告）。
                    continue
                if cluster.rep_key and share_key(cluster.rep_key, key):
                    target = cluster
                    break

        if target is None:
            target = _Cluster(thread_id=_ref(row))
            clusters.append(target)
            pos_of[id(target)] = len(clusters) - 1
        target.members.append(index)
        if stamp is not None and not _is_media_placeholder(text) and text.strip():
            # **只有有内容的那条推进簇尾巴**：表情包/拍一拍是"归线"的附件，
            # 它们不改变"这件事说到哪儿了"（否则一张图就能把 60s 断句永远撑开，
            # 下一句没头没脑的话就会被并进上一个话题）。
            target.last_at = stamp
        target.updated_at = stamp or target.updated_at
        if explicit_person is not None and target.anchor is None:
            target.anchor = explicit_person
            target.confidence = CONF_MEDIUM
        if key and not _is_media_placeholder(text):
            target.rep_key = key
            _register_grams(postings, pos_of, target, key)
            target.content = " ".join(text.split())
            target.keywords = tuple(sorted(key)[:4])

    if not clusters:
        return [], -1
    current = cluster_of_row(clusters, current_index)
    return clusters, current


def cluster_of_row(clusters: Sequence[_Cluster], index: int) -> int:
    """这条消息落在哪个簇（找不到 = ``-1``）。"""
    for position, cluster in enumerate(clusters):
        if index in cluster.members:
            return position
    return -1


def _beyond_gap(
    cluster: _Cluster, stamp: datetime | None, gap_seconds: float | None = None
) -> bool:
    """这条消息与本簇最新一条的间隔是不是已经超过 60s（超了就不许并入）。"""
    if stamp is None or cluster.last_at is None:
        return False
    gap = _timing_qa_gap() if gap_seconds is None else float(gap_seconds)
    return (stamp - cluster.last_at).total_seconds() > gap


def _keywords(rows: Sequence[Any], group: Sequence[int], *, limit: int = 4) -> tuple[str, ...]:
    """簇的呈现关键词（2-gram 频次最高的几个；只作呈现，不作判据）。

    判据只有一条（簇代表共享 2-gram），见 :func:`_rollup`；这里只影响
    ``FocusSlot.keywords`` 的可读性。
    """
    counts: dict[str, int] = {}
    for index in group:
        for gram in _bigram_key(_text(rows[index])):
            counts[gram] = counts.get(gram, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(gram for gram, _ in ordered[:limit])


# ===========================================================================
# 焦点栈 / primary_thread
# ===========================================================================
def _slot_rank(confidence: AnchorConfidence) -> int:
    return _CONFIDENCE_RANK.get(confidence, 0)


def _pick_primary(candidates: Sequence[FocusSlot]) -> tuple[str, bool]:
    """§4.4：``primary_thread`` = 含当前这条、且锚点强度最高的簇；并列取最近的。"""
    if not candidates:
        return "", False
    best = max(
        candidates,
        key=lambda slot: (
            _slot_rank(slot.anchor_confidence),
            slot.updated_at or datetime.min,
            slot.thread_id,
        ),
    )
    tied = [
        slot
        for slot in candidates
        if _slot_rank(slot.anchor_confidence) == _slot_rank(best.anchor_confidence)
        and (slot.updated_at or datetime.min) == (best.updated_at or datetime.min)
    ]
    if len(tied) > 1:
        return "", True
    return best.thread_id, False


def _slot_from_prev(prev: Any, now: datetime) -> FocusSlot | None:
    """上一轮的焦点行 → 「previous」槽（读不到 / 过期 → ``None``，退回当轮判定）。"""
    if prev is None:
        return None
    anchor_id = str(getattr(prev, "current_anchor_person_id", "") or "")
    updated = getattr(prev, "updated_at", None)
    if updated is not None and not isinstance(updated, datetime):
        updated = None
    expires = (updated + SLOT_TTL) if updated is not None else None
    if expires is not None and now > expires:
        return None
    if not str(getattr(prev, "current_topic", "") or "") and not anchor_id and not str(
        getattr(prev, "revision", "") or ""
    ):
        return None
    from core.conversation.addressing import Person

    anchor = (
        Person(person_id=anchor_id, display_name=anchor_id) if anchor_id else None
    )
    return FocusSlot(
        slot=SLOT_PREVIOUS,
        thread_id="",
        anchor=anchor,
        anchor_confidence=CONF_MEDIUM if anchor is not None else CONF_UNRESOLVED,
        topic=str(getattr(prev, "current_topic", "") or ""),
        evidence=("上一轮焦点（session_focus）",),
        updated_at=updated,
        expires_at=expires,
    )


# ===========================================================================
# 主入口
# ===========================================================================
def calculate_focus(
    focus_input: FocusInput, *, deadline: float | None = None
) -> FocusResult:
    """一轮的焦点判定：**纯函数、无 IO、同输入同输出**（规格 §十四.1）。

    任何异常都退回"当轮判定"的空结果（``degraded=True``）——
    焦点判定失败**只损失一句确认**，绝不许拖垮回合（降级契约，``db/repo_focus.py``）。
    """
    _bump_compute_count()
    try:
        return _calculate(focus_input, deadline=deadline)
    except Exception:  # noqa: BLE001 - 判定失败 = 当轮无焦点，不是回合失败
        warn("core/context/threads.py:963 Exception", err=repr(__import__("sys").exc_info()[1]))
        return FocusResult(degraded=True)


#: 判定**真算过几次**的进程内计数（**纯观测**，不参与任何判定）。
#: S2.5 的取证点：同一回合里焦点只算一次（封口取一次 → 装配复用，见
#: :func:`prefetch_focus` / :func:`focus_block_from_context`）。
_COMPUTE_COUNT = 0


def _bump_compute_count() -> None:
    global _COMPUTE_COUNT
    _COMPUTE_COUNT += 1


def focus_compute_count() -> int:
    """本进程里 :func:`calculate_focus` 被调用过几次（**只读**；测试/审计用）。"""
    return int(_COMPUTE_COUNT)


def reset_focus_compute_count() -> None:
    """把计数归零（测试隔离用；生产无人调用）。"""
    global _COMPUTE_COUNT
    _COMPUTE_COUNT = 0


def _calculate(
    focus_input: FocusInput, *, deadline: float | None = None
) -> FocusResult:
    rows = list(focus_input.window_rows or ())
    current = focus_input.current
    if current is not None:
        rows.append(current)
    rows = [row for row in rows if row is not None]
    if not rows:
        return FocusResult(degraded=True)

    now = focus_input.now or _stamp(rows[-1]) or datetime.now()
    identities, display = _identity_maps(rows)
    known = frozenset(display) | frozenset(identities)
    keys = [_bigram_key(_text(row)) for row in rows]

    # ---- 每行的归属与锚点候选 ----
    attributions: list[Any] = [None] * len(rows)
    candidates: list[_Anchor] = [_Anchor() for _ in rows]
    for index, row in enumerate(rows):
        attribution = _attribution_of(
            row,
            self_name=focus_input.self_name,
            self_person_id=focus_input.self_person_id,
            identities=identities,
            display_names=display,
        )
        attributions[index] = attribution
        candidates[index] = _anchor_of_row(
            row,
            attribution,
            self_name=focus_input.self_name,
            self_person_id=focus_input.self_person_id,
            known=known,
        )

    # ---- 聚类（显式换人 → 另起一簇）----
    clusters, current_cluster = _rollup(
        rows,
        keys,
        len(rows) - 1,
        explicit=[item.person for item in candidates],
        deadline=deadline,
    )
    cluster_of = {
        index: position
        for position, cluster in enumerate(clusters)
        for index in cluster.members
    }

    # ---- 簇 → 槽位 / 逐步解锚 ----
    previous_slot = _slot_from_prev(focus_input.prev_focus, now)
    # 跨轮（session_focus）的锚点：**只**用来判"这一轮该不该允许换人"，
    # 不用来当这一轮的候选（§4.3 禁止条款 2：不得用"我刚回答过谁"当锚点依据）。
    previous_anchor = previous_slot.anchor if previous_slot is not None else None

    # 锚点链：从窗口第一条滚到最后一条（**跨轮累计**的锚点部分，见 _rollup）。
    # 起点 = **上一轮焦点行**（§4.2 ④ 的跨轮形态：上一轮已经在聊某人的事，
    # 这一轮没有新证据时继续谈他——丢行只损失"一句确认"，读不到就是 None）。
    # 注意：它**不是**"最近说话人"，也不是"我刚回答过谁"（§4.3 禁止条款 1/2）。
    carried: Any | None = previous_anchor
    prev_key: frozenset[str] = frozenset()
    #: 窗口里（**含历史行**，不是"本轮消息本身"）最后一条自己判出候选的人。
    #: 只给「最终锚点没判出来」那一格当兜底比较对象用，见下面 `switched` 的注释：
    #: 它扫的是整个窗口 → 这正是「整窗取证」（M-b）那处归因的来源，**本期不动**。
    explicit_anchor: Any | None = None
    #: 上一条（历史末条 + 这一轮已走过的）**自己**有没有判定出候选。
    #: 它是"新簇顺延上一簇主角"的闸门：上一条若有候选（可能正是错锚的来源），
    #: 新簇必须拿词面/对她说当证据才允许顺延。
    prev_had_candidate = False
    #: 窗口里（**含历史行**）有没有任何一条带显式接管证据（@/引用/点名，≥ medium）。
    #: 同样**只**服务「最终锚点没判出来」的兜底那一格；主判据不看它（见 `switched`）。
    explicit_takeover = any(
        candidate.person is not None
        and _slot_rank(candidate.confidence) >= _slot_rank(CONF_MEDIUM)
        for candidate in candidates
    )
    for index, row in enumerate(rows):
        cluster = clusters[cluster_of[index]]
        candidate = candidates[index]
        inherited: Any | None = None
        inheritance_evidence: tuple[str, ...] = ()

        # ④ 簇内上一条的锚点继承（同簇、无显式转移）——事故修复关键。
        for back in range(index - 1, max(-1, index - 1 - _INHERIT_LOOKBACK), -1):
            if cluster_of.get(back) != cluster_of.get(index):
                continue
            if candidates[back].person is not None:
                inherited = candidates[back].person
                inheritance_evidence = ("继承簇内上一条锚点",)
                break
            if candidates[back].unresolved:
                break

        person = candidate.person
        confidence = candidate.confidence
        evidence = candidate.evidence
        source = "显式" if person is not None else ""
        if person is None and not candidate.unresolved:
            same_cluster = index > 0 and cluster_of.get(index - 1) == cluster_of.get(index)
            # 「这一簇接得住上一条」的证据（三选一；都不是 → 宁可不判）：
            #   ① 同簇（cluster 内推进会走上面的 inherited 分支，这里是兜底）；
            #   ② 无独立话题内容的归线型簇；
            #   ③ 与上一条共享实义 2-gram（词面连接本身就是"同一件事"）；
            #   ④ 这一簇里有"对她说"的证据（@她/引用她/直呼她）= 接在她的话后面。
            said_to_her = any(
                _addresses_self(
                    rows[member],
                    self_name=focus_input.self_name,
                    self_person_id=focus_input.self_person_id,
                )
                for member in cluster.members[:3]
            )
            linked = bool(
                same_cluster
                or not cluster.rep_key
                or (prev_key and share_key(cluster.rep_key, prev_key))
                or said_to_her
                # 上一条**自己没判出候选**（一串谁也定不下来的话）时说不上"换锚"，
                # 这一簇没有自己的证据就不该把锚点从有变成没有。
                or not prev_had_candidate
            )
            if inherited is not None:
                person, confidence, source = inherited, CONF_MEDIUM, "簇内继承"
                evidence = inheritance_evidence
            elif cluster.anchor is not None and same_cluster:
                person, confidence, source = cluster.anchor, CONF_MEDIUM, "簇内继承"
                evidence = ("继承本簇锚点",)
            elif carried is not None and linked:
                # 顺延上一条的锚点。**这不是"最近说话人"**（§4.3 禁止条款 1/2）：
                # 接续本身是结构/词面证据；没有接续就宁可不判。
                person, confidence, source = carried, CONF_MEDIUM, "顺延"
                evidence = ("顺延上一话题簇的锚点",)
        # §4.2 优先序的**实现修正**（N3 第三期 [2]）：六序是 ①→⑥，**当轮自己的
        # 显式候选（①②③）排在"继承来的簇锚点"（④）前面**。旧写法只比
        # ``_slot_rank``：候选与簇锚点**同强度**（都是 medium）时 `>` 不成立 →
        # 簇锚点原地不动，"这一轮明明点了别人"被继承来的旧主角压住
        # （实测 14 条：@ 了 A、簇锚点是 B → 输出 B）。
        # 判据仍只有置信度**排序**这一条（不新造强度、不新造阈值）：
        # 候选强度 **≥** 簇锚点强度 → 当轮候选胜出。
        if person is not None:
            if _slot_rank(confidence) >= _slot_rank(cluster.confidence):
                cluster.anchor = person
                cluster.confidence = confidence
            elif cluster.anchor is None:
                cluster.anchor = person
            if evidence:
                cluster.evidence = tuple(evidence)
            carried = person
            if source == "显式":
                explicit_anchor = person
        prev_key = keys[index]
        prev_had_candidate = candidate.person is not None

    # ---- 「该不该允许换人」（§五 不换人规则）**不在这里算** ----
    # 主判据是**最终结果**（下面 `anchor` 算出来之后才成立），所以那段挪到
    # `last_slot` 之后。原因：`switched` 曾经拿"本轮显式候选 moved"当**代理判据**，
    # 而候选会被"继承来的簇锚点"压住 → **报换人但锚点根本没换**（幽灵换人）。

    slots: list[FocusSlot] = []
    for cluster in clusters:
        topic = _topic_of(rows, cluster.members)
        slots.append(
            FocusSlot(
                slot=SLOT_CURRENT,
                thread_id=cluster.thread_id,
                anchor=cluster.anchor,
                anchor_confidence=(
                    cluster.confidence
                    if cluster.anchor is not None
                    else CONF_UNRESOLVED
                ),
                keywords=_keywords(rows, cluster.members),
                topic=topic,
                evidence=cluster.evidence,
                updated_at=cluster.updated_at,
                expires_at=(cluster.updated_at or now) + SLOT_TTL,
            )
        )

    # ---- primary_thread：含当前这条、锚点最强的簇（§4.4）----
    last_group = cluster_of.get(len(rows) - 1)
    candidates_slots = (
        [slots[last_group]] if last_group is not None and last_group < len(slots) else []
    )
    for index in range(len(rows) - 2, -1, -1):
        group_index = cluster_of.get(index)
        if group_index is None or group_index == last_group:
            continue
        # 与当前这条共享词面的另一个簇 = 并行话题候选（§3.2【已定 3】多簇）。
        if share_key(keys[index], keys[len(rows) - 1]):
            candidates_slots.append(slots[group_index])
            break
    primary, ambiguous = _pick_primary(candidates_slots)

    last_slot = (
        slots[last_group] if last_group is not None and last_group < len(slots) else None
    )
    anchor = last_slot.anchor if last_slot is not None else None
    confidence: AnchorConfidence = (
        last_slot.anchor_confidence if last_slot is not None else CONF_UNRESOLVED
    )
    evidence = tuple(last_slot.evidence) if last_slot is not None else ()

    # ---- 「该不该允许换人」（§五 不换人规则）：**判据 = 最终结果** ----
    # 主判据：**最终锚点**（`anchor`：本轮焦点行给出去的那个主角）与**上一轮焦点行**
    # 不是同一个人 → 记一次换人。不再拿"本轮显式候选 moved"当**代理判据**：候选会被
    # 上面 ④「继承来的簇锚点」压住，于是出现**报换人但锚点根本没换**的记账
    # （"幽灵换人"，2026-10-06 第二期实测 4 条：t0019 / t0026 / t0051 / t0056，
    #  = 当时换人误判 8 条里的一半）。
    # 另一个反例（同样不许）：把"我自己这一轮算出的中间态锚点变了"记成换人——
    # 那等于让判定噪声变成"允许换人"（更早实测：那样会给出 0.87 的换人误判率）。
    #
    # 唯一保留旧代理的格子：**最终锚点没判出来**（`anchor is None`，§六 诚实失败）时
    # 没有"最终结果"可比，只能沿用窗口里最后一条显式候选。这一格**故意不动**——
    # 它是「整窗取证」（M-b）的地盘，两种"顺手改"都会污染归因（本期实测）：
    #   · 改成"未判定也算换人"（拿 `None` 当"变了"）→ 多出 7 条误判（0.129→0.177，更差）；
    #   · 改成"未判定不算换人"（要求换到某个具体人）→ 连 M-b 的 t0052 一起吞掉（3 条，
    #     指标会以 0.048 假通过 ≤0.05，而 M-b 的缺陷在读数里消失）。
    switched = False
    switch_evidence: list[str] = []
    if previous_anchor is not None:
        if anchor is not None:
            moved, proven = anchor, True
        else:
            moved, proven = (
                explicit_anchor
                if explicit_anchor is not None
                else (clusters[-1].anchor if clusters else None)
            ), explicit_takeover
        if proven and moved is not None and not _same_person(moved, previous_anchor):
            switched = True
            switch_evidence.append(
                f"锚点从 {_person_label(previous_anchor)} 变为 {_person_label(moved)}"
            )

    addressee = _addressee_of(attributions[len(rows) - 1])

    stack: list[FocusSlot] = []
    if last_slot is not None:
        stack.append(
            FocusSlot(
                slot=SLOT_CURRENT,
                thread_id=last_slot.thread_id,
                anchor=last_slot.anchor,
                anchor_confidence=last_slot.anchor_confidence,
                keywords=last_slot.keywords,
                topic=last_slot.topic,
                evidence=last_slot.evidence,
                updated_at=last_slot.updated_at,
                expires_at=last_slot.expires_at,
            )
        )
    if previous_slot is not None and (
        last_slot is None or previous_slot.topic != last_slot.topic
    ):
        stack.append(previous_slot)

    # ---- 方向 1/2A：变化检测与允许换人拆成两个输出（2026-10-14 语义对齐提案）----
    anchor_changed = bool(switched)
    takeover_evidence: list[str] = []
    prev_label = _person_label(previous_anchor) if previous_anchor is not None else ""
    for item in evidence:
        text = str(item)
        label = ""
        if text.startswith("引用"):
            label = text[2:].strip()
        elif "@" in text:
            label = text.split("@", 1)[1].strip()
        elif "点名" in text:
            label = text.split("点名", 1)[1].strip()
        elif "拍" in text:
            label = text.split("拍", 1)[1].strip()
        parts = label.split()
        label = parts[-1].strip("，。、）,;）(") if parts else ""
        if not label or (prev_label and label == prev_label):
            continue
        if text.startswith("引用"):
            takeover_evidence.append(f"引用对方原话（{label}）")
        elif "@" in text:
            takeover_evidence.append(f"@ 到新对象（{label}）")
        elif "点名" in text:
            takeover_evidence.append(f"被点名（{label}）")
        else:
            takeover_evidence.append(f"拍目标（{label}）")
    for phrase in ("我这边", "我来", "我接手", "这事我办", "我处理"):
        if phrase in _text(rows[-1]):
            takeover_evidence.append(f"自称接管（{phrase}）")
            break
    takeover_allowed = bool(takeover_evidence)
    last_row = rows[-1]
    if isinstance(last_row, Mapping):
        speaker_label = str(last_row.get("speaker") or last_row.get("sender_name") or "")
    else:
        speaker_label = str(
            getattr(last_row, "speaker", "") or getattr(last_row, "sender_name", "") or ""
        )
    if anchor is not None:
        about = anchor
        about_is_speaker = False
    else:
        about = speaker_label or None
        about_is_speaker = bool(speaker_label)

    return FocusResult(
        focus_id=_ref(rows[-1]),
        thread_id=last_slot.thread_id if last_slot is not None else "",
        anchor=anchor,
        anchor_confidence=confidence,
        anchor_evidence=evidence,
        stack=tuple(stack),
        primary_thread=primary,
        thread_ambiguous=ambiguous,
        switched=bool(switched),
        switch_evidence=tuple(switch_evidence),
        anchor_changed=anchor_changed,
        takeover_allowed=takeover_allowed,
        takeover_evidence=tuple(takeover_evidence),
        about=about,
        about_is_speaker=about_is_speaker,
        topic=last_slot.topic if last_slot is not None else "",
        threads=tuple(slots),
        addressee=addressee,
        degraded=False,
    )


def _topic_of(rows: Sequence[Any], members: Sequence[int]) -> str:
    """簇的「在聊什么」一句话：代表（最新有内容的一条）文本，裁到 40 字。

    **不做 LLM 出话**（规格 §4.2 全确定性）：这里给的是**事实行**，
    出话随 WP-10 第 5 行退役（话题线 LLM / flag 已删）。
    """
    for index in reversed(list(members)):
        text = _text(rows[index])
        if text.strip() and not _is_media_placeholder(text):
            flat = " ".join(text.split())
            return flat if len(flat) <= 40 else flat[:39] + "…"
    return ""


def _addressee_of(attribution: Any) -> tuple[str, ...]:
    """这条消息的收件人（**认不出 → ``("room",)``**，不回落最近说话人）。"""
    if attribution is None:
        return ("room",)
    if str(getattr(attribution, "confidence", "")) == "unresolved":
        return ("room",)
    names = tuple(
        _person_label(person)
        for person in (getattr(attribution, "addressees", ()) or ())
        if _person_label(person)
    )
    return names or ("room",)


# ===========================================================================
# 焦点块渲染（规格 §八【已定 7】：独立常驻块 ≤6 行）
# ===========================================================================
def render_focus_block(result: FocusResult | None, *, max_lines: int = BLOCK_MAX_LINES) -> str:
    """``[话题簇与锚点]`` 块文本；没有任何可说的 → ``""``（**整块不注入**）。

    规格 §六「失败即不注入」：开关关 / 取数失败 → 整块 ``""``，**不半开半关**。
    """
    if result is None:
        return ""
    lines: list[str] = [BLOCK_HEADER]
    slot = result.primary_slot
    if slot is not None and slot.thread_id:
        label = result.anchor_label()
        who = f"主角={label}" if label else "主角=不确定"
        conf = "" if result.anchor_confidence == CONF_UNRESOLVED else f"（{result.anchor_confidence}）"
        topic = f"「{slot.topic}」" if slot.topic else ""
        lines.append(f"- 当前话题：{topic}{who}{conf}")
        if result.anchor_evidence:
            lines.append("- 依据：" + "；".join(result.anchor_evidence[:2]))
    elif result.threads:
        lines.append("- 当前话题：未归线（这条没有独立话题内容）")
    else:
        lines.append("- 当前话题：不确定（窗口里没有可判定的话题簇）")

    parallel = [
        item
        for item in result.threads
        if item.thread_id and item.thread_id != result.primary_thread and item.topic
    ]
    if parallel:
        shown = "；".join(
            f"「{item.topic}」({item.anchor_label() or '主角不明'})" for item in parallel[:2]
        )
        lines.append(f"- 并行话题：{shown}")

    stack = [item for item in result.stack if item.slot == SLOT_PREVIOUS and item.topic]
    if stack:
        lines.append(f"- 上一个焦点：「{stack[0].topic}」")

    if result.thread_ambiguous:
        lines.append("- 话题并列：说不出唯一主角，**不要指派**，拿不准就问一句")
    elif result.anchor_confidence == CONF_UNRESOLVED:
        lines.append("- 主角不明：不要指派到具体人，拿不准就问一句或泛化说")
    else:
        lines.append(
            f"- 硬规则：没有显式转移不得换人（继续对 {result.anchor_label() or '他'} 说）；"
            "要换先问一句"
        )
    return "\n".join(lines[: max(2, int(max_lines))])


def render_focus_line(result: FocusResult | None) -> str:
    """一行事实版本（给不需要整块的消费方；判据同 :func:`render_focus_block`）。"""
    block = render_focus_block(result, max_lines=BLOCK_MAX_LINES)
    return block


# ===========================================================================
# 每会话一行（读 / 写走 db/repo_focus.py，不新造第二套读写）
# ===========================================================================
def load_prev_focus(platform: str, account_id: str, chat_id: str, *, repo: Any = None):
    """读上一轮的焦点行；**读失败降级为"当轮判定"**（``None``，不抛）。

    降级契约在 owner 处（``db/repo_focus.FocusStateRepository.get`` 永不抛）；
    这里同样**永不抛**——拿不到就是 ``None``，调用方走当轮判定。
    """
    if repo is None or not chat_id:
        return None
    try:
        return repo.get(str(platform or ""), str(account_id or ""), str(chat_id))
    except Exception:  # noqa: BLE001 - 状态缺失是预期内的降级
        warn("core/context/threads.py:1415 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def save_focus(
    result: FocusResult,
    *,
    platform: str,
    account_id: str,
    chat_id: str,
    repo: Any = None,
    now: datetime | None = None,
) -> bool:
    """把这一轮的焦点写成 ``session_focus`` 的**一行**；成功返回 ``True``。

    **读失败降级、写失败响亮**（``db/repo_focus.py`` 的硬约束）：``upsert`` 抛就让它
    抛——写不进去是配置/磁盘问题，需要被看见；调用方决定是否吞（回合路径必须吞，
    见 :func:`focus_block_from_context`）。
    """
    if repo is None or not chat_id:
        return False
    topic = str(result.topic or "")
    previous = ""
    for slot in result.stack:
        if slot.slot == SLOT_PREVIOUS:
            previous = str(slot.topic or "")
            break
    repo.upsert(
        str(platform or ""),
        str(account_id or ""),
        str(chat_id),
        current_topic=topic,
        # 锚点落库：id 优先；行里只出现过名字（没有 person_id）时退回显示名——
        # 下一轮读回来仍能认出"还是同一个人"（丢了名字就等于丢了跨轮继承）。
        current_anchor_person_id=_person_id(result.anchor, fallback_name=True),
        previous_topic=previous,
        now=now,
    )
    return True


def refine_topic_line(rows: Sequence[Any], *, limit: int = 2) -> str:
    """T1 的一句话输入（``CLUSTER_FACTS``）：**取本 owner 的事实再出话**。

    WP-10 第 5 行（2026-10-10）起话题线 LLM 与 flag 已退役；本函数保留为
    N3 的簇事实读口（``topic_line.cluster_facts`` 与焦点测试消费）。
    """
    try:
        rows = [row for row in (rows or ()) if row is not None]
        if not rows:
            return ""
        result = calculate_focus(FocusInput(window_rows=rows[:-1], current=rows[-1]))
        lines: list[str] = []
        for slot in result.threads[-max(1, int(limit)):]:
            if not slot.topic:
                continue
            anchor = slot.anchor_label() or "主角不明"
            lines.append(f"- 话题「{slot.topic}」主角={anchor}")
        if not lines:
            return ""
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - 事实取不到 = 没事实，退回旧形制
        warn("core/context/threads.py:1476 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


# ===========================================================================
# 生产接线入口（router 两处装配点同调；判定与落库仍全在本 owner 里）
# ===========================================================================
#: "当前这条已在窗口末条"的判等窗口与 ``chat_window._compose_parts`` 同口径，
#: 现由 ``core.timing`` 唯一持有（``TimingPolicy.duplicate_window``）。


def focus_row_of(message: Any, *, at: Any = None) -> dict[str, Any]:
    """把"这一轮这条"的消息对象规整成判定**行**（**只做形状适配，不做判定**）。

    ``InboundMessage`` / ``PendingQuestion`` / ``WindowMessage`` / 普通 dict 都吃
    （读法与 :func:`_get` 同口径：Mapping 取键、其余取属性）。轻拍 / 撤回按
    canonical 判据显式标记（与 :func:`_eval_row` 同一套来源：``db.parser`` 的
    ``_PAT_TITLE_RE`` 与 ``<sysmsg type="revokemsg">``），**不在本文件新写解析**。

    时间只从调用方给的那一个来源取（``at`` 优先）——回合路径上"这一轮是什么时候"
    已经算过一次，这里不再读第二次钟。
    """
    text = str(_get(message, "content", "") or _get(message, "text", "") or "")
    row: dict[str, Any] = {
        "content": text,
        "sender_name": _speaker(message)
        or str(_get(message, "user_name", "") or _get(message, "user_id", "") or ""),
        # 生产侧有真 person id（``InboundMessage.user_id`` / ``PendingQuestion.sender_id``）：
        # 给了它，窗口里的"名字 → person_id"映射才建得起来（``_identity_maps``）。
        "sender_id": str(
            _get(message, "sender_id", "") or _get(message, "user_id", "") or ""
        ),
        "created_at": (
            at
            if at is not None
            # ``InboundMessage`` 的时间字段叫 ``timestamp``（窗口行叫 ``occurred_at``、
            # 评测样本叫 ``at``）：三个名字都认，**不新造第二套时间口径**。
            else (_stamp(message) or _get(message, "timestamp"))
        ),
        "msg_id": str(
            _get(message, "msg_id", "") or _get(message, "event_id", "") or ""
        ),
        "is_self": bool(_get(message, "is_self", False)),
    }
    if "拍了拍" in text:
        try:
            from core.conversation.addressing import _PAT_TITLE_RE

            if _PAT_TITLE_RE.match(text):
                row["is_pat"] = True
                row["event_type"] = "group_pat"
        except Exception:  # noqa: BLE001 - 判据库缺失时按普通消息处理
            warn("core/context/threads.py:1527 Exception", err=repr(__import__("sys").exc_info()[1]))
    if '<sysmsg type="revokemsg">' in text:
        row["is_revoke"] = True
    return row


def _same_message(left: Any, right: Any) -> bool:
    """两行是不是**同一条消息**（同说话人 + 同正文 + 120s 内）。

    口径照 ``room_state.build_room_situation``（它只比窗口末条）与
    ``chat_window._compose_parts``（120s）——本文件不新造阈值。
    """
    if left is None or right is None:
        return False
    left_text = _text(left).strip()
    if not left_text or left_text != _text(right).strip():
        return False
    left_speaker, right_speaker = _speaker(left), _speaker(right)
    if left_speaker and right_speaker and left_speaker != right_speaker:
        return False
    left_at, right_at = _stamp(left), _stamp(right)
    if left_at is None or right_at is None:
        # 时间缺失：同说话人 + 同正文就按同一条（宁可少喂一条重复，不许多算一遍）。
        return True
    return abs((right_at - left_at).total_seconds()) < _duplicate_window_seconds()


def _without_current_duplicate(rows: Sequence[Any], current: Any) -> list[Any]:
    """窗口末条**就是**当前这条时，把它从窗口里去掉（当前这条只在 ``current`` 位喂）。

    不这样做的话，当前这条会被喂两遍（判定语义与评测集口径不一致：评测是
    ``hist + current``）。只比**末条**：若当前这条落在窗口中间（后面已经有人说话），
    去掉中间那条会把"当前"错认成别人——那种情况按原样喂，判定自己处理。
    """
    items = list(rows or ())
    if current is None or not items:
        return items
    if _same_message(items[-1], current):
        return items[:-1]
    return items


def focus_repo() -> Any:
    """焦点状态仓库（META 库 ``session_focus``）；**解析失败返回 ``None``，不抛**。

    解析口径照 ``core.context.chat_window.load_messages``（同一个 engine 的唯一来源
    ``core.bootstrap.get_meta_repo``）；导入放在函数内，避免 ``core.bootstrap`` 与
    本模块的顶层环。拿不到仓库 = 这一轮没有跨轮状态，**当轮判定照跑**（降级契约）。
    """
    try:
        from core.terminal.store_port import get_meta_repo
        from db.repo_focus import get_focus_repo

        return get_focus_repo(get_meta_repo().engine)
    except Exception:  # noqa: BLE001 - 没有仓库不是错误，是"没有跨轮状态"
        warn("core/context/threads.py:1582 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def attach_focus(
    context: Any = None,
    *,
    window_rows: Sequence[Any] = (),
    current: Any = None,
    at: Any = None,
    repo: Any = None,
    result: Any = None,
) -> bool:
    """把这一轮的焦点**输入**写进 ``context.extra``（生产接线入口；两处装配点同调）。

    只写三个键——判定、渲染、落库全在 :func:`focus_block_from_context` 里，本函数
    **不判定、不注入、不落库**：

    * ``focus_rows``：这一回合**看得见**的窗口行（复用调用方已经读过的那一份，
      本函数**不读窗口**、不产生第二次 IO）；
    * ``focus_current``：这一轮这条——``current`` 收**消息形或行形**，一律经
      :func:`focus_row_of` 规整后写回（调用方不必先自己适配；写回的是**行**，
      不是原来的对象）；
    * ``focus_repo``：``session_focus`` 的读写入口（拿不到就是 ``None``）。

    ``result=`` 是 S2.5 的**复用口**：封口那一刻已经算好的焦点结论
    （:func:`prefetch_focus` 的返回值）在这里原样写进 ``extra["focus_result"]``，
    本回合的 prompt 装配就**不再算第二遍**（:func:`focus_block_from_context` 只渲染）。
    不给（``None``）= 老行为：装配时现算。

    两个硬约束：

    * ``off``（``YINYUE_FEATURE_THREADS``）→ **一个键都不写**（`extra` 与接线前
      逐字节一致，回滚位）；
    * 任何异常 → 一个键都不写（降级为无焦点）+ ``focus_wiring_failed`` 留痕，
      **绝不抛**：焦点接线失败只损失一块，不打断这一轮。
    """
    if not threads_enabled():
        return False
    try:
        extra = getattr(context, "extra", None)
        if not isinstance(extra, MutableMapping):
            log.warning(
                "focus_wiring_failed", error="context.extra is not a mutable mapping"
            )
            return False
        row = focus_row_of(current, at=at) if current is not None else None
        rows = _without_current_duplicate(window_rows, row)
        target = repo if repo is not None else focus_repo()
        # 三个键一起写（前面的取值都可失败；失败了就一个键都不留）。
        extra["focus_rows"] = rows
        extra["focus_current"] = row
        extra["focus_repo"] = target
        # 已算好的那一份（封口预取）：给了才写 —— 没给时 extra 与接线前逐字段相同。
        if result is not None:
            extra["focus_result"] = result
        return True
    except Exception as exc:  # noqa: BLE001 - 接线失败 = 这一轮没有焦点块
        log.warning(
            "focus_wiring_failed", error=f"{type(exc).__name__}: {exc}"[:160]
        )
        return False


# ===========================================================================
# 消费方入口（装配器调用）
# ===========================================================================
def compute_focus(
    *,
    deadline: float | None = None,
    chat_id: str = "",
    platform: str = "",
    account_id: str = "",
    current: Any = None,
    window_rows: Sequence[Any] = (),
    repo: Any = None,
) -> FocusResult:
    """**唯一**的"算一次"序列：读上一轮焦点行 → 判定 → 落库（S2.5 起两个入口共用）。

    两个入口都经过这里，所以"同一回合算了几次"就等于 :func:`focus_compute_count`
    的增量：回合装配（:func:`focus_result_from_context`）与封口预取
    （:func:`prefetch_focus`）。**本函数不改任何判定口径**——三步序列与接线前
    :func:`focus_block_from_context` 里那一段逐字同序（``load_prev_focus`` →
    ``calculate_focus`` → ``save_focus``）。
    """
    prev = load_prev_focus(platform, account_id, chat_id, repo=repo)
    result = calculate_focus(
        FocusInput(
            chat_id=str(chat_id or ""),
            platform=str(platform or ""),
            account_id=str(account_id or ""),
            current=current,
            window_rows=tuple(window_rows or ()),
            prev_focus=prev,
        ),
        deadline=deadline,
    )
    if repo is not None and chat_id:
        try:
            save_focus(
                result,
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                repo=repo,
            )
        except Exception:  # noqa: BLE001 - 写失败不许弄坏这一轮（读侧才降级）
            warn("core/context/threads.py:1688 Exception", err=repr(__import__("sys").exc_info()[1]))
    return result


def focus_result_from_context(context: Any = None, *, repo: Any = None) -> FocusResult | None:
    """从回合上下文里取窗口 → 判定一次（``off`` / 没有输入 → ``None``，不抛）。

    这是 :func:`focus_block_from_context` 的"算"那一半（拆出来是为了让
    "同一回合只算一次"可被计数/复用）；取数顺序与原来逐字相同：
    ``extra["focus_rows"]`` → 单行 ``["focus_current"]``；末条若就是当前这条，
    读侧同样去掉（:func:`_without_current_duplicate`）。
    """
    if not threads_enabled():
        return None
    try:
        extra = getattr(context, "extra", None) or {}
        if not isinstance(extra, Mapping):
            return None
        rows = extra.get("focus_rows") or ()
        current = extra.get("focus_current")
        rows = _without_current_duplicate(rows, current)
        if not rows and current is None:
            return None
        chat_id = str(extra.get("chat_id") or "")
        platform = str(extra.get("platform") or "")
        principal = getattr(context, "principal", None)
        if principal is not None:
            chat_id = chat_id or str(getattr(principal, "chat_id", "") or "")
            platform = platform or str(getattr(principal, "platform", "") or "")
        account_id = str(extra.get("account_id") or "")
        focus_repo_ = repo if repo is not None else extra.get("focus_repo")
        return compute_focus(
            chat_id=chat_id,
            platform=platform,
            account_id=account_id,
            current=current,
            window_rows=rows,
            repo=focus_repo_,
        )
    except Exception:  # noqa: BLE001 - 任何失败 = 这一轮没有焦点（不注入）
        warn("core/context/threads.py:1728 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def prefetch_focus(
    *,
    deadline: float | None = None,
    chat_id: str,
    current: Any = None,
    window_rows: Sequence[Any] = (),
    at: Any = None,
    platform: str = "",
    account_id: str = "",
    repo: Any = None,
) -> FocusResult | None:
    """**封口那一刻**取一次焦点（S2.5）：算完交出去，同一回合的装配**复用**这一份。

    "群内回合归属"的第①问要一份**已算好的**焦点结论，而封口发生在回合装配之前：
    如果只在装配时算，判据 owner 拿到的永远是 ``focus=None``（生产读数停在
    ``unsure`` + ``focus_absent_text_overlap``）。所以预取在封口那一刻发生，
    结果随载荷（``PendingQuestion.focus_result``）交给同一回合的
    :func:`attach_focus` → ``extra["focus_result"]`` → :func:`focus_block_from_context`
    **只渲染不重算** —— 同一回合 :func:`calculate_focus` 的调用次数因此仍是 **1**。

    三条纪律：

    * **窗口由调用方给**（``window_rows``）：本函数**不读窗口**，"每回合至多一次
      额外读"这件事由调用方记账（生产上是 ``router._focus_window_rows``）；
    * ``off`` → ``None`` 且**一次 IO 都不发**（不读窗口行、不读焦点行、不落库）；
    * 任何异常 → ``None``（降级成"这一格没有事实"，判据侧照既有降级路径走），
      **绝不抛** —— 封口路径上抛异常 = 整个回合没了。
    """
    if not threads_enabled():
        return None
    if not str(chat_id or "").strip():
        return None
    try:
        row = focus_row_of(current, at=at) if current is not None else None
        rows = _without_current_duplicate(window_rows, row)
        if not rows and row is None:
            return None
        target = repo if repo is not None else focus_repo()
        return compute_focus(
            chat_id=str(chat_id),
            platform=str(platform or ""),
            account_id=str(account_id or ""),
            current=row,
            window_rows=rows,
            repo=target,
            deadline=deadline,
        )
    except Exception as exc:  # noqa: BLE001 - 取不到焦点 = 这一格没事实（不弄坏封口）
        log.warning(
            "focus_prefetch_failed", error=f"{type(exc).__name__}: {exc}"[:160]
        )
        return None


def focus_block_from_context(
    context: Any = None, *, repo: Any = None, result: Any = None,
) -> str:
    """从回合上下文里取窗口 → 判定 → 渲染 ``[话题簇与锚点]`` 块；失败返回 ``""``。

    **off = 一个字节都不走**（规格 §十二 回滚口径 + §六「失败即不注入」）。
    取数顺序（每一步都可缺，缺了就降级，绝不猜）：

    * ``result=``（或 ``extra["focus_result"]``）：**已经算好**的那一份（封口预取 /
      调用方注入）——给了它就直接渲染，**不再判定、不再读数、不再落库**（S2.5 的
      复用口；"同一回合只算一次"就靠这一句成立）；
    * 否则 窗口行：``context.extra["focus_rows"]`` → 单行 ``["focus_current"]``；
      末条若就是当前这条，读侧同样去掉（:func:`_without_current_duplicate`）——
      手工填 ``extra`` 的调用方与生产接线得到**同一套判定输入**；
    * 上一轮焦点行：``context.extra["focus_repo"]``（显式注入，生产由 router 传）
      → ``load_prev_focus``；拿不到 → 当轮判定；
    * 写出这一行：只有拿到 repo 才写（写失败**吞掉**：焦点行丢了只损失一句确认）。
    """
    if not threads_enabled():
        return ""
    try:
        if result is None:
            extra = getattr(context, "extra", None)
            if isinstance(extra, Mapping):
                result = extra.get("focus_result")
        if result is None:
            result = focus_result_from_context(context, repo=repo)
        # ``shadow`` 只算不注入（``.env:324-327``）：判定/落库已在上面走完，
        # 这里只决定"这一份结论要不要进 prompt"。
        if not threads_inject_enabled():
            return ""
        return render_focus_block(result)
    except Exception:  # noqa: BLE001 - 任何失败 = 整块不注入
        warn("core/context/threads.py:1818 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


# ===========================================================================
# 离线评测入口（scripts/score_threads_eval.py --predictor core.context.threads:predict）
# ===========================================================================
#: 跨轮焦点行（评测进程内模拟 ``session_focus`` 的"每会话一行"）。
_EVAL_STATE: dict[str, Any] = {}


def _eval_row(sample: Mapping[str, Any]) -> dict[str, Any]:
    """评测集样本 → 行（拍一拍/撤回**照 ``_is_pat``/``_is_revoke`` 的口径显式标**）。

    这不是第二套解析：拍/撤回的正则仍由 ``db.parser`` 与 canonical 判定，
    这里只补"评测样本没有 event_type"这一件事。
    """
    text = str(sample.get("text", "") or "")
    row: dict[str, Any] = {
        "content": text,
        "sender_name": str(sample.get("speaker", "") or ""),
        "created_at": str(sample.get("at", "") or ""),
        "event_id": str((sample.get("refs") or {}).get("event_id", "") or ""),
    }
    if "拍了拍" in text:
        try:
            from core.conversation.addressing import _PAT_TITLE_RE

            if _PAT_TITLE_RE.match(text):
                row["is_pat"] = True
                row["event_type"] = "group_pat"
        except Exception:  # noqa: BLE001 - 判据库缺失时按普通消息处理
            warn("core/context/threads.py:1849 Exception", err=repr(__import__("sys").exc_info()[1]))
    if "<sysmsg type=\"revokemsg\">" in text:
        row["is_revoke"] = True
    return row


def _eval_prev(chat_id: str) -> Any:
    return _EVAL_STATE.get(str(chat_id))


def reset_eval_state() -> None:
    """清空评测进程内的跨轮焦点状态（``score_threads_eval`` 每轮跑分前调一次）。

    没有它，同一个进程里跑第二遍会**继承第一遍最后一条的焦点** → 两次跑分不同数。
    "同输入同输出"是打分器的硬要求（读数必须可复现），所以状态清空挂在跑分入口上，
    而不是靠"每次开一个新进程"。
    """
    _EVAL_STATE.clear()


def _eval_remember(chat_id: str, at: str, result: FocusResult) -> None:
    """把这一轮的焦点写进进程内状态（模拟 ``session_focus`` 一行 → 下一轮读回）。"""
    try:
        from db.repo_focus import FocusState

        stamp = None
        try:
            stamp = datetime.fromisoformat(str(at))
        except ValueError:
            warn("core/context/threads.py:1878 ValueError", err=repr(__import__("sys").exc_info()[1]))
            stamp = None
        previous = _eval_prev(chat_id)
        _EVAL_STATE[str(chat_id)] = FocusState(
            platform="eval",
            account_id="eval",
            chat_id=str(chat_id),
            current_topic=str(result.topic or ""),
            current_anchor_person_id=_person_id(result.anchor),
            previous_topic="",
            topic_since=stamp,
            updated_at=stamp,
            revision=int(getattr(previous, "revision", 0) or 0) + 1,
        )
    except Exception:  # noqa: BLE001 - 记不住就等于下一轮走当轮判定（降级契约）
        warn("core/context/threads.py:1892 Exception", err=repr(__import__("sys").exc_info()[1]))


def predict(sample: Mapping[str, Any], hist: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """评测集预测器（契约见 ``scripts/score_threads_eval.py`` 文件头）。

    判定**全部**在 :func:`calculate_focus` 里；这里只做两件搬运：
    ① 评测样本形 → 行形；② 上一轮的焦点行（跨轮继承，§4.2 ④）。

    除契约五字段外，另带三个**诊断读数**（打分器/诊断脚本用，不是契约字段）：
    ``anchor_evidence`` / ``switch_evidence`` / ``topic``——判错时"为什么这么判"
    必须能从读数里查（否则归因只能靠猜，那是这份读数存在的唯一理由）。
    """
    chat_id = str(sample.get("chat", "") or "")
    result = calculate_focus(
        FocusInput(
            chat_id=chat_id,
            platform="eval",
            account_id="eval",
            current=_eval_row(sample),
            window_rows=tuple(_eval_row(item) for item in (hist or ())),
            prev_focus=_eval_prev(chat_id),
        )
    )
    _eval_remember(chat_id, str(sample.get("at", "") or ""), result)
    return {
        "thread_id": result.thread_id or None,
        "anchor": result.anchor_label() or None,
        "addressee": list(result.addressee or ()) or None,
        "should_switch": bool(result.switched),
        "confidence": (
            CONF_UNRESOLVED if result.anchor is None else str(result.anchor_confidence)
        ),
        "anchor_evidence": list(result.anchor_evidence or ()),
        "switch_evidence": list(result.switch_evidence or ()),
        "anchor_changed": bool(result.anchor_changed),
        "takeover_allowed": bool(result.takeover_allowed),
        "takeover_evidence": list(result.takeover_evidence or ()),
        "about": (
            str(result.about) if result.about_is_speaker and result.about
            else (result.anchor_label() or None)
        ),
        "about_is_speaker": bool(result.about_is_speaker),
        "topic": str(result.topic or ""),
    }


__all__ = [
    "BLOCK_HEADER",
    "BLOCK_MAX_LINES",
    "CONF_HIGH",
    "CONF_LOW",
    "CONF_MEDIUM",
    "CONF_UNRESOLVED",
    "FocusInput",
    "FocusResult",
    "FocusSlot",
    "SLOT_CURRENT",
    "SLOT_PREVIOUS",
    "SLOT_SUSPENDED",
    "SLOT_TTL",
    "attach_focus",
    "calculate_focus",
    "compute_focus",
    "focus_block_from_context",
    "focus_compute_count",
    "focus_repo",
    "focus_result_from_context",
    "focus_row_of",
    "load_prev_focus",
    "predict",
    "prefetch_focus",
    "refine_topic_line",
    "render_focus_block",
    "render_focus_line",
    "reset_eval_state",
    "reset_focus_compute_count",
    "save_focus",
    "threads_enabled",
    "threads_inject_enabled",
    "threads_mode",
]
