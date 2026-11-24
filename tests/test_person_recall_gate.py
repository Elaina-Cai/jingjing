"""人物观察线召回开关（PERSON_MEMORY × PERSON_RECALL）回归（2026-09-29 收口 A）。

契约：
- 写线关（PERSON_MEMORY=0）时，PersonMemoryProvider 不得召回存量旧观察；
- 影子提取（PERSON_MEMORY=1, PERSON_RECALL=0）只写不读；
- 只有两者都开才读；且开关判定必须发生在访问仓储之前（短路）。

Run directly: python tests/test_person_recall_gate.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.contracts.events import ChatType
from core.contracts.person_memory import (
    ObservationKind,
    ObservationVisibility,
    PersonObservation,
)
from core.memory.hub import MemoryHub
from core.memory.providers import PersonMemoryProvider
from db.meta_models import init_meta_db
from db.repo_person_memory import PersonMemoryRepository

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "_test_person_recall_gate.db"
PRINCIPAL = SimpleNamespace(person_id="qq_p", platform="qq", bot_account_id="bot_10001")
ENV_MEMORY = "YINYUE_FEATURE_PERSON_MEMORY"
ENV_RECALL = "YINYUE_FEATURE_PERSON_RECALL"


def _fresh_engine():
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    return init_meta_db(str(DB_PATH))


def _observation() -> PersonObservation:
    return PersonObservation(
        observation_id="obs_gate_1",
        person_id="qq_p",
        platform="qq",
        account_id="bot_10001",
        occurred_scope_type=ChatType.PRIVATE,
        occurred_scope_id="qq_p",
        speaker_person_id="qq_p",
        source_evidence_id="ev_1",
        source_message_id="msg_1",
        observed_at=datetime(2026, 9, 20, 12, 0),
        kind=ObservationKind.PREFERENCE,
        content="喜欢喝奶茶，第一次聊天就点了两杯",
        confidence=0.9,
        visibility=ObservationVisibility.PRIVATE_ONLY,
    )


def _flags(memory: str, recall: str):
    # 兄弟开关一并显式表态（守卫 tests/test_env_flag_isolation.py §③）：本文件只测
    # "写侧关/开 × 读侧关/开"这四格，抽取与聚合不该参与——钉在缺省位，不吃 .env。
    return patch.dict(os.environ, {
        ENV_MEMORY: memory,
        ENV_RECALL: recall,
        "YINYUE_FEATURE_PERSON_LLM": "0",
        "YINYUE_FEATURE_PERSON_PROFILE_AGGREGATE": "0",
    }, clear=False)


def _seeded_provider():
    engine = _fresh_engine()
    repo = PersonMemoryRepository(engine)
    repo.save_many([_observation()])
    return engine, PersonMemoryProvider(repository=repo)


def _close(engine) -> None:
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)


def test_gate_matrix() -> None:
    engine, provider = _seeded_provider()
    try:
        arms = [
            ("0", "0", 0),  # 生产现状：写线关 → 读必关
            ("1", "0", 0),  # 影子提取：只写不读
            ("0", "1", 0),  # 写关读开也必须关（防存量旧观察被召回）
            ("1", "1", 1),  # 全开才读
        ]
        for memory, recall, expected in arms:
            with _flags(memory, recall):
                items = provider.recall(PRINCIPAL, "奶茶", limit=5)
            assert len(items) == expected, (memory, recall, [i.content for i in items])
            if expected:
                assert items[0].kind == "person"
                assert items[0].metadata.get("source") == "person_observations"
                assert "奶茶" in items[0].content
    finally:
        _close(engine)


def test_gate_short_circuits_before_repository() -> None:
    class _RecordingRepo:
        def __init__(self) -> None:
            self.calls = 0

        def list_for_person(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("repo should only be reached when both flags are on")

    repo = _RecordingRepo()
    provider = PersonMemoryProvider(repository=repo)
    for memory, recall in (("0", "0"), ("1", "0"), ("0", "1")):
        with _flags(memory, recall):
            assert provider.recall(PRINCIPAL, "奶茶") == []
        assert repo.calls == 0, (memory, recall)
    with _flags("1", "1"):
        assert provider.recall(PRINCIPAL, "奶茶") == []  # 仓储异常被 provider 吞掉
    assert repo.calls == 1


def test_hub_path_respects_gate() -> None:
    engine, provider = _seeded_provider()
    try:
        hub = MemoryHub({"person": provider})
        with _flags("0", "0"):
            assert hub.recall(PRINCIPAL, "奶茶", kinds=["person"]) == []
        with _flags("1", "1"):
            items = hub.recall(PRINCIPAL, "奶茶", kinds=["person"])
        assert len(items) == 1 and items[0].kind == "person"
    finally:
        _close(engine)


def main() -> int:
    tests = [
        test_gate_matrix,
        test_gate_short_circuits_before_repository,
        test_hub_path_respects_gate,
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
