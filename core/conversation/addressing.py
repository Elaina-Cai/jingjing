"""Who a group reply is aimed at, and whether a message is asking something.

Kept pure and separate because it is a judgement about people, not about
plumbing, and because both the reply path and the group trigger need the same
answers.

The case that motivated it
--------------------------
P3 asked "你头像是谁". Elaina then *quoted* that question and added a
comment of her own.  The reply came back addressed to Elaina -- because the only
thing the code knew was who spoke last.  A quote that carries a question is
aimed at the person quoted (they asked; the current speaker is relaying it), and
a quote used only to comment on something is aimed at the person who spoke.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from safelog import warn

__all__ = [
    "quoted_person", "is_question_or_appeal", "looks_like_directed_question",
    "mention_target", "QUESTION_MARKERS", "APPEAL_MARKERS",
    # ---- canonical 归属（谁对谁说话）----
    "Attribution", "Person", "QuotedRef", "resolve_attribution",
    "attributions_for_rows", "quote_of", "strip_quote_block", "mentioned_names",
    "pat_sender", "pat_targets", "pat_author_is_self",
    "named_targets", "split_quote_line",
    "strip_mentions", "has_quote_prefix",
    "SOURCE_MENTION", "SOURCE_QUOTE_BOT", "SOURCE_PAT_TARGET",
    "SOURCE_REPLY_PARENT", "SOURCE_PRIVATE_PEER",
    "KIND_GROUP_MESSAGE", "KIND_GROUP_PAT", "KIND_GROUP_QUOTE", "KIND_PRIVATE",
    "KIND_SYSTEM", "KIND_REVOKE",
    "CONFIDENCE_EXACT", "CONFIDENCE_UNRESOLVED",
]

# `[引用 P5: 宋艳萍，一教西1004]\n我们昨天刚上完` -- WeChat renders the quoted
# speaker into the message text, because quote_reply_user holds the *chat* id,
# not the person.
# canonical 版：允许空作者（历史行 `[引用: 原话]`，163 条里 63 条），空作者=认不出。
_QUOTE_PREFIX_RE = re.compile(r"^\[引用\s*(?P<name>[^:：\]\n]{0,32}?)\s*[:：]")

# A name that is not a person as far as addressing goes.
_NOT_A_PERSON = frozenset({"", "unknown", "?", "-", "none", "null"})
# 认不出作者的引用（chat_window 把 `[引用: …]` 标成 `[引用〔作者不明〕: …]`）。
# 它们是**信息缺口**，不是"新作者"——当成作者就会把归属安到别人头上。
# 媒体占位名（`[引用 图片: …]`）同样不是人：引用的是媒体，不是某人的话。
_NON_PERSON_QUOTE_NAMES = frozenset({
    "图片", "表情包", "视频", "文件", "链接", "语音", "位置", "合并转发",
    "媒体消息", "动画表情", "聊天记录",
})
_UNKNOWN_QUOTE_AUTHORS = _NON_PERSON_QUOTE_NAMES | frozenset({
    "〔作者不明〕", "[作者不明]", "作者不明", "未知",
})
# `@所有人` 这类广播收件人：明确**不是**她，但也不是某个人（person_id 留空）。
_BROADCAST_MENTIONS = frozenset({"所有人", "全体成员", "all", "everyone"})

QUESTION_MARKERS = frozenset({
    "？", "?", "吗", "呢", "啥", "怎么", "什么", "为什么", "哪",
    "如何", "多少", "几", "谁", "能不能", "可不可以",
    "有没有", "是不是", "会不会", "要不要",
})
APPEAL_MARKERS = frozenset({
    "帮我", "求助", "请问", "请教", "有人知道",
    "有没有人", "谁能", "谁会", "谁可以",
})


def quoted_person(content: str) -> str:
    """The display name of the person a quoted message quotes, or "".

    ``quote_reply_user`` in the database holds the chat id, so the name is only
    available in the rendered text.  Returns "" when the line does not quote
    anyone, or when what it quotes is not a person (a link card, say) -- 包括
    ``[引用〔作者不明〕: …]`` 这种认不出作者的行（canonical 判为 unresolved，
    绝不把占位名当成新作者）。
    """
    quoted = quote_of(content)
    return quoted.author_name if quoted.resolvable else ""


def is_question_or_appeal(content: str) -> bool:
    """Whether a message is asking for something rather than just saying it.

    Shared with the group trigger, which uses it to decide that a message is
    worth waiting to answer rather than ignoring.
    """
    text = str(content or "")
    return (any(marker in text for marker in QUESTION_MARKERS)
            or any(marker in text for marker in APPEAL_MARKERS))


# 纯标点/符号串（含孤立的「？」）——不构成提问。
_PUNCT_ONLY_RE = re.compile(
    r"^[\s?？.。,，!！~～…、:：;；'\"“”‘’()（）\[\]【】<>《》\-—_]+$"
)

# 句末疑问词：句尾是它们才算问句（句中出现不算——「我哪又拍他了」）。
_QUESTION_TAIL_WORDS = (
    "谁", "什么", "啥", "哪", "哪儿", "哪里", "多少", "几", "怎么", "为什么",
    "为啥", "咋", "如何", "多久", "多大", "是否", "有没有", "能不能", "要不要",
    "行不行", "对不对", "好不好",
)


def looks_like_directed_question(content: str) -> bool:
    """Stricter "is this asking something" for the group deferred gate.

    Unlike :func:`is_question_or_appeal` (wide substring hit; kept for the
    quote-target semantics in ``mention_target``), this only fires when the
    text *structurally* asks for something:

    * an explicit appeal marker (帮我 / 请问 / 有人知道 …);
    * a sentence-final question mark (a bare 「？」 does **not** count);
    * a sentence-final interrogative particle (吗 / 呢) or question word
      (谁 / 什么 / 怎么 / 多少 …).

    A statement that merely contains 哪 / 什么 / 呢 somewhere is not a
    question -- otherwise 「我哪又拍他了」 would be read as one (live case,
    2026-09-27) and parked in the deferred queue as if someone asked her.
    """
    raw = " ".join(str(content or "").split())
    if not raw or _PUNCT_ONLY_RE.match(raw):
        return False
    if any(marker in raw for marker in APPEAL_MARKERS):
        return True
    tail = raw.rstrip("。.!！~～…、,，;；")
    if tail.endswith(("？", "?")):
        return True
    if tail.endswith(("吗", "呢")):
        return True
    if any(tail.endswith(word) for word in _QUESTION_TAIL_WORDS):
        return True
    return False


def mention_target(*, speaker: str, content: str = "",
                   quoted: bool = False, self_name: str = "") -> str:
    """Whom a group reply should address.

    A quote that asks something is aimed at the person quoted; anything else is
    aimed at whoever just spoke.  Quoting herself is never a target -- she does
    not need to @ herself -- and neither is a placeholder name.
    """
    who = " ".join(str(speaker or "").split())
    if who.lower() in _NOT_A_PERSON:
        return ""
    if not quoted:
        return who

    target = quoted_person(content)
    if not target:
        return who
    if self_name and target == str(self_name).strip():
        # She quoted her own earlier reply; the speaker is still who she answers.
        return who
    if not is_question_or_appeal(content):
        # Quoted only to comment on it, so the comment is for the speaker.
        return who
    return target


# ===========================================================================
# canonical 归属：一条消息「谁对谁说」，只此一处判定
# ===========================================================================
# 2026-09-30 19:08–19:11 群 10000000001@chatroom 事故：P3 分明 @了两个人
# （7089 → @鲸鲸、7094 → @Elaina），但入站 @ 从不落库
# （``recipient_person_ids_json=[]``），于是各个消费方各自用正则或"最近说话人"
# 猜归属：thought 里写成「这句你刚**对P7**说过」——全库该句只出现过 3 次
# （7089→鲸鲸、7093 引用、7094→Elaina），从没对P7说过，P7只是 19:08:41
# 之前最近说话的人。
#
# 这里收口成**唯一**的归属判定：给一条消息行，给出 (谁, 对谁, 依据, 置信)。
# 纪律：
#   * 读时判定，不落库、不迁移、不改数据；
#   * 只有 ``exact`` / ``unresolved`` 两态，**没有"可能"**；
#   * ``unresolved`` 时 ``addressees=[]``：消费方必须显式渲染「归属不明」，
#     **禁止**回落成"最近说话的人"；
#   * 名字只作显示（display_name）；身份判等走 person_id；同名多人不消歧 →
#     unresolved（宁可说不确定，也不指派）；
#   * 本文件是 ``core/`` 下**唯一**允许写 ``@`` / ``[引用`` 归属正则的地方。

SOURCE_MENTION = "mention"
SOURCE_QUOTE_BOT = "quote_bot"
SOURCE_PAT_TARGET = "pat_target"
SOURCE_REPLY_PARENT = "reply_parent"
SOURCE_PRIVATE_PEER = "private_peer"

KIND_GROUP_MESSAGE = "group_message"
KIND_GROUP_PAT = "group_pat"      # 拍一拍（系统通知形态，群/私聊同一口径）
KIND_GROUP_QUOTE = "group_quote"
KIND_PRIVATE = "private"
KIND_SYSTEM = "system"
KIND_REVOKE = "revoke"

CONFIDENCE_EXACT = "exact"
CONFIDENCE_UNRESOLVED = "unresolved"

# 归属归属正则（唯一实现处）：
#   * `@昵称` —— 微信把 @ 渲染进正文；
#   * `[引用 昵称: …]` —— 引用的作者只存在于渲染文本里（quote_reply_user 存的是
#     会话 id，不是人）；
#   * `拍了拍` 标题 —— 拍一拍的作者/目标只在渲染文本里（见 db/parser）。
# 昵称在正文里以空白/全角空格结尾（微信渲染 `@昵称 `），但手打的 `@鲸鲸，在吗`
# 也常见——所以同样在标点处断开，别把整句话当成一个名字。
_MENTION_TOKEN_RE = re.compile(
    r"@([^\s@\u2005，。！？、；：,.!?;:\[\]【】（）()<>《》\"'“”‘’]{1,32})"
)
_MENTION_TRIM = "，。！？、；：,.!?;:[]【】（）()<>《》\"'“”‘’"
_PAT_TARGET_RE = re.compile(r"拍了拍\s*[\"'“”]?\s*(?P<target>[^\"'“”\s,，。]{1,24})")
_REVOKE_RE = re.compile(r'<sysmsg type="revokemsg">', re.S)
_PAT_TITLE_RE = re.compile(r'^\s*"?[^"\s]+"?\s*拍了拍')
_SELF_PAT_RE = re.compile(r"^\s*(?:我|自己)\s*拍了拍")
_SELF_NAMES = frozenset({"我", "自己", "本人"})


@dataclass(frozen=True)
class Person:
    """一个人（``person_id`` 可空：只在渲染文本里出现过的名字）。"""

    person_id: str = ""
    display_name: str = ""
    source: str = ""      # 仅 addressees 有意义：mention/quote_bot/pat_target/…
    is_self: bool = False

    def label(self) -> str:
        """给模型看的显示名（不带 person_id；那是工程字段）。"""
        return self.display_name or self.person_id or "?"


@dataclass(frozen=True)
class QuotedRef:
    """这条消息引用了谁的话（``present`` = 确实有引用，哪怕认不出作者）。"""

    author_name: str = ""
    author_person_id: str = ""
    msg_id: str = ""
    resolvable: bool = False
    present: bool = False


@dataclass(frozen=True)
class Attribution:
    """一条消息的归属事实（``message_ref`` = event_id，证据唯一键）。"""

    message_ref: str = ""
    speaker: Person = field(default_factory=Person)
    addressees: tuple[Person, ...] = ()
    quoted: QuotedRef = field(default_factory=QuotedRef)
    is_self_addressed: bool = False
    kind: str = KIND_GROUP_MESSAGE
    confidence: str = CONFIDENCE_EXACT
    evidence: str = ""
    # 收件人里有"同名多人"的名字：名字照写（display_name），但 person_id 不给
    # ——不消歧、也不指派，消费方要比 person_id 时自己按"缺 id"处理。
    identity_unresolved: bool = False

    @property
    def resolved(self) -> bool:
        return self.confidence == CONFIDENCE_EXACT

    @property
    def has_addressee(self) -> bool:
        return bool(self.addressees)

    def addressee_names(self) -> tuple[str, ...]:
        return tuple(a.label() for a in self.addressees)

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_ref": self.message_ref,
            "speaker": {
                "person_id": self.speaker.person_id,
                "display_name": self.speaker.display_name,
                "is_self": self.speaker.is_self,
            },
            "addressees": [
                {
                    "person_id": a.person_id,
                    "display_name": a.display_name,
                    "source": a.source,
                }
                for a in self.addressees
            ],
            "quoted": {
                "author_name": self.quoted.author_name,
                "author_person_id": self.quoted.author_person_id,
                "msg_id": self.quoted.msg_id,
                "resolvable": self.quoted.resolvable,
            },
            "is_self_addressed": self.is_self_addressed,
            "kind": self.kind,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "identity_unresolved": self.identity_unresolved,
        }


# ------------------------------------------------------------------ 引用/拍一拍
def quote_of(content: Any) -> QuotedRef:
    """``[引用 X: …]`` 的作者；认不出作者时 ``resolvable=False``（不装懂）。"""
    text = str(content or "").lstrip()
    match = _QUOTE_PREFIX_RE.match(text)
    if not match:
        return QuotedRef()
    name = " ".join(match.group("name").split()).strip('"“”\'')
    if not name or name.lower() in _NOT_A_PERSON or name in _UNKNOWN_QUOTE_AUTHORS:
        # `[引用: 原话]` / `[引用〔作者不明〕: 原话]` / `[引用 图片: …]`
        return QuotedRef(present=True, resolvable=False)
    return QuotedRef(author_name=name, present=True, resolvable=True)


def split_quote_line(content: Any) -> tuple[str, str]:
    """``[引用 X: 原话] 正文`` → ``(作者, 正文)``；拆不开返回 ``("", 原文)``。

    作者为空也可能是"有引用但认不出"——调用方要区分时看 :func:`quote_of`。
    """
    text = str(content or "")
    quoted = quote_of(text)
    if not quoted.present:
        return "", text
    match = _QUOTE_PREFIX_RE.match(text.lstrip())
    body = text
    if match:
        body = text.lstrip()[match.end():]
    return (quoted.author_name if quoted.resolvable else ""), body


def has_quote_prefix(content: Any) -> bool:
    """行首是不是引用块（包括 ``[引用: …]`` / ``[引用 3多]`` 这种认不出作者的）。"""
    return str(content or "").lstrip().startswith("[引用")


def _quote_block_span(text: str) -> tuple[int, int] | None:
    """``[引用 …]`` 块的 ``(起, 止)`` 下标（嵌套方括号按**深度配对**）；找不到 → None。

    「块到哪结束」这件事**只在这里算一次**：``strip_quote_block``（要块之后的
    新正文）与 ``quoted_body``（要块内被引用的原话）共用它。两处各写一遍
    ＝格式一改就漏一处。
    """
    start = text.find("[引用")
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return start, index
    return None


def strip_quote_block(content: Any) -> str:
    """只要引用块之后**新写**的正文（引用块用嵌套方括号，按深度配对）。

    引用的是上下文，不是本轮说的话：@/提问/话题信号都只看新写的那段。
    """
    text = str(content or "")
    span = _quote_block_span(text)
    if span is None:
        return text
    return text[span[1] + 1:].strip()


def quoted_body(content: Any) -> str:
    """``[引用 X: 原话] 新正文`` → **块内部**被引用的那段原话。

    与 :func:`split_quote_line` 互补：那个给"块之后新写的正文"，这个给"块内被引用
    的原话"。``core.conversation.media.extract_quoted_media`` 用它抠表情包 md5。

    与 :func:`quote_of` 的分工：``quote_of`` 判"作者认不认得出"（认不出也算 present），
    这个只管"块里写了什么"——认不出作者时照样返回原话。
    """
    text = str(content or "")
    span = _quote_block_span(text)
    if span is None:
        return ""
    block = text[span[0]:span[1] + 1]
    match = _QUOTE_PREFIX_RE.match(block)
    if not match:
        return ""
    inner = block[match.end():]
    if inner.endswith("]"):
        inner = inner[:-1]      # 只脱**块自己**的右括号；嵌套时 rstrip 会多脱
    return inner.strip()


def mentioned_names(text: Any) -> tuple[str, ...]:
    """正文里 ``@`` 到的名字（去重、按出现顺序；`@所有人` 也在内）。"""
    out: list[str] = []
    for match in _MENTION_TOKEN_RE.finditer(str(text or "")):
        name = match.group(1).strip().strip(_MENTION_TRIM)
        if name and name not in out:
            out.append(name)
    return tuple(out)


def strip_mentions(text: Any) -> str:
    """去掉正文里的 ``@昵称``（收件人称呼不是"同一件事"的判据）。

    ⚠ **只用于解析 @点名，不要当通用文本清洗用**：``_MENTION_TOKEN_RE`` 以标点断名，
    ``@`` 后面若是一整段中文会被**整段**当成昵称——例如
    「刚刚@我那位报的籍贯我记着了：长沙、株洲、湘潭」会被洗成「刚刚长沙株洲湘潭」，
    把"她说了什么"一起抹掉。需要"抹掉点名噪声但留住正文"时，用
    ``mentioned_names()`` 取名字再按名字删除（`core/speech/guards.py::_fingerprint`
    就是这么做的：它只用本模块判定"哪一段名字"，并对 ASCII 名字加以界定）。
    """
    return _MENTION_TOKEN_RE.sub(" ", str(text or ""))


def _default_self_name() -> str:
    """她自己那个显示名（**canonical 一份**：``core.terminal.speaker.BOT_NAME``）。

    与"窗口里哪句话是她说的"是同一份事实；lazy import（本模块顶层只依赖
    ``db.parser``），读不到 → 空串（少一个名字，绝不编）。
    """
    try:
        from core.terminal.speaker import BOT_NAME

        return _text(BOT_NAME)
    except Exception:  # noqa: BLE001 - 名字读不到 ≠ 判定失败
        warn("core/conversation/addressing.py:426 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def pat_sender(content: Any) -> str:
    """拍一拍的**作者**（取自 prose；自己拍的无作者 → ""）。"""
    try:
        from db.parser import extract_pat_sender

        return str(extract_pat_sender(str(content or "")) or "")
    except Exception:  # noqa: BLE001 - 解析库缺失时不猜
        warn("core/conversation/addressing.py:436 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def pat_targets(content: Any, self_names: Sequence[str] = ()) -> tuple[str, ...]:
    """拍一拍的**目标**（``拍了拍 "X"`` → X；读不到就空）。

    **自指不算**（2026-10-06 修）：作者是她、被拍的**也是她**时返回**空元组** ——
    没有人拍她，这条不是"她被人拍"（``"鲸鲸" 拍了拍 "鲸鲸"``，规则层自己的假阳性
    = 刁难卷题 11）。判据只有 :func:`pat_author_is_self` 一处。

    **不吞咽**：她拍**别人**（``"鲸鲸" 拍了拍 "P5"``）照旧返回 ``("P5",)`` ——
    那是"收件人是别人"的**事实**，归 ``other``（只不再算成"她被人拍"），
    所以过滤只落在"目标 resolutions 到她自己"这一种，不落在"作者是她"上。
    """
    text = str(content or "")
    targets = tuple(
        match.group("target").strip().strip('"“”')
        for match in _PAT_TARGET_RE.finditer(text)
    )
    if not targets or not pat_author_is_self(text, self_names):
        return targets
    names = {
        _text(name) for name in ([*self_names] if self_names else [])
    }
    names.add(_text(_default_self_name()))
    names.discard("")
    # 作者是她 ⇒ 只有"被拍的也是她（或渲染成'我'）"才判自指；拍别人仍照实返回。
    return () if any(target in names or target in _SELF_NAMES for target in targets) else targets


def pat_author_is_self(content: Any, self_names: Sequence[str] = ()) -> bool:
    """这条拍一拍的**发起人是不是她**（拍的自指判据，唯一一处）。

    判据只有一条、只看 prose（与 :func:`pat_sender` 同一份读法）：

    * ``我/自己 拍了拍 X``（本端账号发的）→ **是她**（没有可归属的作者）；
    * ``"鲸鲸" 拍了拍 …`` 且这个名字就是她自己（在 ``self_names`` 里，或默认名）→ 是她；
    * 读不出作者（``拍了拍`` 这种残句）→ 判不出 ⇒ **不认**（不装懂、不把所有人的拍
      都当成她发的）。

    ``self_names`` 省略时用默认名（``core.terminal.speaker.BOT_NAME``），**不在这里
    再写一个名字字面量**；读不到就只剩"我/自己"这一条命中。
    """
    text = str(content or "").split("\n", 1)[0].strip()
    if _SELF_PAT_RE.match(text):
        return True
    author = pat_sender(text)
    if not author:
        return False
    names = {_text(name) for name in (self_names or ()) if _text(name)}
    names.add(_text(_default_self_name()))
    return _text(author) in {name for name in names if name}


def named_targets(text: Any) -> tuple[str, ...]:
    """``@某人`` / ``[引用 某人:`` 明确点到的名字（出现顺序、去重）。"""
    names: list[str] = []
    quoted = quote_of(text)
    if quoted.resolvable and quoted.author_name:
        names.append(quoted.author_name)
    for name in mentioned_names(text):
        if name not in names:
            names.append(name)
    return tuple(names)


# ------------------------------------------------------------------ 行规范化
@dataclass(frozen=True)
class _RowView:
    content: str = ""
    event_id: str = ""
    msg_id: str = ""
    parent_msg_id: str = ""
    event_type: str = ""
    speaker: str = ""
    is_self: bool = False
    is_pat: bool = False
    is_revoke: bool = False
    actor_person_id: str = ""
    actor_account_id: str = ""
    recipients: tuple[str, ...] = ()
    quoted_author: str = ""          # 已被上游规范化过的行（room_state）
    quote_attributable: bool | None = None
    is_group: bool | None = None
    parent_author_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _pick(source: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(source, Mapping):
            if name in source:
                return source.get(name)
        else:
            value = getattr(source, name, None)
            if value is not None:
                return value
    return default


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def _is_bot_event(event_type: str, metadata: dict, is_self_field: Any) -> bool:
    try:
        from core.terminal.speaker import is_bot_event

        return bool(is_bot_event(event_type, metadata))
    except Exception:  # noqa: BLE001 - 判据库缺失时不猜
        warn("core/conversation/addressing.py:552 Exception", err=repr(__import__("sys").exc_info()[1]))
        return _as_bool(is_self_field) or str(event_type or "") in {
            "reply", "thought", "action", "tool_call", "emotion",
        }


def _row_view(row: Any) -> _RowView:
    """把 WindowMessage / 原始 terminal_events 行 / dict 统一成读取视图。"""
    if row is None:
        return _RowView()
    metadata: dict = {}
    raw_meta = _pick(row, "metadata_json", "metadata", default=None)
    if isinstance(raw_meta, str) and raw_meta.strip():
        try:
            parsed = json.loads(raw_meta)
            metadata = parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            warn("core/conversation/addressing.py:568 Exception", err=repr(__import__("sys").exc_info()[1]))
            metadata = {}
    elif isinstance(raw_meta, Mapping):
        metadata = dict(raw_meta)

    event_type = _text(_pick(row, "event_type", "kind", default=""))
    is_self_field = _pick(row, "is_self", default=None)
    content = str(_pick(row, "content", "text", "message", default="") or "")

    recipients: list[str] = []
    raw_recipients = _pick(
        row, "recipients", "recipient_person_ids", "recipient_person_ids_json",
        default=None,
    )
    if isinstance(raw_recipients, str):
        try:
            raw_recipients = json.loads(raw_recipients or "[]")
        except Exception:  # noqa: BLE001
            warn("core/conversation/addressing.py:585 Exception", err=repr(__import__("sys").exc_info()[1]))
            raw_recipients = []
    if isinstance(raw_recipients, (list, tuple, set)):
        recipients = [
            str(item) for item in raw_recipients if str(item or "").strip()
        ]

    is_revoke = _pick(row, "is_revoke", default=None)
    if is_revoke is None:
        is_revoke = bool(_REVOKE_RE.search(content))
    is_pat = _pick(row, "is_pat", default=None)
    if is_pat is None:
        stripped = content.strip()
        if "拍了拍" in stripped:
            is_pat = bool(_PAT_TITLE_RE.match(stripped)) or bool(
                _SELF_PAT_RE.match(stripped)
            )
        else:
            is_pat = False

    chat_type = _text(_pick(row, "chat_type", "chat_type_name", default=""))
    is_group = _pick(row, "is_group", default=None)
    if is_group is None and chat_type:
        is_group = chat_type == "group"

    return _RowView(
        content=content,
        event_id=_text(_pick(row, "event_id", "message_ref", default="")),
        msg_id=_text(_pick(row, "msg_id", "message_id", default="")),
        parent_msg_id=_text(_pick(row, "parent_msg_id", default="")) or _text(
            metadata.get("parent_msg_id")
        ),
        event_type=event_type,
        speaker=_text(_pick(row, "speaker", "sender_name", "author", "user_name")),
        is_self=_is_bot_event(event_type, metadata, is_self_field),
        is_pat=bool(is_pat),
        is_revoke=bool(is_revoke),
        actor_person_id=_text(
            _pick(row, "actor_person_id", "author_id", "sender_id", "person_id")
        ),
        actor_account_id=_text(_pick(row, "actor_account_id", default="")),
        recipients=tuple(recipients),
        quoted_author=_text(_pick(row, "quoted_author", default="")),
        quote_attributable=_pick(row, "quote_attributable", default=None),
        is_group=is_group,
        parent_author_id=_text(_pick(row, "parent_author_id", default="")),
        metadata=metadata,
    )


def _person_index(
    identities: Mapping[str, str] | None,
    display_names: Mapping[str, str] | None,
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """``名字 → person_id`` + ``名字 → {person_id}``（用来发现重名）。"""
    by_name: dict[str, str] = {}
    ambiguous: dict[str, set[str]] = {}
    for name, person_id in (identities or {}).items():
        key = _text(name)
        person = _text(person_id)
        if key and person:
            by_name.setdefault(key, person)
    for person_id, name in (display_names or {}).items():
        key = _text(name)
        person = _text(person_id)
        if key and person:
            by_name.setdefault(key, person)
            ambiguous.setdefault(key, set()).add(person)
    return by_name, ambiguous


def _resolve_person(
    name: str,
    *,
    self_names: frozenset[str],
    self_person_id: str,
    by_name: Mapping[str, str],
    ambiguous: Mapping[str, set[str]],
    source: str = "",
) -> tuple[Person, bool]:
    """名字 → Person；第二个返回值 = "同名多人、不消歧"（person_id 留空）。

    她自己那一档**优先级最高**：名字/别名对上她，就是她（再多的同名也不改这条，
    与路由/局况一直以来的口径一致）。别人的名字同名多人时：名字照写进
    ``display_name``（那是消息正文里明写的事实），但**不给 person_id**——不挑一个
    顶上（线上 ``person_identities`` 里 "Elaina" 就有 5 个 person_id）。
    """
    clean = _text(name).strip('"“”\'')
    if not clean:
        return Person(display_name="", source=source), False
    if clean in self_names:
        return (
            Person(
                person_id=self_person_id,
                display_name=clean,
                source=source,
                is_self=True,
            ),
            False,
        )
    person_id = by_name.get(clean, "")
    if person_id and len(ambiguous.get(clean, ())) > 1:
        return Person(display_name=clean, source=source), True
    return Person(person_id=person_id, display_name=clean, source=source), False


def _speaker_names(
    identities: Mapping[str, str] | None,
    display_names: Mapping[str, str] | None,
) -> dict[str, str]:
    """``actor id/wxid → 显示名``（``resolve_speaker`` 用）。"""
    out: dict[str, str] = {}
    for key, value in (display_names or {}).items():
        name = _text(value)
        if name:
            out.setdefault(_text(key), name)
    for identity_value, person_id in (identities or {}).items():
        name = out.get(_text(person_id))
        if name:
            out.setdefault(_text(identity_value), name)
    return out


def _resolve_speaker_name(view: "_RowView", names: Mapping[str, str]) -> str:
    """原始 DB 行的说话人（与 chat_window / 投影同一口径：bot 事件 = 她）。"""
    try:
        from core.terminal.speaker import resolve_speaker

        return _text(
            resolve_speaker(
                view.event_type,
                view.actor_person_id,
                metadata=dict(view.metadata or {}),
                names=dict(names),
                actor_account_id=view.actor_account_id,
            )
        )
    except Exception:  # noqa: BLE001 - 判据库缺失时不猜
        warn("core/conversation/addressing.py:722 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def resolve_attribution(
    row: Any = None,
    *,
    self_name: str = "",
    self_person_id: str = "",
    self_aliases: Sequence[str] = (),
    identities: Mapping[str, str] | None = None,
    display_names: Mapping[str, str] | None = None,
    is_group: bool | None = None,
    parent: Any = None,
) -> Attribution | None:
    """一条消息行的归属事实（``None`` = 这行没有可用信息）。

    判定顺序（全部来自**显式**证据，绝不回落到"最近说话的人"）：

    1. 作者：行自带的 speaker；否则按 ``core.terminal.speaker`` 口径（bot 事件
       → 她本人，其余 sendername→display_name→actor）。
    2. 拍一拍（``group_pat``）：作者取自 prose（``db.parser.extract_pat_sender``，
       自己拍的无作者 → 她本人），目标 = 被拍者 → ``pat_target``。
    3. 引用：``[引用 X: …]``；X 认不出（`[引用〔作者不明〕]` / `[引用: …]` /
       媒体占位名）→ **unresolved**，不当成新作者；X = 她自己 → ``quote_bot``。
    4. ``@昵称``（只看引用块之后新写的正文）：她 → 自己；别人 → ``mention``；
       ``@所有人`` → 广播收件人（person_id 空，明确不是她）。
    5. 正文里直呼她的名字/别名 → ``mention``（与 router 旧口径一致）。
    6. ``recipient_person_ids_json``：群聊里她这条回复 @ 的对象 →
       ``reply_parent``；私聊 → ``private_peer``。给不出人但有 ``parent``（确定
       的父链）时用父消息作者 → ``reply_parent``。

    没有任何显式证据 → ``addressees=[]`` + ``exact``（"没有明确目标"是正常
    事实，不是失败）；有指向信号却认不出人 → ``unresolved`` + ``addressees=[]``。

    重名（线上 ``person_identities`` 里 "Elaina" 有 5 个 person_id）：**不消歧、
    不指派**——收件人照写名字（``display_name``，那是正文里明写的事实），
    ``person_id`` 留空 + ``identity_unresolved=True``。只有"引用作者/被拍者根本
    认不出是谁"才判 ``unresolved``（此时 ``addressees=[]``）。
    """
    view = _row_view(row)
    if not view.content and not view.event_id:
        return None

    group = True if is_group is None else bool(is_group)
    if view.is_group is not None and is_group is None:
        group = bool(view.is_group)

    self_names = frozenset(
        name
        for name in (
            [_text(self_name)] + [_text(alias) for alias in (self_aliases or ())]
        )
        if name
    )
    by_name, ambiguous = _person_index(identities, display_names)
    speaker_names = _speaker_names(identities, display_names)

    # ---- 作者 ----
    speaker_is_self = view.is_self
    speaker_name = view.speaker
    if not speaker_name:
        speaker_name = _resolve_speaker_name(view, speaker_names)
    if not speaker_name:
        speaker_name = "鲸鲸" if speaker_is_self else ""
    evidence: list[str] = []

    # ---- 拍一拍 ----
    if view.is_pat:
        return _pat_attribution(
            view,
            group=group,
            self_names=self_names,
            self_person_id=self_person_id,
            self_name=_text(self_name) or "鲸鲸",
            by_name=by_name,
            ambiguous=ambiguous,
        )

    if view.is_revoke:
        return Attribution(
            message_ref=view.event_id,
            speaker=Person(
                person_id=self_person_id if speaker_is_self else view.actor_person_id,
                display_name=speaker_name,
                is_self=speaker_is_self,
            ),
            kind=KIND_REVOKE,
            confidence=CONFIDENCE_EXACT,
            evidence="撤回/系统通知：不指向任何人",
        )

    addressees: list[Person] = []
    seen_person: set[str] = set()
    unresolved = False
    identity_unresolved = False

    def _add(person: Person) -> None:
        key = person.person_id or f"name:{person.display_name}"
        if key in seen_person:
            return
        seen_person.add(key)
        addressees.append(person)

    # ---- 引用 ----
    if view.quoted_author:
        quoted = QuotedRef(author_name=view.quoted_author, present=True, resolvable=True)
    elif view.quote_attributable is False:
        quoted = QuotedRef(present=True, resolvable=False)
    else:
        quoted = quote_of(view.content)
    if quoted.author_name:
        evidence.append(f"content 含 [引用 {quoted.author_name}:")
        person, clash = _resolve_person(
            quoted.author_name,
            self_names=self_names,
            self_person_id=self_person_id,
            by_name=by_name,
            ambiguous=ambiguous,
        )
        if clash:
            # 引用的是谁有多个同名候选：不能判成"引用别人"，也不能判成"引用她"。
            unresolved = True
            identity_unresolved = True
            evidence.append(f"引用作者「{quoted.author_name}」同名多人，不消歧")
        elif person.is_self:
            quoted = QuotedRef(
                author_name=quoted.author_name,
                author_person_id=person.person_id,
                msg_id=view.parent_msg_id,
                resolvable=True,
                present=True,
            )
            _add(
                Person(
                    person_id=person.person_id,
                    display_name=person.display_name,
                    source=SOURCE_QUOTE_BOT,
                    is_self=True,
                )
            )
            evidence.append("引用了她自己 → 对她说")
        else:
            quoted = QuotedRef(
                author_name=quoted.author_name,
                author_person_id=person.person_id,
                msg_id=view.parent_msg_id,
                resolvable=True,
                present=True,
            )
    elif quoted.present:
        # `[引用〔作者不明〕: …]` / `[引用: …]`：认不出引用对象 = 信息缺口。
        unresolved = True
        evidence.append("content 有引用但认不出作者（[引用〔作者不明〕]）")

    # ---- @ ----
    typed = strip_quote_block(view.content)
    for name in mentioned_names(typed):
        person, clash = _resolve_person(
            name,
            self_names=self_names,
            self_person_id=self_person_id,
            by_name=by_name,
            ambiguous=ambiguous,
            source=SOURCE_MENTION,
        )
        if clash:
            # 同名多人：名字照写（@Elaina 就是 @Elaina），但不给 person_id。
            identity_unresolved = True
            evidence.append(f"content 前缀 @{name}（同名多人，person_id 未消歧）")
        elif person.is_self:
            evidence.append(f"content 前缀 @{name}（点到她）")
        elif name in _BROADCAST_MENTIONS:
            evidence.append(f"content 前缀 @{name}（广播，不是她）")
        else:
            evidence.append(f"content 前缀 @{name}")
        _add(person)

    # ---- 正文直呼她的名字 ----
    if not any(a.is_self for a in addressees) and typed:
        for name in self_names:
            if name and name in typed:
                _add(
                    Person(
                        person_id=self_person_id,
                        display_name=name,
                        source=SOURCE_MENTION,
                        is_self=True,
                    )
                )
                evidence.append(f"content 提到 {name}")
                break

    # ---- 结构化收件人（她自己发出的行 / 私聊对端）----
    if view.recipients:
        source = SOURCE_PRIVATE_PEER if not group else SOURCE_REPLY_PARENT
        for person_id in view.recipients:
            person_id = str(person_id)
            if group and view.event_type not in {"reply", "request", "question"}:
                source = SOURCE_MENTION
            display = (display_names or {}).get(person_id, "") or person_id
            _add(
                Person(
                    person_id=person_id,
                    display_name=display,
                    source=source,
                    is_self=bool(self_person_id and person_id == self_person_id),
                )
            )
        evidence.append(
            "她这条回复的 recipient_person_ids="
            f"{list(view.recipients)}" if view.is_self else
            f"结构化收件人 recipient_person_ids={list(view.recipients)}"
        )
    elif view.is_self and view.event_type == "reply" and (parent is not None or view.parent_author_id):
        parent_view = _row_view(parent) if parent is not None else _RowView()
        author_id = parent_view.actor_person_id or view.parent_author_id
        author_name = parent_view.speaker
        if author_id or author_name:
            _add(
                Person(
                    person_id=author_id,
                    display_name=author_name or author_id,
                    source=SOURCE_REPLY_PARENT,
                )
            )
            evidence.append("reply 的 parent_event_id 指向的那条的作者")

    is_self_addressed = any(a.is_self for a in addressees)
    # 诚实失败只在"一个确定收件人都没有"时成立：确定证据与"认不出的引用"可以
    # 并存（`[引用〔作者不明〕: …] @鲸鲸 你看`）——那时不能把确定的 @ 抹掉；
    # 认不出的那部分由 ``quoted.resolvable=False`` 与 evidence 记录。
    if unresolved and not addressees:
        confidence = CONFIDENCE_UNRESOLVED
        evidence.append("没有可确定的收件人 → 归属不明")
    else:
        confidence = CONFIDENCE_EXACT

    if view.is_revoke:
        kind = KIND_REVOKE
    elif not group:
        kind = KIND_PRIVATE
    elif quoted.present:
        kind = KIND_GROUP_QUOTE
    else:
        kind = KIND_GROUP_MESSAGE

    return Attribution(
        message_ref=view.event_id,
        speaker=Person(
            person_id=self_person_id if speaker_is_self else view.actor_person_id,
            display_name=speaker_name,
            is_self=speaker_is_self,
        ),
        addressees=tuple(addressees),
        quoted=quoted,
        is_self_addressed=is_self_addressed,
        kind=kind,
        confidence=confidence,
        evidence="；".join(evidence) or "无 @ 无引用 → 没有明确目标",
        identity_unresolved=identity_unresolved,
    )


def _pat_attribution(
    view: _RowView,
    *,
    group: bool,
    self_names: frozenset[str],
    self_person_id: str,
    self_name: str,
    by_name: Mapping[str, str],
    ambiguous: Mapping[str, set[str]],
) -> Attribution:
    """拍一拍：作者取自 prose，目标 = 被拍者（不猜、不沿用窗口作者）。

    **自指拍一拍**（``"鲸鲸" 拍了拍 "鲸鲸"``：作者是她、被拍的也是她）**不算
    "她被人拍"**（2026-10-06 修，刁难卷题 11 是规则层自己的假阳性）：
    这种标题里没有任何人指向她 ⇒ 判 ``unresolved``（``addressees=[]``），
    消费方照旧渲染"归属不明"，**不许**判 ``her``。
    别人拍她（作者不是她、目标是她）一字不改，仍判 ``is_self_addressed=True``。
    """
    author = pat_sender(view.content)
    # 自指判据复用 canonical 那一处（`self_names` = 她自己；本端账号的"我/自己"
    # 与"读不出作者但她自己发的行"同样算她发的）。
    targets = pat_targets(view.content, tuple(sorted(self_names)))
    is_self_pat = bool(_SELF_PAT_RE.match(view.content.strip())) or (
        not author and view.is_self
    )
    # 自指 = 作者是她（名字对上她自己，或本端账号渲染的"我/自己"）。
    author_is_self = bool(is_self_pat) or (
        bool(author) and _text(author) in self_names
    )
    speaker_name = author or (self_name if is_self_pat else view.speaker)
    addressees: list[Person] = []
    evidence = [f"拍一拍 prose：{_text(view.content)[:40]}"]
    unresolved = False
    identity_unresolved = False
    target_is_self = False
    for raw_target in targets:
        if raw_target in _SELF_NAMES:
            target_is_self = True
            addressees.append(
                Person(
                    person_id=self_person_id,
                    display_name=self_name,
                    source=SOURCE_PAT_TARGET,
                    is_self=True,
                )
            )
            evidence.append(f"拍了拍目标「{raw_target}」= 她自己")
            continue
        person, clash = _resolve_person(
            raw_target,
            self_names=self_names,
            self_person_id=self_person_id,
            by_name=by_name,
            ambiguous=ambiguous,
            source=SOURCE_PAT_TARGET,
        )
        if clash:
            identity_unresolved = True
            evidence.append(f"被拍者「{raw_target}」同名多人（person_id 未消歧）")
        target_is_self = target_is_self or person.is_self
        addressees.append(person)
        evidence.append(f"拍了拍目标「{person.label()}」")
    if author_is_self and not targets:
        # 自指：发起人也是她 ⇒ 没有任何人拍她（刁难卷题 11 的那条假阳性）。
        addressees = []
        unresolved = True
        evidence.append(
            f"自指：发起人「{speaker_name}」= 她自己、被拍的也是她自己 ⇒ 不是「她被人拍」"
        )
    elif not targets:
        unresolved = True
        evidence.append("拍一拍标题里读不出被拍者")
    if unresolved:
        addressees = []
    return Attribution(
        message_ref=view.event_id,
        speaker=Person(
            person_id=(
                self_person_id if (is_self_pat or view.is_self) else view.actor_person_id
            ),
            display_name=speaker_name,
            is_self=bool(is_self_pat or view.is_self),
        ),
        addressees=tuple(addressees),
        is_self_addressed=any(a.is_self for a in addressees),
        kind=KIND_GROUP_PAT,
        confidence=CONFIDENCE_UNRESOLVED if unresolved else CONFIDENCE_EXACT,
        evidence="；".join(evidence),
        identity_unresolved=identity_unresolved,
    )


def attributions_for_rows(
    rows: Sequence[Any] | None, **kwargs: Any
) -> list[Attribution]:
    """一组行的归属（``None`` 的行跳过；顺序保持）。"""
    out: list[Attribution] = []
    for row in rows or ():
        attribution = resolve_attribution(row, **kwargs)
        if attribution is not None:
            out.append(attribution)
    return out
