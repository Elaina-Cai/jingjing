#!/usr/bin/env python
"""WP-05: ReplyMode x 路由段 -> TurnDecision mapping matrix."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.routing.turn_decision import (  # noqa: E402
    DEFAULT_CANCEL_IF,
    DEFAULT_DEFER_SECONDS,
    ROUTE_SEGMENT_AMBIENT,
    ROUTE_SEGMENT_DEFERRED,
    ROUTE_SEGMENT_MENTION,
    ROUTE_SEGMENT_PAT,
    TurnDecision,
    from_engagement,
    from_route,
    from_trigger,
)
from core.speech.domain import EngagementDecision, ReplyMode  # noqa: E402


def _eng(mode: ReplyMode, reason: str = "", defer: float = 0.0) -> EngagementDecision:
    return EngagementDecision(reply_mode=mode, reply_reason=reason, defer_seconds=defer)


def _check(name: str, got: TurnDecision, *, action: str, reason: str = "",
           defer: float | None = None, cancel_if: tuple[str, ...] | None = None) -> bool:
    ok = got.action == action
    if reason:
        ok = ok and got.reason == reason
    if defer is not None:
        ok = ok and abs(got.defer_seconds - defer) < 1e-9
    if cancel_if is not None:
        ok = ok and got.cancel_if == cancel_if
    if not got.reason_chain:
        ok = False
    if not ok:
        print(f"FAIL {name}: {got}")
    return ok


def main() -> bool:
    ok = True
    # ---- ReplyMode ----
    ok &= _check("always", from_engagement(_eng(ReplyMode.ALWAYS, "web_chat")),
                 action="reply_now", reason="web_chat")
    ok &= _check("urgent", from_engagement(_eng(ReplyMode.URGENT, "non_owner_private_urgent")),
                 action="reply_now", reason="non_owner_private_urgent")
    ok &= _check("mention_hit", from_engagement(_eng(ReplyMode.MENTION), is_mentioned=True),
                 action="reply_now")
    ok &= _check("mention_miss", from_engagement(_eng(ReplyMode.MENTION)),
                 action="silent", reason="mention_not_addressed")
    ok &= _check("mention_pat", from_engagement(_eng(ReplyMode.MENTION), is_group=True, pat_on_self=True),
                 action="reply_now")
    ok &= _check("defer", from_engagement(_eng(ReplyMode.DEFER, "group_deferred_to_state", 15.0)),
                 action="defer", defer=15.0, cancel_if=DEFAULT_CANCEL_IF)
    ok &= _check("defer_default", from_engagement(_eng(ReplyMode.DEFER)),
                 action="defer", defer=DEFAULT_DEFER_SECONDS)
    ok &= _check("never", from_engagement(_eng(ReplyMode.NEVER, "ignored_group")),
                 action="silent", reason="ignored_group")
    ok &= _check("unknown_mode", from_engagement(_eng("weird")), action="silent")
    # ---- 路由段 ----
    ok &= _check("route_none", from_route("", reason="stale_or_empty"), action="silent", reason="stale_or_empty")
    ok &= _check("route_mention", from_route(ROUTE_SEGMENT_MENTION), action="reply_now")
    ok &= _check("route_pat", from_route(ROUTE_SEGMENT_PAT), action="reply_now")
    ok &= _check("route_pat_deferred", from_route(ROUTE_SEGMENT_PAT, pat_deferred=True),
                 action="defer")
    ok &= _check("route_deferred", from_route(ROUTE_SEGMENT_DEFERRED),
                 action="defer", defer=DEFAULT_DEFER_SECONDS, cancel_if=DEFAULT_CANCEL_IF)
    ok &= _check("route_ambient", from_route(ROUTE_SEGMENT_AMBIENT), action="reply_now")
    ok &= _check("trigger_unknown", from_trigger("weird"), action="silent")
    ok &= _check("route_unknown", from_route("weird_route"), action="silent")
    # relevance 合并
    class _Rel:
        action = "skip"
        reason = "position_low_signal"
        chain = ("position:skip/low_signal",)

    ok &= _check("relevance_skip", from_route(ROUTE_SEGMENT_AMBIENT, relevance=_Rel()),
                 action="silent", reason="position_low_signal")

    class _Take(_Rel):
        action = "take"
        reason = "strong_address"

    ok &= _check("relevance_take", from_route(ROUTE_SEGMENT_AMBIENT, relevance=_Take()),
                 action="reply_now", reason="strong_address")
    # 地址字段透传
    got = from_engagement(
        _eng(ReplyMode.ALWAYS, "owner_private_message"),
        source="private", reply_chat_id="wxid_x", reply_platform="wechat",
        reply_account_id="bot_wxid", sender_id="wxid_x", is_group=False,
    )
    if not (got.should_reply and got.reply_chat_id == "wxid_x"
            and got.reply_account_id == "bot_wxid" and got.source == "private"
            and not got.is_group):
        print(f"FAIL address passthrough: {got}")
        ok = False
    if not (from_route(ROUTE_SEGMENT_DEFERRED).should_defer
            and from_route("").is_silent):
        print("FAIL property helpers")
        ok = False
    if ok:
        print("turn decision mapping: ok")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
