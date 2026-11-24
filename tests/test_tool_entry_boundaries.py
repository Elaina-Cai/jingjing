# -*- coding: utf-8 -*-
"""工具入口/任务会话边界检查：引用剥离、否定排除、隐式任务会话 TTL。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.conversational import (  # noqa: E402
    TASK_SESSION_TTL_SECONDS,
    _tools_evidence,
    task_session_active,
)


def test_quote_block_is_not_evidence():
    assert _tools_evidence({}, "[引用 小K: 帮我查一下天气] 这波我赢了") is False
    assert _tools_evidence({}, "这波我赢了") is False


def test_negated_ask_is_not_evidence():
    assert _tools_evidence({}, "别帮我查了，我自己来") is False
    assert _tools_evidence({}, "不用搜了") is False


def test_positive_ask_and_task_session():
    assert _tools_evidence({}, "帮我查一下明天的天气") is True
    assert _tools_evidence({"task_session": True}, "随便聊聊") is True


class _Plan:
    def __init__(self, done: bool) -> None:
        self.all_done = done


class _Session:
    def __init__(self, *, done: bool, age_seconds: float) -> None:
        self.plan = _Plan(done)
        self.status = "active"
        self.updated_at = datetime.now() - timedelta(seconds=age_seconds)

    def is_active(self) -> bool:
        if self.status in {"completed", "failed"} or self.plan is None:
            return False
        return not bool(self.plan.all_done)


def test_task_session_ttl():
    fresh = _Session(done=False, age_seconds=60)
    stale = _Session(done=False, age_seconds=TASK_SESSION_TTL_SECONDS + 10)
    done = _Session(done=True, age_seconds=1)
    assert task_session_active(fresh) is True
    assert task_session_active(stale) is False
    assert task_session_active(done) is False
    assert task_session_active(None) is False


TESTS = [
    test_quote_block_is_not_evidence,
    test_negated_ask_is_not_evidence,
    test_positive_ask_and_task_session,
    test_task_session_ttl,
]


def main() -> bool:
    ok = True
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
            ok = False
        else:
            print(f"PASS {test.__name__}")
    if ok:
        print(f"tool-entry boundary checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
