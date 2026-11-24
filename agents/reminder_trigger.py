"""Scheduled reminder delivery.

A reminder is a promise the user explicitly asked for.  When it comes due the
scheduler calls :func:`run_reminder_turn`, which runs one synthetic turn in the
*source* conversation and sends the line through ``OutboundGateway`` — the
same transport layer every normal reply uses.

Differences from proactive outreach are deliberate and narrow:

* the whitelist / quiet-hours / cooldown pre-check is skipped: the user asked
  for this message, so it is not unprompted contact;
* the send is classified as ``trigger_kind="reminder"``, not ``proactive``,
  so the gateway's proactive rate limits do not swallow it;
* ``fallback_text`` guarantees delivery even when the model replies with
  silence: the deterministic line still goes out (她开口 or 兜底，二选一都必须到).
"""

from __future__ import annotations

from agents.proactive_trigger import ProactiveTurnResult, run_proactive_turn
from core.tasks.mapping import split_mention_names

REMINDER_PROMPT = (
    "[系统触发 · 定时提醒]\n"
    "你之前答应过对方：到点了要提醒 ta「{content}」。现在时间到了。\n"
    "请直接用你平时说话的方式把这条提醒说出来，只说这一件事："
    "不要道歉、不要解释系统、不要展开别的话题。\n"
    "即使你猜到对方可能在忙，也要把提醒说出来——这是 ta 让你记着的事。"
)


def build_reminder_prompt(content: str, mention_names=None) -> str:
    prompt = REMINDER_PROMPT.format(content=str(content or "").strip())
    names = [str(n).strip() for n in (mention_names or []) if str(n).strip()]
    if names:
        who = "、".join(f"@{n}" for n in names)
        prompt += (
            f"\n这条是群提醒：到点先 {who} 把人叫上，再说提醒内容，别漏掉 ta。"
        )
    return prompt


def parse_mention_names(raw) -> list:
    """解析 ``mention_names``（JSON 数组）或 44 版单值 ``mention_name`` → 名字列表。

    语义 owner = :func:`core.tasks.mapping.split_mention_names`（JSON 数组优先 →
    否则按 ``,，、;；``/空白 切 → 空则空列表；空项丢弃、不去重）。本函数只做两件事：
    转成 ``list``（scheduler 与既有断言按 list 比较）与保留旧名字。
    以前这里自带一份「非 JSON 就整串当一名」的实现，与迁移侧按分隔符切的实现
    给出不同名单——同一条提醒在两条路上 @ 的人不一样。
    """
    return list(split_mention_names(raw))


def reminder_fallback_text(content: str) -> str:
    return f"⏰ 提醒你：{str(content or '').strip()}"


async def run_reminder_turn(
    *,
    platform: str,
    bot_account_id: str = "",
    chat_id: str,
    chat_type: str = "private",
    content: str,
    mention_names=None,
) -> ProactiveTurnResult:
    """Run one reminder turn and return whether the line actually went out."""
    content = str(content or "").strip()
    names = [str(n).strip() for n in (mention_names or []) if str(n).strip()]
    if not content:
        return ProactiveTurnResult(False, reason="empty_content")
    reply_prefix = (
        " ".join(f"@{n}" for n in names)
        if chat_type == "group" and names else ""
    )
    return await run_proactive_turn(
        platform=platform,
        bot_account_id=bot_account_id,
        chat_id=chat_id,
        chat_type=chat_type,
        hint=build_reminder_prompt(content, names),
        source="reminder",
        trigger_kind="reminder",
        enforce_proactive_policy=False,
        fallback_text=reminder_fallback_text(content),
        reply_prefix=reply_prefix,
        supported_texts=(content,),
    )
