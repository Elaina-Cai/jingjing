# -*- coding: utf-8 -*-
"""批D Step 38 巩固环检查：standalone + pytest 双兼容。"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context.situation import WorldModel  # noqa: E402
from core.quality import consolidation as cons  # noqa: E402
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from db.meta_models import MemoryProjectionRow, TerminalEvent  # noqa: E402
from db.repo_memory_projections import MemoryProjectionRepository  # noqa: E402

NOW = datetime(2026, 10, 14, 12, 0, 0)
ENV = "YINYUE_FEATURE_CONSOLIDATION"


def _model():
    model = WorldModel()
    model.scope = {"chat_id": "c1", "platform": "wechat", "account_id": "acct", "chat_type": "private"}
    model.add("fact_asserted", {"key": "主人不吃香菜", "value": "主人不吃香菜"}, at="2026-10-14 11:00")
    return model


class _Repo:
    def __init__(self):
        self.items = {}

    def get(self, memory_id):
        return self.items.get(memory_id)

    def upsert(self, item):
        self.items[item.memory_id] = item


class _NoRepo:
    def get(self, memory_id):
        raise AssertionError("shadow must not read repo")

    def upsert(self, item):
        raise AssertionError("shadow must not write")


def test_mode_default_off():
    old = os.environ.get(ENV)
    try:
        os.environ.pop(ENV, None)
        assert cons.consolidation_mode() == "off"
        os.environ[ENV] = "shadow"
        assert cons.consolidation_mode() == "shadow"
        os.environ[ENV] = "on"
        assert cons.consolidation_mode() == "on"
    finally:
        if old is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old


def test_off_zero_read():
    class _Boom:
        def items(self):
            raise AssertionError("off must not read models")

    report = cons.run_consolidation(models={"c1": _Boom()}, mode="off")
    assert report["mode"] == "off" and report["candidates"] == 0


def test_collect_filters_and_caps():
    models = {"c1": _model(), "c2": WorldModel()}
    items = cons.collect_candidates(models, now=NOW, min_score=0.0, max_items=5)
    assert len(items) == 1
    assert items[0].kind == "fact_asserted" and items[0].chat_id == "c1"
    assert items[0].label == "主人不吃香菜"
    assert cons.collect_candidates(models, now=NOW, min_score=0.99) == ()


def test_memory_id_stable_and_unique():
    a = cons.memory_id("c1", "fact_asserted", "主人不吃香菜")
    b = cons.memory_id("c1", "fact_asserted", "主人不吃香菜")
    c = cons.memory_id("c1", "fact_asserted", "别的")
    assert a == b and a != c
    assert a.startswith("cons-") and len(a) == 21


def test_shadow_only_audits_no_repo():
    report = cons.run_consolidation(
        models={"c1": _model()}, now=NOW, repo=_NoRepo(), mode="shadow"
    )
    assert report["mode"] == "shadow" and report["candidates"] == 1 and report["written"] == 0


def _make_db(tmpdir):
    engine = create_engine("sqlite:///" + str(Path(tmpdir) / "consolidation.db"))
    TerminalEvent.__table__.create(engine)
    MemoryProjectionRow.__table__.create(engine)
    return engine, MemoryProjectionRepository(engine), TerminalEventStore(engine)


def _counts(engine):
    with engine.connect() as conn:
        return tuple(
            conn.execute(text("SELECT COUNT(*) FROM " + table)).scalar()
            for table in ("terminal_events", "memory_projections")
        )


def test_on_writes_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine, repo, store = _make_db(tmpdir)
        try:
            first = cons.run_consolidation(
                models={"c1": _model()}, now=NOW, repo=repo, store=store, mode="on"
            )
            candidate = cons.collect_candidates({"c1": _model()}, now=NOW)[0]
            item = repo.get(candidate.memory_id)
            assert first["written"] == 1 and item is not None
            assert item.memory_id.startswith("cons-") and item.memory_type == "episode"
            assert item.content.startswith("[事实]") and "主人不吃香菜" in item.content
            assert item.source_event_ids and item.metadata.get("kind") == "fact_asserted"
            assert _counts(engine) == (1, 1)
            second = cons.run_consolidation(
                models={"c1": _model()}, now=NOW, repo=repo, store=store, mode="on"
            )
            assert second["written"] == 0 and _counts(engine) == (1, 1)
        finally:
            engine.dispose()


def test_source_ids_and_visibility_come_from_world_event():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine, repo, store = _make_db(tmpdir)
        try:
            group = WorldModel()
            group.scope = {"chat_id": "g1", "chat_type": "group", "platform": "wechat", "account_id": "acct"}
            group.add(
                "fact_asserted",
                {"key": "群事实", "value": "群事实", "evidence_refs": ["evt-real-1"]},
                at="2026-10-14 11:00",
            )
            report = cons.run_consolidation(
                models={"g1": group}, now=NOW, repo=repo, store=store, mode="on"
            )
            group_candidate = cons.collect_candidates({"g1": group}, now=NOW)[0]
            item = repo.get(group_candidate.memory_id)
            assert report["written"] == 1 and item is not None
            assert item.source_event_ids == ("evt-real-1",)
            assert item.visibility_scope == ("group_members",)
            assert item.platform == "wechat" and item.bot_account_id == "acct"

            private = WorldModel()
            private.scope = {"chat_id": "p1", "chat_type": "private", "platform": "wechat", "account_id": "acct"}
            private.add("fact_asserted", {"key": "私事", "value": "私事"}, at="2026-10-14 11:00")
            cons.run_consolidation(
                models={"p1": private}, now=NOW, repo=repo, store=store, mode="on"
            )
            private_candidate = cons.collect_candidates({"p1": private}, now=NOW)[0]
            private_item = repo.get(private_candidate.memory_id)
            assert private_item is not None
            assert private_item.visibility_scope == ("owner_only",)
            assert private_item.source_event_ids[0].startswith("world:")
        finally:
            engine.dispose()


def test_projection_failure_rolls_back_event_and_projection():
    with tempfile.TemporaryDirectory() as tmpdir:
        engine, repo, store = _make_db(tmpdir)
        try:
            original = repo.upsert_in_session

            def boom(session, item):
                raise RuntimeError("boom")

            repo.upsert_in_session = boom
            report = cons.run_consolidation(
                models={"c1": _model()}, now=NOW, repo=repo, store=store, mode="on"
            )
            assert report["written"] == 0 and _counts(engine) == (0, 0)
            repo.upsert_in_session = original
        finally:
            engine.dispose()


def test_low_score_not_consolidated():
    model = WorldModel()
    model.add("fact_asserted", {"key": "旧事"}, at="2026-09-01 11:00")
    assert cons.collect_candidates({"c1": model}, now=NOW, min_score=0.35) == ()


def test_static_wiring_and_flag():
    from core import features

    assert features.CONSOLIDATION == "consolidation"
    assert features.CONSOLIDATION in features.FLAG_REGISTRY
    src = (ROOT / "scheduler/main.py").read_text(encoding="utf-8")
    assert "run_consolidation" in src and "consolidation_failed" in src
    mod = (ROOT / "core/quality/consolidation.py").read_text(encoding="utf-8")
    assert "data_channels" in mod and "known_worlds" in mod
    assert "core.bootstrap" not in mod


TESTS = [
    test_mode_default_off,
    test_off_zero_read,
    test_collect_filters_and_caps,
    test_memory_id_stable_and_unique,
    test_shadow_only_audits_no_repo,
    test_on_writes_and_is_idempotent,
    test_source_ids_and_visibility_come_from_world_event,
    test_projection_failure_rolls_back_event_and_projection,
    test_low_score_not_consolidated,
    test_static_wiring_and_flag,
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
        print(f"consolidation checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
