# -*- coding: utf-8 -*-
"""回合指代锚（TurnReferent）：这一轮在说哪条消息、哪张图/表情包，是谁的。

背景（2026-09-24 事故批次）：四个事故的共同根因是「可判定的事实被留给模型
从文本里猜，且没有闸门」。本模块把"这轮的对象"抽成结构化引用，让提示组装、
工具、出口闸门读同一份锚，而不是各自按时间/名字/文本重新猜：

* ``role="quoted"``   —— 消息引用的对象（这轮谈论的靶子，最高优先）；
* ``role="current"``  —— 当前消息自己带的媒体（它本人就是那张图/表情包）；
* ``role="attached"`` —— 批次内为它补齐的相邻媒体（image-first 排序的产物）。

只做确定性提取，不调用 LLM；读不到就留空（诚实失败由消费方负责），绝不编。

2026-09-30 起这里还own**纯文本代词**的绑定（``bind_pronouns`` /
``render_pronoun_facts``，见下方「文本代词绑定」）：媒体的锚是 md5，人的锚是
可见消息行 + 动作证据；两者同一纪律——可判定的绝不留给模型猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from safelog import warn

ROLE_QUOTED = "quoted"
ROLE_CURRENT = "current"
ROLE_ATTACHED = "attached"

KIND_IMAGE = "image"
KIND_STICKER = "sticker"

_ATTACHED_PATHS_KEY = "_attached_media_paths"

# --------------------------------------------------------------- 文本代词绑定
# 2026-09-30 18:34 事故（群 10000000001@chatroom）：P3 说「@鲸鲸 对啊对啊，
# 他在霸凌我」——「他」指 Elaina（她刚连拍四个人、唯独没拍 P3）。当时没有
# 任何规则把纯文本代词绑到具体人，prompt 里唯一一条代词规则还写着「他=你本人」，
# 她就把别人的抱怨认领到自己头上，回了「我拍没拍你，你自己心里没数吗」。
#
# 这里补上那条规则：**确定性、可解释、可单测、无 IO**。候选是最近若干可见行里
# 有真实作者的人（去掉当前说话人/她自己/系统通知）；只有**动作证据**（候选人的
# 行里含"对某人做某事"的强动作词，如「拍了拍 P6」）才算数，仅仅"在场"不算。
# 解析不出、或有多人可指 → 诚实失败（``referent_name=""``，渲染成「不明」），
# 把「不确定」交回模型去问——可判定的事实留给模型猜，正是这类事故的形态。

PRONOUNS: tuple[str, ...] = ("他", "她", "它")

# 「对某人做某事」的强动作词。**故意不收** 说/回/发/问/答 这类泛动词：群里几乎
# 每条消息都含，收了就等于把"最近说话的人"当成代词所指，那是猜不是判。
ACTION_TERMS: tuple[str, ...] = (
    "拍", "戳", "骂", "欺负", "霸凌", "打", "怼", "踢", "抢", "偷", "删",
    "禁言", "举报", "拉黑", "引用", "艾特",
)

MAX_PRONOUN_ROWS = 20
MAX_EVIDENCE_TARGETS = 4

_SYSTEM_SPEAKERS = frozenset({"", "系统", "（系统）", "(系统)", "system", "unknown"})
# 「其」+ 他/它 = 其他/其它，不是第三人称代词。
_NON_PRONOUN_PREFIX = frozenset({"其"})
# 「你 + 会伤到人的动作」：说话人直接指着收话人（=她）说。只收"对她做的坏事"，
# 不收 说/回/发 这类泛动词（「你说的那个他」不是在指控她）；中间夹了
# 他/她/它 的是"转述别人的行为"（「你说他打我」= 指控的是那个他），不算指控她。
_ACCUSE_RE = re.compile(
    r"你(?![^，。！？\s]{0,2}[他她它])[^，。！？\s]{0,2}"
    r"(拍|戳|骂|欺负|霸凌|打|怼|踢|删|禁言|举报|拉黑|抢|偷)"
)


@dataclass(frozen=True)
class PronounBinding:
    """One pronoun of this turn's message, bound to a person or honestly unknown."""

    pronoun: str = ""
    referent_name: str = ""   # "" = 未知（诚实失败，不猜）
    evidence_msg_id: str = ""
    evidence: str = ""
    # named_target / addressee_accusation / action_evidence / self_addressed /
    # ambiguous / no_candidate
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.referent_name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pronoun": self.pronoun,
            "referent_name": self.referent_name,
            "evidence_msg_id": self.evidence_msg_id,
            "evidence": self.evidence,
            "reason": self.reason,
            "resolved": self.resolved,
        }


@dataclass(frozen=True)
class _Row:
    """One visible message row, normalized from WindowMessage / dict / mapping."""

    speaker: str = ""
    content: str = ""
    msg_id: str = ""
    is_self: bool = False
    is_pat: bool = False
    is_revoke: bool = False


def _pick(source: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(source, Mapping):
            value = source.get(name)
        else:
            value = getattr(source, name, None)
        if value is not None:
            return value
    return default


def _row_from(raw: Any) -> _Row:
    if raw is None:
        return _Row()
    return _Row(
        speaker=_text(_pick(raw, "speaker", "sender_name", "author", "user_name")),
        content=_text(_pick(raw, "content", "text", "message")),
        msg_id=_text(_pick(raw, "msg_id", "message_id", "id")),
        is_self=bool(_pick(raw, "is_self", default=False)),
        is_pat=bool(_pick(raw, "is_pat", default=False)),
        is_revoke=bool(_pick(raw, "is_revoke", default=False)),
    )


def extract_pronouns(text: Any) -> tuple[str, ...]:
    """Third-person pronouns present in ``text``（去重、固定顺序）。

    「其他 / 其它」里的那个字不是代词（「其他人都走了」不该触发指代绑定），
    所以跳过紧跟在「其」后面的字。
    """
    raw = str(text or "")
    found: list[str] = []
    for index, char in enumerate(raw):
        if char not in PRONOUNS or char in found:
            continue
        if index and raw[index - 1] in _NON_PRONOUN_PREFIX:
            continue
        found.append(char)
    return tuple(found)


def _action_terms_in(content: Any) -> tuple[str, ...]:
    raw = str(content or "")
    return tuple(term for term in ACTION_TERMS if term in raw)


def _named_targets(text: Any) -> tuple[str, ...]:
    """``@某人`` / ``[引用 某人:`` 明确点到的名字（出现顺序）。

    2026-09-30 收口：解析归 canonical（``addressing.named_targets``）——本模块
    不再自带 @/引用 正则（两份正则会漂移成两套"谁被点到"）。
    """
    from core.conversation.addressing import named_targets

    return tuple(named_targets(text))


def _pat_targets(content: Any) -> tuple[str, ...]:
    """``拍了拍 "P6"`` → ``P6``（证据描述用，读不到就空）。"""
    from core.conversation.addressing import pat_targets

    return tuple(pat_targets(content))


def _is_person_row(row: _Row, self_name: str) -> bool:
    """能不能算「有真实作者的一行」——系统通知/她自己/空作者都不算。"""
    if row.is_self or row.is_revoke or not row.content:
        return False
    speaker = row.speaker.strip()
    if speaker in _SYSTEM_SPEAKERS or speaker == self_name:
        return False
    return True


def _addresses_self(text: Any, self_name: str, named: Sequence[str]) -> bool:
    """点名/@/引用她本人（B5 窄例外：这时「他/她/它」才可能指她）。"""
    me = str(self_name or "").strip()
    if not me:
        return False
    if me in tuple(named or ()):
        return True
    return str(text or "").strip().startswith(me)


def _accuses_self(text: Any, self_name: str, named: Sequence[str]) -> bool:
    """说话人明确对着她本人说「你拍我 / 你骂我」（收话人就是动作人）。

    有这条时**不得**把代词绑给别的候选人（如「@鲸鲸 你拍我干嘛」——说话人在指控
    她，不是在说 Elaina）；这也正是 B5「被点名时他=你」窄例外的保留形态。
    """
    if not _addresses_self(text, self_name, named):
        return False
    return bool(_ACCUSE_RE.search(str(text or "")))


def _describe_evidence(author: str, rows: Sequence[_Row]) -> str:
    targets: list[str] = []
    for row in rows:
        for target in _pat_targets(row.content):
            if target and target != author and target not in targets:
                targets.append(target)
    if targets:
        shown = "、".join(targets[:MAX_EVIDENCE_TARGETS])
        more = "…" if len(targets) > MAX_EVIDENCE_TARGETS else ""
        return f"{author} 最近 {len(rows)} 条消息在拍 {shown}{more}"
    terms = _action_terms_in(rows[0].content)
    snippet = " ".join(str(rows[0].content or "").split())[:24]
    word = terms[0] if terms else "动作"
    return f"{author} 最近的消息里有「{word}」：{snippet}"


def bind_pronouns(
    rows: Sequence[Any] | None,
    *,
    speaker: str = "",
    text: str = "",
    self_name: str = "鲸鲸",
    window: int = MAX_PRONOUN_ROWS,
    participants: Sequence[str] = (),
) -> list[PronounBinding]:
    """Bind every third-person pronoun of ``text`` to a concrete person.

    Pure and deterministic: same rows + same text ⇒ same bindings (no IO, no LLM).
    ``rows`` is the same visible-message sequence the prompt window is built from
    (``WindowMessage`` objects or dicts with speaker/content/msg_id).  Rules, in
    order:

    ① 消息里明确点名了**别人**（``@某人`` / ``[引用 某人:``）→ 绑那个人；
    ② 消息明确对着她本人说「你拍我 / 你骂我」→ 绑鲸鲸（B5 窄例外，指控的是她）；
    ③ 恰好一个候选人有动作证据（他的行里有强动作词，如「拍了拍 P6」）→ 绑他；
    ④ 多个候选人有动作证据 → 不明（``ambiguous``）；
    ⑤ 没证据但这条消息点名/@/引用了鲸鲸本人 → 绑鲸鲸（B5 窄例外）；
    ⑥ 其余 → 不明（``no_candidate``）。

    当前说话人、她本人、系统行（撤回/无作者）永远不是候选；当前消息不含
    「他/她/它」时返回空列表（调用方据此不注入、避免每轮噪声）。
    """
    pronouns = extract_pronouns(text)
    if not pronouns:
        return []

    limit = max(1, int(window or MAX_PRONOUN_ROWS))
    visible = [_row_from(raw) for raw in (rows or ())]
    visible = visible[-limit:]
    current = str(text or "")
    me = _text(self_name)
    speaker_name = _text(speaker)

    candidates: list[str] = []
    evidence: dict[str, list[_Row]] = {}
    for row in visible:
        if not _is_person_row(row, me) or row.speaker == speaker_name:
            continue
        if row.speaker not in candidates:
            candidates.append(row.speaker)
    for row in reversed(visible):
        if not _is_person_row(row, me) or row.speaker == speaker_name:
            continue
        if _action_terms_in(row.content):
            evidence.setdefault(row.speaker, []).append(row)

    all_named = _named_targets(current)
    known = set(candidates) | {
        _text(item) for item in (participants or ()) if _text(item)
    }
    named_other = [
        name
        for name in all_named
        if name != me and (not known or name in known)
    ]

    bindings: list[PronounBinding] = []
    for pronoun in pronouns:
        if named_other:
            target = named_other[0]
            bindings.append(PronounBinding(
                pronoun=pronoun,
                referent_name=target,
                evidence=f"这条消息点名了 {target}",
                reason="named_target",
            ))
            continue
        if _accuses_self(current, me, all_named):
            bindings.append(PronounBinding(
                pronoun=pronoun,
                referent_name=me,
                evidence=f"这条消息点了你、并且说「{pronoun}」这件事是你做的",
                reason="addressee_accusation",
            ))
            continue
        if len(evidence) == 1:
            author, author_rows = next(iter(evidence.items()))
            bindings.append(PronounBinding(
                pronoun=pronoun,
                referent_name=author,
                evidence_msg_id=author_rows[0].msg_id,
                evidence=_describe_evidence(author, author_rows),
                reason="action_evidence",
            ))
            continue
        if len(evidence) > 1:
            others = "、".join(list(evidence)[:MAX_EVIDENCE_TARGETS])
            bindings.append(PronounBinding(
                pronoun=pronoun,
                evidence=f"近期有多人可指（{others}）",
                reason="ambiguous",
            ))
            continue
        if _addresses_self(current, me, all_named):
            bindings.append(PronounBinding(
                pronoun=pronoun,
                referent_name=me,
                evidence="这条消息点了你/引用了你",
                reason="self_addressed",
            ))
            continue
        bindings.append(PronounBinding(
            pronoun=pronoun,
            evidence="近期没有可判定的人",
            reason="no_candidate",
        ))
    return bindings


def render_pronoun_facts(
    bindings: Sequence[PronounBinding] | None,
    *,
    speaker: str = "",
    self_name: str = "鲸鲸",
) -> str:
    """Render the ``[指代事实]`` block, or "" when there is nothing to say.

    命中 → 事实行（谁说的哪个代词=谁 + 依据 + 别对号入座）；解析不出 → 「不明」
    （不确定就先问，不要认领）。只在当前消息含文本代词时才会拿到 bindings。
    """
    lines: list[str] = []
    who = f"{_text(speaker)} 说的" if _text(speaker) else "这条消息里的"
    me = _text(self_name)
    for binding in bindings or ():
        pronoun = binding.pronoun or PRONOUNS[0]
        if binding.resolved and binding.referent_name == me:
            lines.append(
                f"- 指代事实：{who}「{pronoun}」= 你本人"
                f"（依据：{binding.evidence or '这条消息点了你'}）。"
                "被点到你的行为就用第一人称认领，不要推给别人。"
            )
        elif binding.resolved:
            lines.append(
                f"- 指代事实：{who}「{pronoun}」= {binding.referent_name}"
                f"（依据：{binding.evidence}）。别把别人对你的抱怨对号入座。"
            )
        else:
            lines.append(
                f"- 指代事实：{who}「{pronoun}」= 不明"
                f"（{binding.evidence or '解析不出具体人'}）。不确定就先问，不要认领。"
            )
    if not lines:
        return ""
    return "[指代事实]\n" + "\n".join(lines)


@dataclass(frozen=True)
class MediaRef:
    """One media object this turn is about."""

    role: str = ROLE_CURRENT
    kind: str = KIND_IMAGE
    md5: str = ""
    path: str = ""
    sender_id: str = ""
    sender_name: str = ""
    is_self: bool = False

    @property
    def empty(self) -> bool:
        return not (self.md5 or self.path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "kind": self.kind,
            "md5": self.md5,
            "path": self.path,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "is_self": bool(self.is_self),
        }


@dataclass(frozen=True)
class TurnReferent:
    """What this turn refers to.  One turn, one anchor; frozen on purpose."""

    chat_id: str = ""
    message_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    is_self: bool = False
    quoted_message_id: str = ""
    quoted_author: str = ""
    media: tuple[MediaRef, ...] = ()

    def _role(self, role: str) -> tuple[MediaRef, ...]:
        return tuple(item for item in self.media if item.role == role)

    @property
    def quoted_media(self) -> tuple[MediaRef, ...]:
        return self._role(ROLE_QUOTED)

    @property
    def has_media(self) -> bool:
        return bool(self.media)

    @property
    def primary_media(self) -> MediaRef | None:
        """The one media a consumer should look at first, or None."""
        for role in (ROLE_QUOTED, ROLE_CURRENT, ROLE_ATTACHED):
            for item in self._role(role):
                if not item.empty:
                    return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "is_self": bool(self.is_self),
            "quoted_message_id": self.quoted_message_id,
            "quoted_author": self.quoted_author,
            "media": [item.as_dict() for item in self.media],
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sticker_md5(text: Any) -> str:
    try:
        from core.conversation.media import extract_sticker_md5

        return extract_sticker_md5(str(text or ""))
    except Exception:  # noqa: BLE001
        warn("core/context/referent.py:453 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _quoted_media(text: Any):
    try:
        from core.conversation.media import extract_quoted_media

        return extract_quoted_media(str(text or ""))
    except Exception:  # noqa: BLE001
        warn("core/context/referent.py:462 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def _quote_fields(content: str) -> tuple[str, str]:
    """``(quoted_md5, quoted_author)`` readable from the ``[引用 …]`` prefix."""
    quoted = _quoted_media(content)
    if quoted is None:
        return "", ""
    return _text(getattr(quoted, "md5", "")).lower(), _text(
        getattr(quoted, "author", "")
    )


def referent_from_text(
    content: Any,
    *,
    chat_id: str = "",
    message_id: str = "",
    sender_id: str = "",
    sender_name: str = "",
    is_self: bool = False,
) -> TurnReferent:
    """Build an anchor from a message's text alone (group-trigger path).

    Only what the text itself states is used: the quoted md5/author and a
    sticker md5 written as ``[表情包:…]``.  Everything else stays empty.
    """
    text = str(content or "")
    media: list[MediaRef] = []
    quoted_md5, quoted_author = _quote_fields(text)
    if quoted_md5:
        media.append(MediaRef(
            role=ROLE_QUOTED, kind=KIND_STICKER, md5=quoted_md5,
            sender_name=quoted_author,
        ))
    current_md5 = _sticker_md5(text)
    if current_md5 and current_md5 != quoted_md5:
        media.append(MediaRef(
            role=ROLE_CURRENT, kind=KIND_STICKER, md5=current_md5,
            sender_id=sender_id, sender_name=sender_name, is_self=is_self,
        ))
    return TurnReferent(
        chat_id=chat_id,
        message_id=message_id,
        sender_id=sender_id,
        sender_name=sender_name,
        is_self=is_self,
        quoted_author=quoted_author,
        media=tuple(media),
    )


def referent_from_message(
    msg: Mapping[str, Any] | None,
    *,
    account_id: str = "",
) -> TurnReferent:
    """Build an anchor from an ingested message dict.

    The sync pipeline has already resolved some of the hard parts (quoted media
    path, quoted sticker md5, the message's own media); this records them as
    one anchor instead of leaving each consumer to re-derive them.
    """
    data: Mapping[str, Any] = msg if isinstance(msg, Mapping) else {}
    content = str(data.get("content") or "")
    sender_id = _text(data.get("sender_id"))
    sender_name = _text(data.get("sender_name"))
    is_self = bool(data.get("is_self")) or (
        bool(account_id) and sender_id == account_id
    )
    quoted_message_id = _text(
        data.get("quote_reply_id") or data.get("reply_to")
    )
    quoted_md5, quoted_author = _quote_fields(content)
    quoted_sticker = _text(data.get("_quoted_sticker_id")).lower()
    quoted_md5 = quoted_sticker or quoted_md5

    media: list[MediaRef] = []
    quoted_path = _text(data.get("_quoted_media_path"))
    if quoted_md5 or quoted_path:
        quoted_sender = _text(data.get("_quoted_sender_id"))
        media.append(MediaRef(
            role=ROLE_QUOTED,
            kind=KIND_STICKER if quoted_md5 else KIND_IMAGE,
            md5=quoted_md5,
            path=quoted_path,
            sender_id=quoted_sender,
            sender_name=quoted_author,
            is_self=bool(account_id and quoted_sender == account_id),
        ))

    current_md5 = _text(data.get("_attached_sticker_id")).lower()
    if not current_md5:
        current_md5 = _sticker_md5(content)
    if current_md5 and current_md5 == quoted_md5:
        # The md5 in the text is the *quoted* one; the message itself is not it.
        current_md5 = ""
    current_path = _text(data.get("media_path"))
    if current_md5 or current_path:
        media.append(MediaRef(
            role=ROLE_CURRENT,
            kind=KIND_STICKER if current_md5 else _text(
                data.get("msg_type_name")
            ) or KIND_IMAGE,
            md5=current_md5,
            path=current_path,
            sender_id=sender_id,
            sender_name=sender_name,
            is_self=is_self,
        ))

    for raw in data.get(_ATTACHED_PATHS_KEY) or ():
        path = _text(raw)
        if path:
            media.append(MediaRef(
                role=ROLE_ATTACHED, kind=KIND_IMAGE, path=path,
                sender_id=sender_id, sender_name=sender_name, is_self=is_self,
            ))

    return TurnReferent(
        chat_id=_text(data.get("chat_id")),
        message_id=_text(data.get("msg_id")),
        sender_id=sender_id,
        sender_name=sender_name,
        is_self=is_self,
        quoted_message_id=quoted_message_id,
        quoted_author=quoted_author,
        media=tuple(media),
    )


__all__ = [
    "ACTION_TERMS",
    "KIND_IMAGE",
    "KIND_STICKER",
    "MediaRef",
    "PRONOUNS",
    "PronounBinding",
    "ROLE_ATTACHED",
    "ROLE_CURRENT",
    "ROLE_QUOTED",
    "TurnReferent",
    "bind_pronouns",
    "extract_pronouns",
    "referent_from_message",
    "referent_from_text",
    "render_pronoun_facts",
]
