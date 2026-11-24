# -*- coding: utf-8 -*-
r"""``EvidenceRef`` 通用 span（task-2 追加件；P1 情绪证据复用同一类型）。

钉死三件事：

1. **老数据能读**：没有 ``start`` / ``end`` 的 payload 读出来是"未知 span"，
   且重新序列化与旧形状逐字一致（键序、键数都一样）。
2. **新数据能读回 span**：写出去的偏移能原样读回，``quote_span`` /
   ``truncated`` / ``slice_from`` 都对。
3. **截断不丢定位**：``MessageEvent.evidence_ref`` 仍然只留 200 字引文，
   但 span 指向原文完整区间 —— 拿 ``message_id`` + ``[start:end]`` 就能回到原文。

Run directly::

    .\.venv_it\Scripts\python.exe tests\test_events_span.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.contracts.events import (  # noqa: E402
    ChatType,
    ConversationRef,
    EvidenceRef,
    MessageEvent,
)

LEGACY = {
    "source": "wechat",
    "message_id": "m1001",
    "account_id": "wxid_bot",
    "chat_id": "room@chatroom",
    "quoted_text": "我是湖南长沙人",
}


def _event(*, message_id: str, content: str, chat_id: str = "room@chatroom",
           chat_type: ChatType = ChatType.GROUP) -> MessageEvent:
    return MessageEvent(
        event_id=f"e-{message_id}",
        platform="wechat",
        account_id="wxid_bot",
        chat=ConversationRef(platform="wechat", account_id="wxid_bot",
                             chat_id=chat_id, chat_type=chat_type),
        message_id=message_id,
        sender_id="wxid_a",
        content=content,
    )


def test_legacy_payload_without_span_still_reads() -> bool:
    """老数据（无 start/end）能读，且序列化与旧形状逐字一致。"""
    print("\n[legacy payload / no span]")
    ref = EvidenceRef.from_dict(LEGACY)
    ok = ref.message_id == "m1001" and ref.quoted_text == "我是湖南长沙人"
    ok &= ref.start == -1 and ref.end == -1
    ok &= ref.has_span is False and ref.full_span is None
    ok &= ref.quote_span is None and ref.truncated is False
    ok &= ref.slice_from("别的原文") == "我是湖南长沙人"       # 无 span → 退回引文
    ok &= json.dumps(ref.to_dict(), ensure_ascii=False) == \
        json.dumps(LEGACY, ensure_ascii=False)
    print("  ok" if ok else "  FAIL", ref.to_dict())
    return bool(ok)


def test_new_payload_round_trips_the_span() -> bool:
    """新数据：span 写出去、读回来，quote_span / truncated 都对。"""
    print("\n[new payload / span round-trip]")
    ref = EvidenceRef(source="wechat", message_id="m2", quoted_text="abc",
                      start=3, end=10)
    payload = ref.to_dict()
    ok = payload["start"] == 3 and payload["end"] == 10 and len(payload) == 7
    back = EvidenceRef.from_dict(payload)
    ok &= back == ref and back.full_span == (3, 10)
    ok &= back.quote_span == (3, 6)
    ok &= back.truncated is True
    ok &= back.slice_from("xxxabcdefg") == "abcdefg"
    ok &= back.with_span(0, 3).full_span == (0, 3)
    ok &= EvidenceRef.from_dict(json.loads(json.dumps(payload))) == ref
    print("  ok" if ok else "  FAIL", payload)
    return bool(ok)


def test_truncated_quote_can_still_reach_the_original() -> bool:
    """200 字截断保留定位：message_id + span 能回到原文。"""
    print("\n[truncated quote keeps the address]")
    content = "我" + "很长的原话" * 100            # 501 字
    ref = _event(message_id="m3140", content=content).evidence_ref()
    ok = ref.quoted_text == content[:200] and len(ref.quoted_text) == 200
    ok &= ref.full_span == (0, len(content))
    ok &= ref.truncated is True
    ok &= ref.slice_from(content) == content       # 截断的引文仍能按 span 回到原文
    ok &= ref.message_id == "m3140" and ref.chat_id == "room@chatroom"
    ok &= EvidenceRef.from_dict(ref.to_dict()).full_span == (0, len(content))

    short = _event(message_id="m2", content="在吗", chat_type=ChatType.PRIVATE,
                   chat_id="c1").evidence_ref()
    ok &= short.truncated is False and short.full_span == (0, 2)
    ok &= short.slice_from("在吗") == "在吗"

    empty = _event(message_id="m3", content="", chat_type=ChatType.PRIVATE,
                   chat_id="c1").evidence_ref()
    ok &= empty.has_span is False and empty.start == -1
    ok &= "start" not in empty.to_dict()           # 空内容不该假装有 span
    print("  ok" if ok else "  FAIL", ref.quote_span, len(content))
    return bool(ok)


def test_garbage_span_never_pretends_to_be_addressable() -> bool:
    """坏 span（反向 / 非整数 / None）一律收敛成"未知"。"""
    print("\n[garbage span -> unknown]")
    ok = EvidenceRef(source="s", message_id="m", start=5, end=2).has_span is False
    ok &= EvidenceRef(source="s", message_id="m", start="x", end="y").full_span is None
    ok &= EvidenceRef.from_dict(
        {"source": "s", "message_id": "m", "start": None, "end": ""}).start == -1
    ok &= EvidenceRef(source="s", message_id="m", start=-3,
                      end=-3).to_dict().get("start") is None
    ok &= EvidenceRef(source="s", message_id="m", start=0, end=0).has_span is False
    print("  ok" if ok else "  FAIL")
    return bool(ok)


def main() -> bool:
    print("=" * 50)
    print("contracts / EvidenceRef span checks")
    print("=" * 50)
    ok = test_legacy_payload_without_span_still_reads()
    ok &= test_new_payload_round_trips_the_span()
    ok &= test_truncated_quote_can_still_reach_the_original()
    ok &= test_garbage_span_never_pretends_to_be_addressable()
    print("\n" + ("all passed" if ok else "FAILED"))
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
