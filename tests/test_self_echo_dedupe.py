# -*- coding: utf-8 -*-
"""自我回声双写根治：写入侧合并 + 读侧去重（standalone + pytest 兼容）。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.terminal import shadow as shadow_mod  # noqa: E402

BODY = "@P5 意思就是你那句起来嗨，我顺着接了。这句话足够长。"


class _FakeStore:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.appended = []
        self.linked = []

    def get_by_external_msg_id(self, **kwargs):
        return None

    def query(self, **kwargs):
        return list(self.replies)

    def append(self, event):
        self.appended.append(event)
        return event

    def link_external_msg_id(self, event_id, *, external_msg_id):
        self.linked.append((event_id, external_msg_id))
        return True


def _reply(content: str = BODY, at: str = "2026-10-14 21:25:09", event_id: str = "ev-reply"):
    return SimpleNamespace(
        content=content,
        occurred_at=datetime.fromisoformat(at),
        event_id=event_id,
        event_type="reply",
    )


def _row(**over):
    row = {
        "chat_id": "10000000001@chatroom",
        "chat_type": "group",
        "sender_id": "wxid_bot",
        "sender_name": "鲸鲸",
        "msg_id": "1936698435886440749",
        "is_self": 1,
        "source": "wechat",
        "content": BODY,
        "created_at": "2026-10-14 21:25:10",
    }
    row.update(over)
    return row


def test_self_echo_merged_into_reply():
    store = _FakeStore([_reply()])
    result = shadow_mod.record_message_events(
        [_row()], platform="wechat", account_id="wxid_bot", store=store, return_events=True,
    )
    assert store.appended == []
    assert store.linked == [("ev-reply", "1936698435886440749")]
    assert result and result[0].event_id == "ev-reply"


def test_self_echo_without_match_appends():
    store = _FakeStore([_reply("完全不同的一句话，也足够长的一段内容")])
    shadow_mod.record_message_events(
        [_row()], platform="wechat", account_id="wxid_bot", store=store,
    )
    assert len(store.appended) == 1
    assert store.linked == []


def test_short_or_old_echo_not_merged():
    short = _FakeStore([_reply("收到")])
    shadow_mod.record_message_events(
        [_row(content="收到")], platform="wechat", account_id="wxid_bot", store=short,
    )
    assert len(short.appended) == 1
    old = _FakeStore([_reply(at="2026-10-14 20:00:00")])
    shadow_mod.record_message_events(
        [_row()], platform="wechat", account_id="wxid_bot", store=old,
    )
    assert len(old.appended) == 1


def test_non_self_message_not_merged():
    store = _FakeStore([_reply()])
    shadow_mod.record_message_events(
        [_row(is_self=0, sender_id="wxid_pan", sender_name="P5", msg_id="m-x")],
        platform="wechat", account_id="wxid_bot", store=store,
    )
    assert len(store.appended) == 1


def test_echo_with_mention_prefix_merged():
    """同步回来的行带 ``@对方`` 前缀，仍要并回发送侧那条 reply。"""
    store = _FakeStore([_reply()])
    shadow_mod.record_message_events(
        [_row(content="@P5\u2005" + BODY)],
        platform="wechat", account_id="wxid_bot", store=store, return_events=True,
    )
    assert store.appended == []
    assert store.linked == [("ev-reply", "1936698435886440749")]


def test_echo_of_truncated_reply_merged():
    """发送侧写入时被截断（尾部对不上），前缀一致也要合并。"""
    long_body = BODY + "后面还有很多字，" * 8
    store = _FakeStore([_reply(content=long_body[:210])])
    shadow_mod.record_message_events(
        [_row(content=long_body)],
        platform="wechat", account_id="wxid_bot", store=store, return_events=True,
    )
    assert store.appended == []
    assert store.linked == [("ev-reply", "1936698435886440749")]


def test_different_mention_but_same_tail_merged():
    """@ 的目标不同（她 @ 别人、同步回来 @ 她）不影响比对。"""
    store = _FakeStore([_reply(content=BODY.replace("@P5 ", ""))])
    shadow_mod.record_message_events(
        [_row(content="@鲸鲸\u2005" + BODY.replace("@P5 ", ""))],
        platform="wechat", account_id="wxid_bot", store=store, return_events=True,
    )
    assert store.appended == []
    assert store.linked == [("ev-reply", "1936698435886440749")]


def test_static_wiring():
    window = (ROOT / "core/context/chat_window.py").read_text(encoding="utf-8")
    # 2026-10-17：`messages_from_rows` 多了第三个参数（寻址边 hints，task-43/44），
    # 静态接线只钉"窗口读消息时确实过自回声去重"，不强绑参数个数。
    assert "_dedupe_self_echoes(messages_from_rows(rows, names" in window
    store_src = (ROOT / "core/terminal/event_store_db.py").read_text(encoding="utf-8")
    assert "def link_external_msg_id" in store_src
    assert (ROOT / "scripts/report_self_echo_dupes.py").exists()


TESTS = [
    test_self_echo_merged_into_reply,
    test_self_echo_without_match_appends,
    test_short_or_old_echo_not_merged,
    test_non_self_message_not_merged,
    test_echo_with_mention_prefix_merged,
    test_echo_of_truncated_reply_merged,
    test_different_mention_but_same_tail_merged,
    test_static_wiring,
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
        print(f"self-echo checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
