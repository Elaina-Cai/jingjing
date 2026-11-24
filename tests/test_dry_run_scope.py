# -*- coding: utf-8 -*-
"""dry-run 作用域（YINYUE_DRY_RUN_SEND_ALLOW_CHATS）检查：standalone + pytest 兼容。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402

FLAG = "YINYUE_FEATURE_DRY_RUN_SEND"
ALLOW = "YINYUE_DRY_RUN_SEND_ALLOW_CHATS"


def test_allow_list_bypasses_dry_run():
    old = {k: os.environ.get(k) for k in (FLAG, ALLOW)}
    try:
        os.environ[FLAG] = "1"
        os.environ[ALLOW] = "10000000001@chatroom, other"
        assert features.dry_run_send("10000000001@chatroom") is False
        assert features.dry_run_send("10000000003@chatroom") is True
        assert features.dry_run_send("") is True
        os.environ.pop(ALLOW, None)
        assert features.dry_run_send("10000000001@chatroom") is True
        os.environ[FLAG] = "0"
        assert features.dry_run_send("10000000001@chatroom") is False
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_static_wiring():
    import inspect

    from core.speech.outbound import _dry_run_send_enabled

    params = list(inspect.signature(_dry_run_send_enabled).parameters)
    assert params[:1] == ["chat_id"], f"演练闸必须按会话判定，签名首参不是 chat_id：{params}"
    wx = (ROOT / "core/execution/wx_sender.py").read_text(encoding="utf-8")
    assert 'dry_run_send(str(who or ""))' in wx
    qq = (ROOT / "core/execution/qq_provider.py").read_text(encoding="utf-8")
    assert 'dry_run_send(str(chat_id or ""))' in qq
    feat = (ROOT / "core/features.py").read_text(encoding="utf-8")
    assert "YINYUE_DRY_RUN_SEND_ALLOW_CHATS" in feat


TESTS = [test_allow_list_bypasses_dry_run, test_static_wiring]


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
        print(f"dry-run scope checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
