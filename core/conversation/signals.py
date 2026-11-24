"""Deterministic signal extraction for addressee resolution.

Signals are intentionally conservative and testable.  No LLM is involved here;
the resolver may use an LLM judge later only for the ambiguous middle band.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from core.contracts import MessageEvent, MessageKind
from core.conversation.domain import ConversationWindow

_PAT = MessageKind.PAT
# Kinds that still behave like typed text for scoring.  A quote-reply is a
# text message with a quoted block, not a media attachment; treating it as
# media added a -0.15 penalty to every quoted answer in real traces.
_TEXTUAL_KINDS = frozenset({MessageKind.TEXT, MessageKind.QUOTE_REPLY})
_BROADCAST_KINDS = frozenset({
    MessageKind.SYSTEM, MessageKind.FORWARD, MessageKind.LINK,
    MessageKind.MINI_PROGRAM, MessageKind.TRANSFER,
})
_BROADCAST_MARKERS = ("[合并转发]", "群公告", "以上是新消息", "-----")
_DEICTIC_MARKERS = ("你", "您", "刚才", "上面", "这个", "那个", "这里")
_TOPIC_SHIFT_MARKERS = ("对了", "另外", "话说", "换个话题", "说个事")
_QUESTION_MARKERS = ("？", "?", "吗", "呢", "什么", "怎么", "为什么", "哪", "谁",
                     "多少", "如何", "能不能", "可不可以", "有没有", "是不是")
_IMPERATIVE_MARKERS = ("帮我", "给我", "请帮", "帮忙", "查一下", "搜一下",
                       "发一下", "说一下", "记得", "提醒我", "告诉我")
_ACK_PHRASES = frozenset({
    "嗯", "嗯嗯", "嗯呐", "嗯哪", "嗯好", "好的", "好的呢", "好", "好滴", "好哒",
    "好哦", "收到", "行", "行行", "ok", "OK", "okk", "okok", "明白", "知道了",
    "懂了", "哦", "哦哦", "噢", "噢噢", "喔", "喔喔", "嗯呢", "好嘞", "行吧",
    "好吧", "可以",
})
# Real senders often type a short phrase with a trailing full stop / tilde.
# Keep the phrase itself exact, but ignore harmless tail punctuation first.
_TRAILING_PUNCTUATION = "。.!！~～,，、…"
_BANTER_MARKERS = ("哈哈", "呵呵", "嘿嘿", "笑死", "难崩", "绷不住", "无语", "乐死",
                   "笑不活")
_CLOSING_PHRASES = frozenset({
    "谢谢", "好的谢谢", "谢谢啦", "拜拜", "再见", "晚安", "没事了", "就这样",
    "先这样", "回头聊", "下次聊",
})
# Answers that mean "I have no information about the topic you asked".
# Only matched exactly (after tail punctuation normalization) and only when the
# bot actually asked this person a question; the loop guard then prevents the
# bot from asking another question instead of accepting the answer.
_UNKNOWN_ANSWER_PHRASES = frozenset({
    "不知道", "我也不知道", "也不知道", "不知道呢", "不知道啊", "不知道呀",
    "不清楚", "我也不清楚", "不太清楚", "不清楚呢",
    "不晓得", "不晓得呢", "不了解", "没了解过",
    "没想过", "没想过这个问题", "没想过这个", "没想过呢",
    "不记得", "不记得了", "记不清", "记不清了", "忘了", "忘记了",
    "说不准", "不一定", "没想法", "没什么想法", "没想好", "没考虑过",
})


@dataclass
class AddresseeSignals:
    explicit_mention: bool = False
    reply_to_bot: bool = False
    reply_link_to_bot: bool = False
    quote_to_bot: bool = False
    quote_to_other: bool = False
    name_call: bool = False
    pat_on_bot: bool = False
    window_open: bool = False
    same_sender: bool = False
    no_intervening: bool = False
    third_party: bool = False
    bot_asked_question: bool = False
    deictic_reference: bool = False
    expects_reply: bool = False
    ack: bool = False
    unknown_answer: bool = False
    command: bool = False
    closing: bool = False
    broadcast: bool = False
    other_addressed: bool = False
    topic_shift: bool = False
    banter: bool = False
    media: bool = False
    time_decay: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            name: (1.0 if value is True else 0.0 if value is False else float(value))
            for name, value in self.__dict__.items()
        }


def _is_question(text: str) -> bool:
    return any(marker in text for marker in _QUESTION_MARKERS)


def text_looks_like_question(text: str) -> bool:
    """Public helper reused when judging whether the bot's reply asked a question."""
    return _is_question(text or "")


_EMOJI_TAG_RE = re.compile(r"^\[[^\[\]\s]{1,12}\]$")
_BARE_PUNCT_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def _other_text_mentions(
    visible_content: str,
    aliases: Sequence[str],
    bot_ids: set[str],
) -> bool:
    """True when the newly typed text explicitly @-mentions a non-bot name.

    2026-09-30 收口：@ 的解析不再由本模块负责（canonical 在
    ``core.conversation.addressing``）——这里只读它的收件人事实。
    """
    from core.conversation.addressing import resolve_attribution

    attribution = resolve_attribution(
        {"content": visible_content},
        self_name=str(next(iter(aliases), "") or ""),
        self_aliases=tuple(aliases) + tuple(sorted(bot_ids)),
        is_group=True,
    )
    if attribution is None:
        return False
    return bool(attribution.addressees) and not any(
        addressee.is_self for addressee in attribution.addressees
    )


def _looks_like_emoji_tag(text: str) -> bool:
    """``[旺柴]`` / ``[破涕为笑]`` arrive as text but behave like stickers."""
    return bool(_EMOJI_TAG_RE.match((text or "").strip()))


def _is_bare_punctuation(text: str) -> bool:
    stripped = (text or "").strip()
    return bool(stripped) and bool(_BARE_PUNCT_RE.match(stripped))


def _quote_target(
    content: str,
    aliases: Sequence[str],
    bot_ids: set[str],
) -> tuple[bool, bool]:
    """Return ``(quote_to_bot, quote_to_other)`` from a legacy quote header.

    Real WeChat text often starts with ``[引用 鲸鲸: ...]`` or
    ``[引用 P5: ...]``.  The platform message id may not be reconciled yet,
    so the header is the most reliable local signal for who is being answered.

    2026-09-30 收口：引用作者的解析交给 canonical
    （``core.context.attribution.quote_facts``）。认不出作者的引用
    （``[引用〔作者不明〕: …]`` / ``[引用: …]`` / 引用的是媒体）**两者都不是**
    ——它是信息缺口，不是"新作者"。
    """
    from core.context.attribution import quote_facts, default_context

    names = [str(a) for a in aliases if str(a or "").strip()]
    ctx = default_context(
        self_name=names[0] if names else "",
        self_aliases=tuple(names[1:]) + tuple(sorted(str(b) for b in bot_ids if b)),
    )
    quoted, is_self = quote_facts(content, ctx=ctx)
    if not quoted.present:
        return False, False
    if is_self:
        return True, False
    if not quoted.resolvable:
        # `[引用〔作者不明〕]` / `[引用: …]` / 媒体占位：谁都不是。
        return False, False
    return False, True


def _strip_quote_block(content: str) -> str:
    """Return only the newly typed part after a leading ``[引用 ...]`` block.

    Quoted text must never drive mention / question / topic signals for the
    *current* message.  WeChat's quote block uses nested brackets for media
    placeholders, so count depth instead of taking the first ``]``.

    实现已归 canonical（``addressing.strip_quote_block``）；这里只留转发，
    避免第二份引用块解析漂移。
    """
    from core.conversation.addressing import strip_quote_block

    return strip_quote_block(content)


def _is_imperative(text: str) -> bool:
    return any(text.startswith(marker) or marker in text for marker in _IMPERATIVE_MARKERS)


def _looks_like_closing(text: str) -> bool:
    stripped = text.strip()
    return stripped in _CLOSING_PHRASES


def _looks_like_ack(text: str) -> bool:
    stripped = (text or "").strip().strip(_TRAILING_PUNCTUATION).strip()
    return stripped in _ACK_PHRASES


def _looks_like_unknown_answer(text: str) -> bool:
    stripped = (text or "").strip().strip(_TRAILING_PUNCTUATION).strip()
    return stripped in _UNKNOWN_ANSWER_PHRASES


def _time_decay(window: ConversationWindow | None, now: datetime,
                ttl_seconds: float) -> float:
    if window is None:
        return 0.0
    last = window.last_activity_at or window.opened_at
    if last is None:
        return 0.0
    age = (now - last).total_seconds()
    if age < 0:
        return 1.0
    return max(0.0, 1.0 - (age / max(1.0, ttl_seconds)))


def _other_sender_intervened(
    window: ConversationWindow | None,
    event: MessageEvent,
    recent: Sequence[MessageEvent],
) -> bool:
    if window is None or not recent:
        return False
    since = window.last_activity_at or window.opened_at
    if since is None:
        return False
    for item in recent:
        if item.sender_id and item.sender_id == event.sender_id:
            continue
        created = item.created_at
        if created is None:
            continue
        if created > since and (event.created_at is None or created <= event.created_at):
            return True
    return False


def extract_signals(
    event: MessageEvent,
    *,
    window: ConversationWindow | None = None,
    bot_ids: Iterable[str] = (),
    aliases: Iterable[str] = (),
    other_names: Iterable[str] = (),
    bot_message_ids: Iterable[str] = (),
    recent: Sequence[MessageEvent] = (),
    now: datetime | None = None,
    ttl_seconds: float = 180.0,
) -> AddresseeSignals:
    now = now or datetime.now()
    bot_ids = {str(x) for x in bot_ids if x}
    aliases = [str(x) for x in aliases if x]
    other_names = [str(x) for x in other_names if x]
    bot_message_ids = {str(x) for x in bot_message_ids if x}

    content = event.content or ""
    # Quoted text is context, not the current utterance.  Strip a leading
    # ``[引用 ...]`` block before extracting mention/question/topic signals.
    visible_content = _strip_quote_block(content)
    text = visible_content.strip()
    mentions = list(event.mentions)

    mention_bot = any(
        (m.user_id and m.user_id in bot_ids)
        or (m.person_id and m.person_id in bot_ids)
        or (m.display_name and m.display_name in aliases)
        for m in mentions
    )
    # The platform sync does not always populate the ``mentions`` column (real
    # traces from WeChat can have an empty list while the text still contains
    # "@鲸鲸").  Treat "@" + alias as an explicit mention fallback; a bare alias
    # without "@" still stays a weak ``name_call``（@ 的切词归 canonical）。
    from core.conversation.addressing import mentioned_names

    at_names = set(mentioned_names(visible_content))
    mine = set(aliases) | bot_ids
    text_mention_bot = any(name in mine for name in at_names)
    mention_bot = mention_bot or text_mention_bot
    mention_other = any(
        not ((m.user_id and m.user_id in bot_ids)
             or (m.person_id and m.person_id in bot_ids)
             or (m.display_name and m.display_name in aliases))
        for m in mentions
    )
    mention_other = mention_other or _other_text_mentions(
        visible_content, aliases, bot_ids,
    )
    other_name_hit = any(
        name and name not in aliases and name in visible_content
        for name in other_names
    )
    quote_to_bot, quote_to_other = _quote_target(content, aliases, bot_ids)

    window_open = bool(window is not None and window.is_open(now, ttl_seconds))
    same_sender = bool(
        window_open and window.person_id and event.sender_id == window.person_id
    )
    # A *reply pointer* at one of her messages is a genuine address: the sender
    # picked that message to answer.
    reply_link_to_bot = bool(
        event.reply_to_id
        and (
            event.reply_to_id in bot_message_ids
            or (window is not None and event.reply_to_id == window.last_bot_message_id)
        )
    )
    # `reply_to_bot` is the broad version, kept for the soft score: it is true
    # for a reply pointer *or* for merely containing a `[引用 鲸鲸: ...]` header.
    # Quoting is weaker evidence than replying, which is why the two are
    # separate -- see the hard-positive block in resolver.decide().
    reply_to_bot = bool(quote_to_bot or reply_link_to_bot)
    name_call = any(alias in visible_content for alias in aliases)
    pat_on_bot = bool(
        event.kind == _PAT and (name_call or mention_bot)
    )
    intervened = _other_sender_intervened(window, event, recent)
    no_intervening = bool(window_open and recent and not intervened)

    broadcast = (
        event.kind in _BROADCAST_KINDS
        or any(marker in visible_content for marker in _BROADCAST_MARKERS)
    )
    closing = _looks_like_closing(text) and not _is_question(text)
    ack = _looks_like_ack(text) and not _is_question(text)
    bare_punctuation = _is_bare_punctuation(text)
    expects_reply = bool(
        not broadcast
        and not closing
        and not bare_punctuation
        and (_is_question(text) or _is_imperative(text))
    )
    other_addressed = bool(
        (mention_other and not mention_bot)
        or other_name_hit
        or (quote_to_other and not mention_bot and not quote_to_bot)
    )
    bot_asked_question = bool(
        window_open
        and window is not None
        and window.last_bot_asked_question
        and not bare_punctuation
    )
    command = bool(
        _is_imperative(text) and not _is_question(text) and not broadcast
    )
    # "I don't know" only counts as an answer when the bot actually asked this
    # person a question; it must not downgrade unrelated chatter.
    unknown_answer = bool(
        bot_asked_question
        and same_sender
        and not other_addressed
        and not expects_reply
        and _looks_like_unknown_answer(text)
        and not _is_question(text)
    )
    deictic = any(marker in visible_content for marker in _DEICTIC_MARKERS)
    topic_shift = any(marker in visible_content for marker in _TOPIC_SHIFT_MARKERS)
    banter = any(marker in visible_content for marker in _BANTER_MARKERS)

    return AddresseeSignals(
        explicit_mention=mention_bot,
        reply_to_bot=reply_to_bot,
        reply_link_to_bot=reply_link_to_bot,
        quote_to_bot=quote_to_bot,
        quote_to_other=quote_to_other,
        name_call=name_call,
        pat_on_bot=pat_on_bot,
        window_open=window_open,
        same_sender=same_sender,
        no_intervening=no_intervening,
        third_party=bool(window_open and intervened),
        bot_asked_question=bot_asked_question,
        deictic_reference=deictic,
        expects_reply=expects_reply,
        ack=ack,
        unknown_answer=unknown_answer,
        command=command,
        closing=closing,
        broadcast=broadcast,
        other_addressed=other_addressed,
        topic_shift=topic_shift,
        banter=banter,
        media=(
            event.kind not in _TEXTUAL_KINDS
            or _looks_like_emoji_tag(text)
        ),
        time_decay=_time_decay(window, now, ttl_seconds),
    )
