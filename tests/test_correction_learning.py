"""Correction learning: owner corrections become memory + invalidation.

Run directly:
    python tests/test_correction_learning.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _tempdirs import make_db_dir  # noqa: E402

from core.terminal.event_model import Event, EventType  # noqa: E402
from core.memory.explicit_write import projection_version
from core.terminal.event_runtime import (  # noqa: E402
    install_terminal_event_store,
    reset_terminal_event_store,
)
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from core.terminal.memory_gateway import MemoryGateway  # noqa: E402
from core.terminal.memory_projection import MemoryProjectionItem, MemoryProjector  # noqa: E402
from core.terminal.principal import (  # noqa: E402
    ChatType,
    PrincipalContext,
    RelationLevel,
    VisibilityScope,
)
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.migrations import run_migrations  # noqa: E402

UTC = timezone.utc


def _owner() -> PrincipalContext:
    return PrincipalContext(
        platform="wechat",
        bot_account_id="bot_a",
        chat_id="owner_private",
        chat_type=ChatType.PRIVATE,
        person_id="p_owner",
        display_name="主人",
        relation_to_bot=RelationLevel.OWNER,
    )


def _seed_projection(meta, *, memory_id="proj:old", content="她周五有空", scope=("owner_only",),
                     memory_type="fact", age_minutes=1440.0, chat_id="owner_private"):
    item = MemoryProjectionItem(
        memory_id=memory_id,
        memory_type=memory_type,
        content=content,
        platform="wechat",
        bot_account_id="bot_a",
        occurred_at=datetime.now() - timedelta(minutes=float(age_minutes)),
        confidence=0.7,
        visibility_scope=scope,
        source_event_ids=(f"evt_{memory_id}",),
        metadata=({"chat_id": chat_id} if chat_id else {}),
    )
    meta.memory_projections.upsert(item)
    return item


def _seed_sample(meta, *, person_id="p_owner", text="不对，不是周五，是周六", quote_id="m-quoted"):
    return meta.feedback_samples.add(
        source="correction",
        platform="wechat",
        bot_account_id="bot_a",
        chat_id="owner_private",
        chat_type="private",
        person_id=person_id,
        sample_text=text,
        previous_user_text="周五有空吗",
        reply_preview="嗯，你周五有空",
        metadata={"signals": ["correction"], "quote_reply_id": quote_id, "quote_content": "嗯，你周五有空"},
    )


def _temp_meta(tmp: Path):
    db_path = tmp / "correction_meta.db"
    run_migrations(str(db_path))
    return MetaRepositoryManager(str(db_path))


def test_invalidate_and_recall_filter() -> None:
    tmp = make_db_dir("corr_")
    meta = None
    try:
        meta = _temp_meta(tmp)
        item = _seed_projection(meta)
        gateway = MemoryGateway(
            event_store=TerminalEventStore(meta.engine),
            projection_repository=meta.memory_projections,
            vector_recaller=lambda q, limit: {},
        )
        hits = gateway.recall(_owner(), query="周五", limit=5)
        assert any(hit.memory_id == item.memory_id for hit in hits), hits

        closed = meta.memory_projections.invalidate([item.memory_id])
        assert closed == 1
        hits = gateway.recall(_owner(), query="周五", limit=5)
        assert all(hit.memory_id != item.memory_id for hit in hits), hits
    finally:
        if meta is not None:
            meta.engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok   invalidate() closes a projection and recall stops using it")


def test_worker_learns_and_supersedes() -> None:
    from core.quality import corrections as corr

    tmp = make_db_dir("corr_")
    meta = None
    store = None
    try:
        meta = _temp_meta(tmp)
        store = TerminalEventStore(meta.engine)
        install_terminal_event_store(store)
        old = _seed_projection(meta)
        sample = _seed_sample(meta)

        original_owner, original_collect, original_extract, original_lookup = (
            corr._owner_ids,
            corr._collect_candidates,
            corr._extract,
            corr._lookup_quoted,
        )
        corr._lookup_quoted = lambda *args, **kwargs: None
        corr._owner_ids = lambda platform: {"p_owner"}
        corr._collect_candidates = lambda **kwargs: [
            {
                "memory_id": old.memory_id,
                "content": old.content,
                "visibility": ["owner_only"],
                "source": "trace",
                "version": projection_version(old),
            }
        ]

        async def _fake_extract(llm, *, correction_text, quoted_text, candidates):
            return {
                "action": "upsert",
                "fact": "她周六有空",
                "target_memory_id": old.memory_id,
                "confidence": 0.9,
            }

        corr._extract = _fake_extract
        corr.reset_rate_for_tests()
        try:
            stats = asyncio.run(
                corr.process_pending_corrections(meta_repo=meta, llm=SimpleNamespace())
            )
        finally:
            (
                corr._owner_ids,
                corr._collect_candidates,
                corr._extract,
                corr._lookup_quoted,
            ) = (
                original_owner,
                original_collect,
                original_extract,
                original_lookup,
            )

        assert stats["learned"] == 1, stats
        # new fact projected with correction provenance + inherited visibility
        rows = [
            item
            for item in meta.memory_projections.all(limit=50)
            if item.memory_id != old.memory_id
        ]
        assert len(rows) == 1, rows
        new_item = rows[0]
        assert "周六" in new_item.content
        assert tuple(new_item.visibility_scope) == ("owner_only",)
        assert new_item.metadata.get("source") == "user_correction"
        assert new_item.source_event_ids, "correction must carry provenance"
        # old projection closed
        refreshed = meta.memory_projections.get(old.memory_id)
        assert refreshed is not None and refreshed.valid_to is not None
        # sample marked learned
        updated = meta.feedback_samples.list_recent(limit=5)[0]
        assert updated.status == "learned", updated.status
    finally:
        reset_terminal_event_store()
        if meta is not None:
            meta.engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok   worker learns the correction, supersedes the old memory")


def test_non_owner_and_low_confidence_change_nothing() -> None:
    from core.quality import corrections as corr

    tmp = make_db_dir("corr_")
    meta = None
    try:
        meta = _temp_meta(tmp)
        store = TerminalEventStore(meta.engine)
        install_terminal_event_store(store)
        old = _seed_projection(meta)
        _seed_sample(meta, person_id="stranger")  # not the owner

        original = (
            corr._owner_ids,
            corr._collect_candidates,
            corr._extract,
            corr._lookup_quoted,
        )
        corr._lookup_quoted = lambda *args, **kwargs: None
        corr._owner_ids = lambda platform: {"p_owner"}
        corr._collect_candidates = lambda **kwargs: []
        corr.reset_rate_for_tests()

        async def _low(llm, *, correction_text, quoted_text, candidates):
            return {"action": "upsert", "fact": "她周六有空", "confidence": 0.2}

        corr._extract = _low
        try:
            stats = asyncio.run(
                corr.process_pending_corrections(meta_repo=meta, llm=SimpleNamespace())
            )
        finally:
            (
                corr._owner_ids,
                corr._collect_candidates,
                corr._extract,
                corr._lookup_quoted,
            ) = original

        assert stats["skipped"] == 1, stats
        assert len(meta.memory_projections.all(limit=50)) == 1  # no new memory

        # now an owner sample with low confidence -> reviewed, still no write
        corr._owner_ids = lambda platform: {"p_owner"}  # part 1 restored the real one
        _seed_sample(meta, text="嗯……也许吧")
        original_extract = corr._extract
        corr._extract = _low
        corr.reset_rate_for_tests()
        try:
            stats = asyncio.run(
                corr.process_pending_corrections(meta_repo=meta, llm=SimpleNamespace())
            )
        finally:
            corr._extract = original_extract
        assert stats["reviewed"] == 1, stats
        assert len(meta.memory_projections.all(limit=50)) == 1
        assert meta.memory_projections.get(old.memory_id).valid_to is None
    finally:
        reset_terminal_event_store()
        if meta is not None:
            meta.engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok   non-owner and low-confidence samples change nothing")


def test_long_block_upsert_merges_instead_of_replacing() -> None:
    from core.quality import corrections as corr

    tmp = make_db_dir("corr_")
    meta = None
    try:
        meta = _temp_meta(tmp)
        store = TerminalEventStore(meta.engine)
        install_terminal_event_store(store)
        long_text = (
            "示例作业群成员清单（截至2026-09-22）："
            "P5，计算机25-4班，前学委（期末卸任），女；"
            "Elaina（主人），计算机25-4班，普通学生，男；"
            "P4，计算机25-3班，心理委员，男；"
            "P7，计算机25-3班，前学委（期末卸任），女；"
            "P3，计算机25-1班，学委，男；P6，物联网专业，女。"
        )
        old = _seed_projection(
            meta, memory_id="proj:big", content=long_text, scope=("group_members",)
        )
        _seed_sample(meta, text="补一下，P6是物联网25-2班的")
        original = (
            corr._owner_ids,
            corr._collect_candidates,
            corr._extract,
            corr._lookup_quoted,
        )
        corr._lookup_quoted = lambda *args, **kwargs: None
        corr._owner_ids = lambda platform: {"p_owner"}
        corr._collect_candidates = lambda **kwargs: [
            {
                "memory_id": old.memory_id,
                "content": old.content[:300],
                "full_content": old.content,
                "visibility": ["group_members"],
                "source": "trace",
                "version": projection_version(old),
            }
        ]

        async def _fake_extract(llm, *, correction_text, quoted_text, candidates):
            return {
                "action": "upsert",
                "fact": "P6是物联网25-2班的",
                "target_memory_id": old.memory_id,
                "confidence": 0.9,
            }

        corr._extract = _fake_extract
        corr.reset_rate_for_tests()
        try:
            stats = asyncio.run(
                corr.process_pending_corrections(meta_repo=meta, llm=SimpleNamespace())
            )
        finally:
            (
                corr._owner_ids,
                corr._collect_candidates,
                corr._extract,
                corr._lookup_quoted,
            ) = original

        assert stats["learned"] == 1, stats
        rows = [
            item
            for item in meta.memory_projections.all(limit=50)
            if item.memory_id != old.memory_id
        ]
        assert len(rows) == 1, rows
        merged = rows[0]
        assert "P5" in merged.content and "P3" in merged.content
        assert "[更正补充]" in merged.content and "物联网25-2班" in merged.content
        assert merged.metadata.get("partial_update") is True
        assert meta.memory_projections.get(old.memory_id).valid_to is not None
    finally:
        reset_terminal_event_store()
        if meta is not None:
            meta.engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok   long-block correction merges instead of replacing")


def test_recent_same_chat_episode_is_not_recall_material() -> None:
    """2026-10-10（②）：同会话近窗的 **episode** 不回流成记忆素材。

    实测事故：22:21 私聊「讲个笑话听一下」的召回 top3 = 21:55–21:57 同会话的
    「讲个笑话／我让你讲个笑话／你这个笑话谁能听懂？」——这些已由 [最近对话] 窗口承载，
    再当"记忆素材"喂回去，只会让这一轮围着刚发生的事打转（自指元玩笑）。
    口径三重限定：同会话 + 近窗 + episode；跨会话 / 更早 / fact 一律不动。
    """
    tmp = make_db_dir("corr_")
    meta = None
    try:
        meta = _temp_meta(tmp)
        echo = _seed_projection(
            meta, memory_id="proj:echo", content="讲个笑话",
            memory_type="episode", age_minutes=5.0, chat_id="owner_private",
        )
        other = _seed_projection(
            meta, memory_id="proj:other", content="讲个笑话",
            memory_type="episode", age_minutes=5.0, chat_id="other_chat",
        )
        old_fact = _seed_projection(meta, memory_id="proj:old")
        gateway = MemoryGateway(
            event_store=TerminalEventStore(meta.engine),
            projection_repository=meta.memory_projections,
            vector_recaller=lambda q, limit: {},
        )
        hits = gateway.recall(_owner(), query="讲个笑话听一下", limit=5)
        ids = {hit.memory_id for hit in hits}
        assert echo.memory_id not in ids, hits
        assert other.memory_id in ids, hits          # 跨会话的近窗 episode 不排除
        hits2 = gateway.recall(_owner(), query="周五", limit=5)
        assert any(hit.memory_id == old_fact.memory_id for hit in hits2), hits2
    finally:
        if meta is not None:
            meta.engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok   同会话近窗 episode 不召回；跨会话/更早 fact 照旧")


def main() -> bool:
    tests = [
        test_long_block_upsert_merges_instead_of_replacing,
        test_invalidate_and_recall_filter,
        test_recent_same_chat_episode_is_not_recall_material,
        test_worker_learns_and_supersedes,
        test_non_owner_and_low_confidence_change_nothing,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"correction learning checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
