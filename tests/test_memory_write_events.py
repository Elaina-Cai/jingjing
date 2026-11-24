"""Phase 5 E1/E2: explicit remember writes canonical events.

Run directly:
    python tests/test_memory_write_events.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import _tempdirs  # noqa: E402,F401  临时目录纪律（见 tests/_tempdirs.py）：SQLite 临时文件走它

# ---------------------------------------------------------------------------
# 数据库隔离（照 tests/test_event_chain_links.py / test_command_service_db.py）：
# 本文件会经 core.bootstrap.init_singletons() 触发 init_meta_db + run_migrations
# （bootstrap 兜底、runtime 访问器等），不隔离就把 pending 迁移落到**真实**
# meta / account 库。2026-09-29 实测：Migration 51 被本套测试落到了生产库
# （data/yinyue_meta.db + 4 个账号库，applied_at 14:58:19Z）。必须在 import core.*
# 之前设好：PathSettings 在 core.config.settings **首次**导入时就取值。
# ---------------------------------------------------------------------------
_ISO_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "_test_memory_write_events_iso"
)
os.makedirs(_ISO_ROOT, exist_ok=True)
os.environ["META_DB_PATH"] = os.path.join(_ISO_ROOT, "meta.db")
os.environ["PLATFORMS_DIR"] = os.path.join(_ISO_ROOT, "platforms")
os.environ["DATA_DIR"] = os.path.join(_ISO_ROOT, "data")

from core.config.settings import config as _config  # noqa: E402

# 守卫：env 只在 core.config.settings 首次导入前设好才有效，否则真实路径已经
# 固化进 config。这里硬失败，绝不静默把迁移/写操作落到生产库。
assert os.path.realpath(_config.paths.meta_db_path) != os.path.realpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "yinyue_meta.db"
    )
), f"测试数据库隔离失效：meta_db_path={_config.paths.meta_db_path}"


ROOT = Path(__file__).resolve().parent.parent
_TEST_TMP_ROOT = ROOT / "data" / "_memory_write_events_test"
UTC = timezone.utc


def _make_temp_dir() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


from core.terminal.event_runtime import (  # noqa: E402
    install_terminal_event_store,
    reset_terminal_event_store,
)
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from core.terminal.memory_gateway import MemoryGateway  # noqa: E402
from core.terminal.principal import (  # noqa: E402
    ChatType,
    PrincipalContext,
    RelationLevel,
)
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.repo_memory_projections import MemoryProjectionRepository  # noqa: E402
from agents.tools.memory import RememberTool  # noqa: E402
from agents.tools.search import SearchVectorTool  # noqa: E402


def _cleanup(meta, db_path: Path) -> None:
    if meta is not None:
        try:
            meta.engine.dispose()
        except Exception:
            pass
    for suffix in ("", "-wal", "-shm"):
        try:
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
        except PermissionError:
            pass


def _owner_principal() -> PrincipalContext:
    return PrincipalContext(
        platform="wechat",
        bot_account_id="bot_a",
        chat_id="owner_raw",
        chat_type=ChatType.PRIVATE,
        person_id="p_owner",
        display_name="老板",
        relation_to_bot=RelationLevel.OWNER,
        capabilities=frozenset(),
        memory_scopes=frozenset(),
    )


def test_explicit_remember_writes_event_and_projection() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    original_get_meta = None
    try:
        meta = MetaRepositoryManager(str(db_path))
        store = TerminalEventStore(meta.engine)
        projection_repo = MemoryProjectionRepository(meta.engine)
        install_terminal_event_store(store)

        from core import bootstrap

        original_get_meta = bootstrap.get_meta_repo
        bootstrap.get_meta_repo = lambda: meta

        principal = _owner_principal()
        context = SimpleNamespace(principal=principal, extra={})
        tool = RememberTool(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
        tool.set_context(context)
        result = asyncio.run(
            tool.execute(
                content="我下周三去上海出差",
                memory_type="semantic",
                importance=0.9,
            )
        )
        assert not result.is_error
        assert "event_id=" in result.output

        events = store.all()
        assert len(events) == 1
        assert events[0].event_type.value == "memory_write"
        assert events[0].metadata["explicit_remember"] is True

        item = projection_repo.get(f"proj:{events[0].event_id}")
        assert item is not None
        assert "上海" in item.content
        assert item.source_event_ids == (events[0].event_id,)
        assert item.metadata["explicit_remember"] is True

        gateway = MemoryGateway(
            event_store=store,
            projection_repository=projection_repo,
        )
        hits = gateway.recall(principal, query="上海")
        assert hits
        assert hits[0].source_event_ids == (events[0].event_id,)
    finally:
        if original_get_meta is not None:
            from core import bootstrap

            bootstrap.get_meta_repo = original_get_meta
        reset_terminal_event_store()
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_search_vector_rejects_legacy_memories_collection() -> None:
    class _ExplodingEmbedding:
        async def embed(self, _text):
            raise AssertionError("legacy memories path must not embed")

    class _ExplodingStore:
        def search(self, *_args, **_kwargs):
            raise AssertionError("legacy memories path must not search")

    tool = SearchVectorTool(_ExplodingStore(), _ExplodingEmbedding())
    result = asyncio.run(
        tool.execute(query="以前的事", collection="memories")
    )
    assert result.is_error
    assert "recall_memories" in result.error


def _stranger_principal() -> PrincipalContext:
    return PrincipalContext(
        platform="wechat",
        bot_account_id="bot_a",
        chat_id="g1@bot_a",
        chat_type=ChatType.GROUP,
        person_id="p_stranger",
        display_name="路人",
        relation_to_bot=RelationLevel.STRANGER,
        capabilities=frozenset(),
        memory_scopes=frozenset(),
    )


def test_non_owner_remember_is_rejected() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "memory.db"
    original_get_meta = None
    try:
        meta = MetaRepositoryManager(str(db_path))
        store = TerminalEventStore(meta.engine)
        install_terminal_event_store(store)

        from core import bootstrap

        original_get_meta = bootstrap.get_meta_repo
        bootstrap.get_meta_repo = lambda: meta

        principal = _stranger_principal()
        context = SimpleNamespace(
            principal=principal,
            extra={
                "platform": principal.platform,
                "account_id": principal.bot_account_id,
                "chat_id": principal.chat_id,
                "chat_type": "group",
                "is_group": True,
                "group_id": principal.group_id or principal.chat_id,
            },
        )
        tool = RememberTool(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
        tool.set_context(context)
        result = asyncio.run(
            tool.execute(
                content="记住我是学委", memory_type="semantic", importance=0.9
            )
        )
        assert result.is_error
        assert "主人" in result.error
        assert not store.all()
    finally:
        if original_get_meta is not None:
            from core import bootstrap

            bootstrap.get_meta_repo = original_get_meta
        reset_terminal_event_store()
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    tests = [
        test_explicit_remember_writes_event_and_projection,
        test_non_owner_remember_is_rejected,
        test_search_vector_rejects_legacy_memories_collection,
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
