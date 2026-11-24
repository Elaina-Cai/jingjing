"""Shadow addressee-resolver checks (P3).

Run directly: python tests/test_addressee_resolver.py

These cases mirror the group/private golden set but execute against the pure
resolver, so the decision logic can be tuned before any router integration.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.contracts import (
    Addressee,
    ChatType,
    DecisionAction,
    DialogueAct,
    MentionRef,
    MessageEvent,
    MessageKind,
)
from core.conversation import AddresseeResolver, ConversationWindow


def _event(
    content: str,
    *,
    sender: str = "wxid_zhang",
    mentions: tuple[MentionRef, ...] = (),
    kind: MessageKind = MessageKind.TEXT,
    reply_to: str = "",
    message_id: str = "m1",
    created_at: datetime | None = None,
) -> MessageEvent:
    return MessageEvent.new(
        platform="wechat",
        account_id="wxid_bot",
        chat_id="group@chatroom",
        chat_type=ChatType.GROUP,
        message_id=message_id,
        sender_id=sender,
        sender_name="张三",
        content=content,
        mentions=mentions,
        kind=kind,
        reply_to_id=reply_to,
        created_at=created_at or datetime.now(),
    )


def _window(
    *,
    person: str = "wxid_zhang",
    asked: bool = False,
    now: datetime | None = None,
    last_bot: str = "bot-1",
) -> ConversationWindow:
    now = now or datetime.now()
    window = ConversationWindow(
        platform="wechat", account_id="wxid_bot",
        chat_id="group@chatroom", person_id=person,
    )
    window.opens_with(person, now=now)
    window.last_bot_message_id = last_bot
    window.last_bot_asked_question = asked
    window.last_activity_at = now
    return window


def _check(name: str, decision, action: DecisionAction,
           addressee: Addressee | None = None) -> bool:
    ok = decision.action == action
    if addressee is not None:
        ok &= decision.addressee == addressee
    print(f"  {'ok' if ok else 'FAIL'} {name}: {decision.action.value}"
          f"/{decision.addressee.value}/{decision.dialogue_act.value}"
          f" score={decision.score:.2f}")
    return bool(ok)


def main() -> bool:
    print("=" * 50)
    print("addressee resolver checks")
    print("=" * 50)
    resolver = AddresseeResolver()
    now = datetime.now()
    bot_ids = {"wxid_bot"}
    aliases = ["鲸鲸", "鲸鲸"]

    # 1. explicit @ mention
    d = resolver.evaluate(
        _event("帮我查下天气", mentions=(MentionRef(user_id="wxid_bot"),)),
        bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok = _check("at_bot", d, DecisionAction.REPLY, Addressee.BOT)
    ok &= d.dialogue_act == DialogueAct.QUESTION_TO_BOT

    # 1b. text "@alias" fallback when the platform payload has no mention list
    d = resolver.evaluate(
        _event("@鲸鲸", message_id="m1b"),
        bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("at_bot_text_fallback", d, DecisionAction.REPLY, Addressee.BOT)

    # 1c. quote replies are text, not media
    d = resolver.evaluate(
        _event("[引用 鲸鲸: 在呢] 今天天气如何", kind=MessageKind.QUOTE_REPLY,
               message_id="m1c", reply_to="bot-1"),
        bot_ids=bot_ids, aliases=aliases, bot_message_ids={"bot-1"}, now=now,
    )
    ok &= d.signals.get("media") == 0.0

    # 1d. quote header names the bot, even without a reconciled message id
    d = resolver.evaluate(
        _event("[引用 鲸鲸: 在呢] 今天天气如何", kind=MessageKind.QUOTE_REPLY,
               message_id="m1d"),
        bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("quote_text_to_bot", d, DecisionAction.REPLY, Addressee.BOT)

    # 1e. quote header names another person: not the bot's conversation
    d = resolver.evaluate(
        _event("[引用 P5: 你是在做训练吗？] 是的", kind=MessageKind.QUOTE_REPLY,
               message_id="m1e"),
        bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("quote_text_to_other", d, DecisionAction.DROP, Addressee.PERSON)

    # 1f. explicit @ bot still wins over the other-person quote
    d = resolver.evaluate(
        _event("[引用 P5: 你是在做训练吗？] @鲸鲸 你看这个", kind=MessageKind.QUOTE_REPLY,
               message_id="m1f"),
        bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("quote_other_with_at_bot", d, DecisionAction.REPLY, Addressee.BOT)

    # 1g. @ inside the quoted body must not leak into the current message
    d = resolver.evaluate(
        _event("[引用 P5: @鲸鲸 我的薯条要冷掉了] 冷了也能吃",
               kind=MessageKind.QUOTE_REPLY, message_id="m1g"),
        bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("quote_body_mention_ignored", d, DecisionAction.DROP,
                 Addressee.PERSON)

    # 1h. @ in the newly typed part still counts
    d = resolver.evaluate(
        _event("[引用 P5: [媒体消息]] 她这是学校饭堂自提的@鲸鲸",
               kind=MessageKind.QUOTE_REPLY, message_id="m1h"),
        bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("quote_visible_at_bot", d, DecisionAction.REPLY, Addressee.BOT)

    # 1i. text "@other" names must count as addressing someone else
    d = resolver.evaluate(
        _event("@P4 @Elaina 你们看这个", message_id="m1i"),
        bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("at_other_text_mentions", d, DecisionAction.DROP,
                 Addressee.PERSON)

    # 1j. [旺柴] arrives as text but behaves like a sticker
    d = resolver.evaluate(
        _event("[旺柴]", message_id="m1j", created_at=now),
        window=_window(asked=True, now=now), bot_ids=bot_ids,
        aliases=aliases, now=now,
    )
    ok &= d.signals.get("media") == 1.0
    ok &= d.action != DecisionAction.REPLY

    # 1l. laughter/banter must not become a strict follow-up reply
    d = resolver.evaluate(
        _event("难崩", message_id="m1l", created_at=now),
        window=_window(asked=True, now=now), bot_ids=bot_ids,
        aliases=aliases, now=now,
    )
    ok &= d.signals.get("banter") == 1.0
    ok &= d.action != DecisionAction.REPLY

    # 1m. acknowledgement words close/drop instead of replying
    d = resolver.evaluate(
        _event("哦哦", message_id="m1m", created_at=now),
        window=_window(asked=True, now=now), bot_ids=bot_ids,
        aliases=aliases, now=now,
    )
    ok &= d.action != DecisionAction.REPLY

    # 1m2. real-trace ack variants must not become strict follow-ups either.
    # Regression: group member replying "噢噢" to the bot's question was scored
    # as a continuation and answered with an unrelated topic.
    for ack_text in ("噢噢", "噢噢。", "噢", "哦", "喔喔", "嗯呐", "好哒", "okk"):
        d = resolver.evaluate(
            _event(ack_text, message_id=f"m1m2-{ack_text}", created_at=now),
            window=_window(asked=True, now=now), bot_ids=bot_ids,
            aliases=aliases, now=now,
        )
        good = d.action != DecisionAction.REPLY
        good &= d.dialogue_act == DialogueAct.ACK
        ok &= good
        print(f"  {'ok' if good else 'FAIL'} ack_variant:{ack_text} -> "
              f"{d.action.value}/{d.dialogue_act.value}")

    # 1n. short answer to the bot's question is still a valid reply
    d = resolver.evaluate(
        _event("1分钟", message_id="m1n", created_at=now),
        window=_window(asked=True, now=now), bot_ids=bot_ids,
        aliases=aliases, now=now,
    )
    ok &= _check("short_answer_to_bot", d, DecisionAction.REPLY,
                 Addressee.BOT)

    # 1k. bare punctuation is not a question
    d = resolver.evaluate(
        _event("？", message_id="m1k", created_at=now),
        window=_window(asked=True, now=now), bot_ids=bot_ids,
        aliases=aliases, now=now,
    )
    ok &= d.signals.get("expects_reply") == 0.0
    ok &= d.action != DecisionAction.REPLY

    # 2. reply to a known bot message
    d = resolver.evaluate(
        _event("是周五", reply_to="bot-1", message_id="m2"),
        bot_ids=bot_ids, aliases=aliases, bot_message_ids={"bot-1"}, now=now,
    )
    ok &= _check("reply_to_bot", d, DecisionAction.REPLY, Addressee.BOT)

    # 3. bot asked a question, same person answers
    d = resolver.evaluate(
        _event("周五下午三点前", message_id="m3", created_at=now),
        window=_window(asked=True, now=now), bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("answer_after_question", d, DecisionAction.REPLY, Addressee.BOT)

    # 4. same person follows up with a question
    d = resolver.evaluate(
        _event("那你觉得要带伞吗", message_id="m4", created_at=now),
        window=_window(asked=False, now=now), bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("followup_question", d, DecisionAction.REPLY, Addressee.BOT)

    # 5. soft acknowledgement stays in observe
    d = resolver.evaluate(
        _event("这个也可以", message_id="m5", created_at=now),
        window=_window(asked=False, now=now), bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("soft_ack", d, DecisionAction.OBSERVE, Addressee.BOT)

    # 6. closing phrase closes the window
    d = resolver.evaluate(
        _event("好的谢谢", message_id="m6", created_at=now),
        window=_window(asked=True, now=now), bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("closing", d, DecisionAction.CLOSE, Addressee.BOT)

    # 7. explicit mention of someone else
    d = resolver.evaluate(
        _event("@李四 你看下", mentions=(MentionRef(user_id="wxid_li"),),
               message_id="m7"),
        bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("at_other", d, DecisionAction.DROP, Addressee.PERSON)

    # 8. name of another member in the text
    d = resolver.evaluate(
        _event("李四你昨天那个方案怎么样了", message_id="m8", created_at=now),
        window=_window(asked=False, now=now), bot_ids=bot_ids,
        aliases=aliases, other_names=["李四"], now=now,
    )
    ok &= _check("other_name", d, DecisionAction.DROP, Addressee.PERSON)

    # 9. broadcast / forward
    d = resolver.evaluate(
        _event("[合并转发] 群聊记录", kind=MessageKind.FORWARD, message_id="m9"),
        bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= _check("broadcast", d, DecisionAction.DROP, Addressee.GROUP)

    # 10. topic shift inside a window
    d = resolver.evaluate(
        _event("对了，今晚谁去打球", message_id="m10", created_at=now),
        window=_window(asked=False, now=now), bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= d.action in (DecisionAction.DROP, DecisionAction.OBSERVE)

    # 11. media-only follow-up after a bot reply
    d = resolver.evaluate(
        _event("[表情包:捂脸笑]", kind=MessageKind.STICKER, message_id="m11",
               created_at=now),
        window=_window(asked=False, now=now), bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= d.action == DecisionAction.DROP

    # 12. no window, ordinary ambient message
    d = resolver.evaluate(
        _event("今天好热", message_id="m12"),
        bot_ids=bot_ids, aliases=aliases, now=now,
    )
    ok &= d.action == DecisionAction.DROP

    print("\n" + ("all passed" if ok else "FAILED"))
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
