"""Phase 5 E3-E9: MemoryHub provider/contract tests.

Run directly:
    python tests/test_memory_hub.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.memory.contracts import MemoryItem  # noqa: E402
from core.memory.hub import MemoryHub  # noqa: E402

UTC = timezone.utc


class _Provider:
    def __init__(self, kind: str, items: list[MemoryItem]):
        self.kind = kind
        self._items = items
        self.calls = 0

    def recall(self, principal, query, *, limit=8, context=None):
        self.calls += 1
        return list(self._items)[: max(1, int(limit))]


def _item(kind: str, memory_id: str, content: str, importance: float = 0.5):
    return MemoryItem(
        memory_id=memory_id,
        kind=kind,
        content=content,
        occurred_at=datetime(2026, 9, 21, 10, 0, tzinfo=UTC),
        importance=importance,
    )


def _principal():
    return SimpleNamespace(
        person_id="p1",
        is_owner=True,
        is_group=False,
        platform="wechat",
        bot_account_id="bot-a",
        chat_id="same",
    )


def test_hub_dispatches_only_requested_kinds() -> None:
    personal = _Provider("personal", [_item("personal", "p1", "个人记忆")])
    history = _Provider("history", [_item("history", "h1", "群历史")])
    hub = MemoryHub({"personal": personal, "history": history})

    result = hub.recall(_principal(), "记忆", kinds=["personal"], limit=5)
    assert [item.content for item in result] == ["个人记忆"]
    assert personal.calls == 1
    assert history.calls == 0


def test_hub_dedupes_and_ranks() -> None:
    low = _item("personal", "dup", "低", importance=0.1)
    high = _item("personal", "dup", "高", importance=0.9)
    other = _item("history", "h1", "历史", importance=0.7)
    hub = MemoryHub(
        {
            "personal": _Provider("personal", [low, high]),
            "history": _Provider("history", [other]),
        }
    )
    result = hub.recall(_principal(), "x", limit=5)
    assert [item.memory_id for item in result] == ["dup", "h1"]
    assert result[0].content == "高"


def test_hub_returns_empty_without_principal() -> None:
    hub = MemoryHub(
        {"personal": _Provider("personal", [_item("personal", "p1", "x")])}
    )
    assert hub.recall(None, "x") == []


def test_hub_filters_dead_evidence_and_keeps_unknown() -> None:
    from core.memory import belief_commit

    original = belief_commit.evidence_state
    dead = MemoryItem(
        memory_id="dead",
        kind="personal",
        content="已撤回内容",
        source_event_ids=("dead-event",),
    )
    unknown = MemoryItem(
        memory_id="unknown",
        kind="personal",
        content="无法核验内容",
        source_event_ids=("missing-event",),
    )
    provider = _Provider("personal", [dead, unknown])
    try:
        belief_commit.evidence_state = (  # type: ignore[assignment]
            lambda ref, scope: "dead" if ref == "dead-event" else "unknown"
        )
        hub = MemoryHub({"personal": provider}, verify_evidence=True)
        result = hub.recall(_principal(), "内容", kinds=["personal"], limit=5)
        assert [item.memory_id for item in result] == ["unknown"]
        assert result[0].metadata["evidence_state"] == "unknown"
    finally:
        belief_commit.evidence_state = original  # type: ignore[assignment]


def main() -> int:
    tests = [
        test_hub_dispatches_only_requested_kinds,
        test_hub_dedupes_and_ranks,
        test_hub_returns_empty_without_principal,
        test_hub_filters_dead_evidence_and_keeps_unknown,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL {test.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
