#!/usr/bin/env python
"""Self-contained Phase 5 B/C/D offline acceptance.

This script is intentionally independent from the live meta DB.  It creates a
temporary SQLite database, seeds canonical terminal events, runs the analysis
and memory projection workers, checks the gateway permission matrix, and
verifies that the Track B/C/D files/flags are gone.

Run:
    python scripts/phase5_offline_acceptance.py

**注（P0 收尾，2026-10-01）——本脚本部分断言基于已退役的表，仅供参考**：

``no_legacy_writes`` 这一项原本直接查 ``memories`` / ``conversations`` 两张表。
**迁移 49**（``db/migrations.py:1588 retire legacy contact_profiles and memories
tables``）已 ``DROP TABLE memories``，``conversations`` 也早已不在 schema 里，
于是这两条查询在新库上必然抛 ``sqlite3.OperationalError: no such table``——
**是脚本过期，不是被测代码坏了**（``docs/全链路架构审计与目标架构.md``
``§4 G3-8`` 记的"两套迁移 + 过期读方"就是这一类）。

最小改法（**不重写脚本**）：表在就照旧断言"没写进 legacy 表"，表不在就
**跳过该条并标注 legacy**，跳过项一律登记进 ``legacy_skipped`` 随结果返回，
**绝不静默当成通过**（静默跳过 = 一条永远绿的假断言，正是本仓踩过的坑）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.analysis.workers import run_analysis_worker_once, run_daily_digest_once  # noqa: E402
from core.memory.recall_router import recall_for_context  # noqa: E402
from core.terminal.event_model import Event, EventType, Sensitivity  # noqa: E402
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from core.terminal.identity_service import IdentityService  # noqa: E402
from core.terminal.memory_gateway import (  # noqa: E402
    MemoryHit,
    install_memory_gateway,
    reset_memory_gateway,
)
from core.terminal.memory_projection import MemoryProjector  # noqa: E402
from core.terminal.principal import (  # noqa: E402
    ChatType,
    GroupMembershipPeriod,
    RelationLevel,
    RelationshipState,
    VisibilityScope,
)
from core.terminal.proactive_decision import ProactiveDecisionWorker  # noqa: E402
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.repo_memory_projections import MemoryProjectionRepository  # noqa: E402
from agents.tools.memory import RecallMemoriesTool  # noqa: E402

UTC = timezone.utc
W = "wechat"


class _CheckFailure(RuntimeError):
    pass


def _check(condition, message: str) -> None:
    if not condition:
        raise _CheckFailure(message)


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    """表在不在（过期读方的跳过判据；不用 try/except 吞异常）。"""
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _deleted_old_chain() -> None:
    old_files = [
        "core/preprocess/classifier.py",
        "core/routing/orchestrator.py",
        "agents/private_chat.py",
        "agents/group_chat.py",
        "agents/daily_digest.py",
    ]
    missing = [path for path in old_files if (ROOT / path).exists()]
    _check(not missing, f"old analysis files still exist: {missing}")


def _identity() -> IdentityService:
    when = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    return IdentityService(
        relationships={
            (W, "bot_a", "p_owner"): RelationshipState(
                W, "bot_a", "p_owner", RelationLevel.OWNER, 1.0, "seed"
            ),
            (W, "bot_a", "p_friend"): RelationshipState(
                W, "bot_a", "p_friend", RelationLevel.ORDINARY, 0.4, "seed"
            ),
        },
        person_identities={
            (W, "owner_raw"): "p_owner",
            (W, "friend_raw"): "p_friend",
            (W, "member_raw"): "p_member",
        },
        group_memberships={
            (W, "g1@bot_a", "p_member"): [
                GroupMembershipPeriod(start_at=when - timedelta(days=1))
            ],
        },
    )


def _seed(store: TerminalEventStore):
    when = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    friend = Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=when,
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type="private",
        actor_person_id="p_friend",
        actor_account_id="friend_raw",
        content="明天上海会不会下雨",
        visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
        metadata={"sender_name": "朋友", "msg_id": "m_friend"},
    )
    group = Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=when + timedelta(minutes=1),
        platform=W,
        bot_account_id="bot_a",
        chat_id="g1",
        chat_type="group",
        actor_person_id="p_member",
        actor_account_id="member_raw",
        content="周末聚会安排一下",
        visibility_scope=frozenset({VisibilityScope.GROUP_MEMBERS}),
        metadata={"sender_name": "群友", "msg_id": "m_group"},
    )
    reply = Event.new(
        event_type=EventType.REPLY,
        occurred_at=when + timedelta(minutes=2),
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type="private",
        actor_person_id="p_bot",
        actor_account_id="bot_a",
        content="我帮你看一下天气",
        visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
        metadata={"sender_name": "鲸鲸"},
    )
    thought = Event.new(
        event_type=EventType.THOUGHT,
        occurred_at=when + timedelta(minutes=3),
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type="private",
        actor_person_id="p_owner",
        content="原始思维链不能进入分析或召回",
        sensitivity=Sensitivity.OWNER_CHAIN_OF_THOUGHT,
        visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
        metadata={"msg_id": "thought"},
    )
    events = [friend, group, reply, thought]
    for event in events:
        store.append(event)
    return friend, group, reply, thought


def _principal(service: IdentityService, raw_id: str, *, group: bool = False):
    return service.resolve_principal(
        platform=W,
        bot_account_id="bot_a",
        chat_id="g1" if group else raw_id,
        chat_type="group" if group else "private",
        sender_id=raw_id,
        sender_name="群友" if group else "朋友",
        occurred_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
    )


def _check_gateway_permissions(store, identity: IdentityService) -> None:
    from core.terminal.memory_gateway import MemoryGateway

    gateway = MemoryGateway(event_store=store, identity_service=identity)
    owner = _principal(identity, "owner_raw")
    friend = _principal(identity, "friend_raw")
    member = _principal(identity, "member_raw", group=True)
    stranger = _principal(identity, "stranger_raw")

    owner_hits = gateway.recall(owner, query="天气")
    friend_hits = gateway.recall(friend)
    member_hits = gateway.recall(member)
    stranger_hits = gateway.recall(stranger)

    _check(any("天气" in hit.content for hit in owner_hits), "owner lost private recall")
    _check(
        all("聚会" not in hit.content for hit in friend_hits),
        "ordinary contact leaked group/private scope",
    )
    _check(
        [hit.content for hit in member_hits] == ["周末聚会安排一下"],
        f"group member recall unexpected: {[hit.content for hit in member_hits]}",
    )
    _check(stranger_hits == [], "unknown identity received recall")
    _check(
        all("思维链" not in hit.content for hit in owner_hits + friend_hits + member_hits),
        "owner chain-of-thought leaked into recall",
    )


def _check_gateway_reader_tool(owner_principal) -> None:
    class _FakeGateway:
        def recall(self, principal, *, query="", limit=6, **_kwargs):
            return [
                MemoryHit(
                    memory_id="event:offline",
                    content="事件流里的长期记忆",
                    occurred_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
                    event_type="message",
                    source_event_ids=("evt_offline",),
                    score=0.9,
                )
            ]

    class _ExplodingVectorStore:
        def search(self, *_args, **_kwargs):
            raise AssertionError("legacy memories vector search must not run")

    fake = _FakeGateway()
    install_memory_gateway(fake)
    try:
        tool = RecallMemoriesTool(SimpleNamespace(), _ExplodingVectorStore(), SimpleNamespace())
        tool.set_context(SimpleNamespace(principal=owner_principal, extra={}))
        result = asyncio.run(tool.execute(query="还记得什么", top_k=3))
        _check("事件流里的长期记忆" in result.output, "RecallMemoriesTool did not use gateway")
        _check(
            result.data and result.data[0]["source_event_ids"] == ["evt_offline"],
            "RecallMemoriesTool lost source_event_ids",
        )
        _check(recall_for_context(tool.ctx, "x"), "recall_for_context returned no hits")
    finally:
        reset_memory_gateway()


def _run_checks(tmp: Path) -> dict:
    checks: list[str] = []
    #: 因表已退役而**跳过**的过期断言（随结果返回，绝不静默当通过）。
    legacy_skipped: list[str] = []
    _deleted_old_chain()
    checks.append("deleted_old_chain")

    meta = MetaRepositoryManager(str(tmp / "offline.db"))
    try:
        store = TerminalEventStore(meta.engine)
        all_events, group_event, reply_event, thought = _seed(store)

        first = run_analysis_worker_once(store, meta.analysis_projections, limit=20)
        _check(first["failed"] == 0, f"analysis worker failed: {first}")
        _check(first["written"] == 2, f"analysis worker projections: {first}")
        _check(first["cursor"] == 4, f"analysis cursor did not pass all events: {first}")
        counts = meta.analysis_projections.count_by_type()
        _check(counts.get("message_analysis") == 1, f"message analysis count: {counts}")
        _check(counts.get("group_summary") == 1, f"group summary count: {counts}")
        for draft in meta.analysis_projections.all():
            _check(draft.source_event_ids, "projection without source_event_ids")
            _check(thought.event_id not in draft.source_event_ids, "thought leaked into analysis")
        checks.append("analysis_worker")

        second = run_analysis_worker_once(store, meta.analysis_projections, limit=20)
        _check(second["scanned"] == 0, f"analysis worker not idempotent: {second}")
        _check(meta.analysis_projections.count() == 2, "analysis projection duplicated")

        digest_result = run_daily_digest_once(
            store, meta.analysis_projections, target_date=datetime(2026, 9, 20, tzinfo=UTC).date()
        )
        _check(digest_result["written"] == 1, f"daily digest not written: {digest_result}")
        digest = meta.analysis_projections.get("analysis:daily_digest:2026-09-20")
        _check(digest is not None, "daily digest projection missing")
        _check(group_event.event_id in digest.source_event_ids, "digest lost group source")
        _check(reply_event.event_id in digest.source_event_ids, "digest lost reply source")
        _check("思维链" not in digest.content, "thought leaked into digest")
        checks.append("daily_digest")

        projection_repo = MemoryProjectionRepository(meta.engine)
        projected = MemoryProjector(store, projection_repo).project_pending(limit=20)
        _check(projected["projected"] > 0, f"memory projection produced nothing: {projected}")
        _check(projection_repo.count() > 0, "memory_projections is empty")
        checks.append("memory_projection")

        identity = _identity()
        _check_gateway_permissions(store, identity)
        checks.append("gateway_permissions")
        owner = _principal(identity, "owner_raw")
        _check_gateway_reader_tool(owner)
        checks.append("gateway_reader_tool")

        con = sqlite3.connect(str(tmp / "offline.db"))
        try:
            # 迁移 49 已 DROP memories；conversations 也已退役 → 表不在就跳过并登记
            # （legacy），**不静默当通过**。表在时语义与改前逐字一致。
            for table, message in (
                ("memories", "analysis worker wrote legacy memories"),
                ("conversations", "analysis worker wrote legacy conversations"),
            ):
                if not _table_exists(con, table):
                    legacy_skipped.append(table)
                    continue
                count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                _check(count == 0, message)
        finally:
            con.close()
        checks.append("no_legacy_writes")
    finally:
        try:
            meta.engine.dispose()
        except Exception:
            pass

    return {"pass": True, "checks": checks, "legacy_skipped": legacy_skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    tmp_root = ROOT / "data" / "_phase5_offline_acceptance"
    tmp = tmp_root / f"case_{uuid.uuid4().hex}"
    tmp.mkdir(parents=True, exist_ok=False)
    try:
        result = _run_checks(tmp)
    except Exception as exc:  # noqa: BLE001
        result = {
            "pass": False,
            "error": str(exc)[:500],
            "temp": str(tmp),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    finally:
        if not args.keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)
    result["temp"] = str(tmp) if args.keep_temp else ""
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
