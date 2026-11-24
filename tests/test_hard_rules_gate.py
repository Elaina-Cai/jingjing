"""Phase 5 Layer-1 hard-rule gate: deterministic checks on a temp DB.

Run directly:
    python tests/test_hard_rules_gate.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _tempdirs import make_db_dir  # noqa: E402

from core.terminal.event_model import Event, EventType  # noqa: E402
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from core.terminal.memory_projection import MemoryProjector  # noqa: E402
from core.terminal.principal import VisibilityScope  # noqa: E402
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.migrations import run_migrations  # noqa: E402
from db.repo_memory_projections import MemoryProjectionRepository  # noqa: E402


def _load_gate():
    path = Path(__file__).resolve().parent.parent / "scripts" / "gate_hard_rules.py"
    spec = importlib.util.spec_from_file_location("gate_hard_rules", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _seed(tmp: Path):
    """Owner-only secret + a group message + whitelist/send rows."""
    db_path = tmp / "gate_meta.db"
    meta = MetaRepositoryManager(str(db_path))
    run_migrations(str(db_path))
    store = TerminalEventStore(meta.engine)
    repo = MemoryProjectionRepository(meta.engine)
    projector = MemoryProjector(store, repo)

    def add(event: Event) -> None:
        store.append(event)
        item = projector.project_event(event)
        if item is not None:
            repo.upsert(item)

    add(
        Event.new(
            event_type=EventType.MEMORY_WRITE,
            occurred_at=datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc),
            platform="wechat",
            bot_account_id="bot_a",
            chat_id="owner_private",
            chat_type="private",
            actor_person_id="p_owner",
            content="主人才知道的暗号：紫水晶",
            visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
            metadata={"memory_type": "semantic", "importance": 0.9, "source": "test"},
        )
    )
    add(
        Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=datetime(2026, 9, 21, 10, 5, tzinfo=timezone.utc),
            platform="wechat",
            bot_account_id="bot_a",
            chat_id="group_x@chatroom",
            chat_type="group",
            actor_person_id="p_member",
            content="群里说的话",
            visibility_scope=frozenset({VisibilityScope.GROUP_MEMBERS}),
            metadata={"group_id": "group_x@chatroom"},
        )
    )

    con = sqlite3.connect(str(db_path))
    con.execute(
        "insert into proactive_targets "
        "(platform, bot_account_id, chat_id, chat_type, enabled, allow_sticker, "
        " max_per_hour, max_per_day, cooldown_minutes, quiet_hours_json) "
        "values ('wechat','bot_a','chat_ok','private',1,0,3,5,180,'[]')"
    )
    for chat in ("chat_ok", "chat_bad"):
        con.execute(
            "insert into proactive_send_log "
            "(platform, bot_account_id, chat_id, sent_at, kind, trigger, "
            " content_preview, ok) "
            "values ('wechat','bot_a',?, datetime('now'),'text','proactive','hi',1)",
            (chat,),
        )
    con.commit()
    return meta, db_path, con, repo


def test_whitelist_gate() -> None:
    gate = _load_gate()
    tmp = make_db_dir("gate_")
    meta = None
    try:
        meta, _, con, _ = _seed(tmp)
        violations = gate.check_proactive_sends(con)
        assert len(violations) == 1, violations
        assert "chat_bad" in violations[0], violations
    finally:
        if meta is not None:
            try:
                meta.engine.dispose()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok   proactive send outside the whitelist is detected")


def test_provenance_gate() -> None:
    gate = _load_gate()
    tmp = make_db_dir("gate_")
    meta = None
    try:
        meta, _, con, repo = _seed(tmp)
        assert gate.check_projection_provenance(
            con, table="memory_projections", id_column="memory_id"
        ) == [], "seeded projections must be clean"

        # write a deliberately broken projection through the memory repo
        from core.terminal.memory_projection import MemoryProjectionItem
        from db.repo_memory_projections import MemoryProjectionRepository as _MR

        _MR(meta.engine).upsert(
            MemoryProjectionItem(
                memory_id="proj:broken",
                memory_type="fact",
                content="引用不存在的事件",
                source_event_ids=("evt_missing",),
            )
        )
        violations = gate.check_projection_provenance(
            con, table="memory_projections", id_column="memory_id"
        )
        assert len(violations) == 1 and "evt_missing" in violations[0], violations
    finally:
        if meta is not None:
            try:
                meta.engine.dispose()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok   projection citing a missing event is detected")


def test_vector_orphan_gate() -> None:
    gate = _load_gate()
    violations = gate.check_vector_orphans(
        {"proj:a", "proj:b", "proj:orphan"}, {"proj:a", "proj:b"}
    )
    assert len(violations) == 1 and "proj:orphan" in violations[0], violations
    assert gate.check_vector_orphans({"proj:a"}, {"proj:a", "proj:b"}) == []
    print("  ok   orphan vector ids are detected")


def test_non_owner_recall_gate() -> None:
    gate = _load_gate()
    tmp = make_db_dir("gate_")
    meta = None
    try:
        meta, _, _, _ = _seed(tmp)
        assert gate.check_non_owner_recall(meta) == [], "seeded owner secret leaked"

        # negative control: force a leaky recall and prove the checker sees it
        from core.terminal import memory_gateway as gw_mod
        from core.terminal.memory_gateway import MemoryHit

        leaked = MemoryHit(
            memory_id="proj:leak",
            content="主人才知道的暗号：紫水晶",
            occurred_at=datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc),
            event_type="fact",
            visibility_scope=("owner_only",),
            source_event_ids=("evt_x",),
        )
        original = gw_mod.MemoryGateway.recall
        gw_mod.MemoryGateway.recall = lambda self, principal, **kwargs: [leaked]
        try:
            violations = gate.check_non_owner_recall(meta)
        finally:
            gw_mod.MemoryGateway.recall = original
        assert len(violations) == 2, violations  # one per non-owner principal
        assert all("owner_only" in item for item in violations), violations
    finally:
        if meta is not None:
            try:
                meta.engine.dispose()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok   owner_only recall by a non-owner is detected")


def test_output_privacy_gate() -> None:
    gate = _load_gate()
    tmp = make_db_dir("gate_")
    meta = None
    try:
        meta, _, con, repo = _seed(tmp)
        store = TerminalEventStore(meta.engine)
        owner_only_marker = "主人才知道的暗号：紫水晶"
        owner_chat = "wxid_owner"

        def _reply(chat_id: str, content: str, index: int) -> None:
            store.append(
                Event.new(
                    event_type=EventType.REPLY,
                    occurred_at=datetime(2026, 9, 21, 11, index, tzinfo=timezone.utc),
                    platform="wechat",
                    bot_account_id="bot_a",
                    chat_id=chat_id,
                    chat_type="group" if "@chatroom" in chat_id else "private",
                    content=content,
                    visibility_scope=frozenset({VisibilityScope.PUBLIC}),
                )
            )

        # true positive: a group reply repeats the owner-only memory text
        _reply("group_x@chatroom", f"转述：{owner_only_marker}", 1)
        violations = gate.check_output_privacy(con, owner_chat_ids={owner_chat})
        assert len(violations) == 1, violations
        assert "group_x@chatroom" in violations[0], violations

        # the owner's own chat is excluded
        _reply(owner_chat, owner_only_marker, 2)
        violations = gate.check_output_privacy(con, owner_chat_ids={owner_chat})
        assert len(violations) == 1, violations

        # tool-name noise: a bare "send_sticker" memory must never flag,
        # even when a reply contains that word (2026-09-21 false positive)
        from core.terminal.memory_projection import MemoryProjectionItem

        repo.upsert(
            MemoryProjectionItem(
                memory_id="proj:toolname",
                memory_type="episode",
                content="send_sticker",
                visibility_scope=("owner_only",),
            )
        )
        _reply("group_x@chatroom", "she tried to send_sticker again", 3)
        violations = gate.check_output_privacy(con, owner_chat_ids={owner_chat})
        assert len(violations) == 1, violations
    finally:
        if meta is not None:
            try:
                meta.engine.dispose()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    print("  ok   output privacy gate (leak detected, owner chat excluded, tool names ignored)")


def main() -> bool:
    tests = [
        test_whitelist_gate,
        test_provenance_gate,
        test_vector_orphan_gate,
        test_non_owner_recall_gate,
        test_output_privacy_gate,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"hard-rule gate checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
