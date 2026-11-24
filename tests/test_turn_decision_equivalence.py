#!/usr/bin/env python
"""WP-05 part 3b: 路由段 x TurnDecision.action 等价矩阵（旧触发分类已退役）。

读 docs/当断则断_执行手册.md §5.1 矩阵逐行断言；不再对账两套分类器（只剩一个纯函数）。

Run directly:
    python tests/test_turn_decision_equivalence.py
WP-05 隔离/全量跑法：
    python scripts/run_all_tests.py --filter test_turn_decision
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 兄弟开关整组钉死（tests/test_env_flag_isolation.py §③）：本文件只让
# GroupState.classify_trigger 的 action/路由段说话；PAT_GRACE / SEMANTIC_UNIT /
# GROUP_COALESCE 都会改写“纯拍/群触发”的走向，必须显式钉死。
os.environ["YINYUE_FEATURE_PAT_GRACE"] = "off"
os.environ["YINYUE_FEATURE_SEMANTIC_UNIT"] = "off"
os.environ["YINYUE_FEATURE_GROUP_COALESCE"] = "off"

from legacy_brain.group_state import (  # noqa: E402
    BufferedMessage,
    GroupState,
    pat_grace_seconds,
)
from core.routing.turn_decision import (  # noqa: E402
    DEFAULT_DEFER_SECONDS,
    ROUTE_SEGMENT_AMBIENT,
    ROUTE_SEGMENT_DEFERRED,
    ROUTE_SEGMENT_MENTION,
    ROUTE_SEGMENT_PAT,
    route_segment,
)


class _env:
    def __init__(self, **values: str | None) -> None:
        self._values = values

    def __enter__(self):
        self._saved = {key: os.environ.get(key) for key in self._values}
        for key, value in self._values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc) -> bool:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _msg(content: str, **kw) -> BufferedMessage:
    created = kw.pop("created_at", None) or datetime.now()
    return BufferedMessage(
        sender_id="wxid_x", sender_name="P5", content=content,
        created_at=created, **kw,
    )


def _plain_cases():
    """(名字, 消息, 期望 action, 期望路由段)——读 §5.1 矩阵。"""
    stale_at = datetime.now() - timedelta(
        seconds=float(getattr(GroupState, "STALE_MESSAGE_SECONDS", 21600)) + 60
    )
    return (
        ("mention", _msg("在吗", mention_self=True), "reply_now", ROUTE_SEGMENT_MENTION),
        ("owner", _msg("今天吃啥", is_owner=True), "reply_now", ROUTE_SEGMENT_AMBIENT),
        ("addressed_other", _msg("@别人 你看", addressed_other=True), "reply_now", ROUTE_SEGMENT_AMBIENT),
        ("question", _msg("这个报表怎么做？"), "reply_now", ROUTE_SEGMENT_AMBIENT),  # 2026-10-23：提问启发式退役
        ("empty_media", _msg("", media_kind="sticker"), "silent", ""),
        ("chatter", _msg("今天天气不错"), "reply_now", ROUTE_SEGMENT_AMBIENT),
        ("stale", _msg("醒醒", created_at=stale_at), "silent", ""),
    )


def _check_case(name: str, msg: BufferedMessage, action: str, route: str) -> None:
    state = GroupState(chat_id="room@chatroom")
    before = (len(state.pending_questions), len(state.recent))
    decision = state.classify_trigger(msg)
    after = (len(state.pending_questions), len(state.recent))
    assert after == before, f"{name}: classify_trigger 写了状态 {before}->{after}"
    assert decision.action == action, (
        f"{name}: action={decision.action} expected={action} ({decision.reason})"
    )
    assert route_segment(decision) == route, (
        f"{name}: route={route_segment(decision)!r} expected={route!r}"
    )
    assert decision.reason_chain, f"{name}: 空 reason_chain"
    if action == "defer" and route == ROUTE_SEGMENT_DEFERRED:
        assert abs(decision.defer_seconds - DEFAULT_DEFER_SECONDS) < 1e-9, decision
        assert decision.cancel_if, decision


def _check_pat() -> None:
    pat = _msg("", pat_on_self=True, is_pure_pat=True)
    with _env(YINYUE_FEATURE_PAT_GRACE="on"):
        state = GroupState(chat_id="room@chatroom")
        new = state.classify_trigger(pat)
        assert new.action == "defer", new
        assert route_segment(new) == ROUTE_SEGMENT_PAT, new
        assert new.cancel_if, new
        assert abs(new.defer_seconds - pat_grace_seconds()) < 1e-9, new

        replied = GroupState(chat_id="room@chatroom")
        replied.last_reply_time = datetime.now()
        replied.last_reply_sender = pat.sender_id
        new2 = replied.classify_trigger(pat)
        assert new2.action == "silent", new2
        assert route_segment(new2) == "", new2

    with _env(YINYUE_FEATURE_PAT_GRACE="off"):
        state2 = GroupState(chat_id="room@chatroom")
        new3 = state2.classify_trigger(pat)
        assert new3.action == "silent", new3
        assert route_segment(new3) == "", new3


def main() -> bool:
    ok = True
    try:
        for name, msg, action, route in _plain_cases():
            _check_case(name, msg, action, route)
        _check_pat()
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        ok = False
    if ok:
        print("turn decision equivalence: ok")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
