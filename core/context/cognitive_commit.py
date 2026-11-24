# -*- coding: utf-8 -*-
"""统一认知提交：认知状态的唯一写入口（P1 第一片）。

为什么要有这个模块
==================
现在信念/意图/承诺的写入散在 ``situation`` 的文件快照、ledger 的 JSONL 和各处
``save_world`` 里。它们各自能写、各自能改，于是「她为什么这么以为」没有唯一答案，
重试一次就可能多记一笔。本模块把这类写入收成**一个事务入口**：

    commit_updates(conn, context=..., proposals=[...],
                   expected_revisions={...}, idempotency_key="...")

一次调用完成：校验来源与权限 → 检查幂等与实体版本 → 追加事件 → 更新投影
→ 推进提交游标。任一条提案不合法，**整批不提交**（不做「能写几条写几条」）。

四条硬约束
==========
1. **模型不能扩大 context**：提案自带的 scope 只能与 ``CommitContext.scope`` 相等；
   证据必须落在 ``context.visible_event_ids`` 里（空集合即不可引用）。
2. **重试不重复落账**：``(scope, idempotency_key, proposal_id)`` 唯一；同键同内容返回
   ``duplicates``，同键不同内容返回 ``idempotency_conflict`` 且不写。
3. **并发不覆盖**：`expected_revisions` 与当前版本不一致时整批拒绝并回报最新版本，
   调用方拿最新状态重判。
4. **可重建**：``cognitive_state_scoped`` 只是缓存；``rebuild()`` 从事件重放得到同一结果，
   ``state_from_events()`` 是纯函数。

不做什么
========
* 不存模型私有思维过程：只存结构化 payload、证据引用与简短理由。
* 不把外部发送放进这个事务：外发仍走 outbound 队列，失败由队列凭据恢复。
* 不在这里判定语义真假：语法/引用合法只证明来源存在，不确定的解释按假说保存。

表建在 ``world.db``（与入站/队列/回执同库），由 :func:`ensure_schema` 幂等建表；
本模块不依赖 Alembic 迁移链，部署时由调用方在启动/首次使用处调用一次。
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from core.infra import skips
from core.infra.envflags import text as _env_text

__all__ = [
    "CommitContext",
    "CommitResult",
    "Proposal",
    "Scope",
    "closed_subjects_for_chat",
    "closed_subjects_for_scope",
    "commit_updates",
    "connect",
    "ensure_schema",
    "event_id_for",
    "read_cursor",
    "read_state",
    "rebuild",
    "state_from_events",
]

#: 只允许这些写入种类。它们描述「状态怎么变」，不枚举话题/情绪/任务类型。
OPERATIONS: frozenset[str] = frozenset(
    {"observe", "believe", "revise", "intend", "close", "appraise", "reflect", "retract"}
)

#: 这些操作声明的是「我相信/我打算/我完成」，没有证据就不该落账。
_EVIDENCE_REQUIRED: frozenset[str] = frozenset({"believe", "revise", "close", "retract"})

#: 读侧派生缓存：``(库路径, chat_id) -> (读取时刻, 已关闭 subject)``。
#: 关闭很少发生，读却每次冷加载都问一次；1 秒 TTL + 提交时清空足够。
_CLOSED_CACHE: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}

_SCOPE_SQL = "json_array(json_extract(scope,'$.platform'),json_extract(scope,'$.account_id'),json_extract(scope,'$.chat_id'))"
_APPRAISAL_TRIGGER_SQL = "json_extract(evidence_refs,'$[0]')"
_APPRAISAL_GOAL_SQL = "json_extract(payload,'$.goal_refs[0]')"
_APPRAISAL_TOPIC_NAME_SQL = "json_extract(payload,'$.topic_name')"
_APPRAISAL_TOPIC_PREFIX_LEN = 4
_APPRAISAL_TOPIC_PREFIX_SQL = f"substr({_APPRAISAL_TOPIC_NAME_SQL},1,{_APPRAISAL_TOPIC_PREFIX_LEN})"
_TOPIC_WHERE_SQL = "operation='appraise' AND subject_key GLOB 'appraisal:topic:*'"

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS cognitive_events (
        seq            INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id       TEXT    NOT NULL UNIQUE,
        idempotency_key TEXT   NOT NULL,
        proposal_id    TEXT    NOT NULL,
        subject_key    TEXT    NOT NULL,
        operation      TEXT    NOT NULL,
        payload        TEXT    NOT NULL,
        evidence_refs  TEXT    NOT NULL,
        scope          TEXT    NOT NULL,
        actor_key      TEXT    NOT NULL DEFAULT '',
        source         TEXT    NOT NULL DEFAULT '',
        supersedes     TEXT    NOT NULL DEFAULT '',
        revision       INTEGER NOT NULL,
        occurred_at    TEXT    NOT NULL DEFAULT '',
        committed_at   TEXT    NOT NULL,
        UNIQUE (idempotency_key, proposal_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cognitive_state (
        subject_key TEXT PRIMARY KEY,
        revision    INTEGER NOT NULL,
        event_id    TEXT    NOT NULL,
        operation   TEXT    NOT NULL,
        payload     TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cognitive_cursors (
        consumer TEXT PRIMARY KEY,
        seq      INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_cognitive_events_subject ON cognitive_events(subject_key, seq)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_appraisal_topic_trigger ON cognitive_events("
    + _SCOPE_SQL + "," + _APPRAISAL_TRIGGER_SQL + ") WHERE " + _TOPIC_WHERE_SQL,
)


def _stamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Scope:
    """会话坐标。写进事件是为了「这条状态属于哪个会话」永远可回答。"""

    platform: str
    account_id: str
    chat_id: str
    chat_type: str = "private"
    group_id: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.platform, self.account_id, self.chat_id)

    @property
    def complete(self) -> bool:
        return bool(self.platform and self.account_id and self.chat_id)

    def to_dict(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "group_id": self.group_id,
        }


@dataclass(frozen=True)
class Proposal:
    """一条待提交的状态变更。字段名是契约，不随调用点改。"""

    proposal_id: str
    subject_key: str
    operation: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    #: 来源唯一键（平台消息 id / 工具调用 id）。为空时用 proposal_id，仍可稳定重试。
    occurrence: str = ""
    supersedes: str = ""
    #: 只允许「与 context 相同」；填别的值会被拒绝，模型不能自己指定范围。
    scope: Scope | None = None
    occurred_at: str = ""

    def resolved_occurrence(self) -> str:
        return str(self.occurrence or self.proposal_id)


@dataclass(frozen=True)
class CommitContext:
    """程序提供的上下文。模型拿不到这个对象，也就改不了它。"""

    scope: Scope
    #: 本回合可见的事件 id；证据必须是它的子集，空集合不是无限制。
    visible_event_ids: frozenset[str] = frozenset()
    #: 本回合允许写的 subject；给了就越界即拒。
    allowed_subjects: frozenset[str] = frozenset()
    require_evidence: bool = True
    source: str = "model"
    actor_key: str = ""


@dataclass(frozen=True)
class CommitResult:
    committed: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    revisions: Mapping[str, int] = field(default_factory=dict)
    #: ``(proposal_id, reason)``；非空表示整批都没写。
    rejected: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.rejected


# --------------------------------------------------------------------------- schema


def ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等建表。重复调用不改变已有数据。"""
    for statement in _SCHEMA:
        conn.execute(statement)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognitive_state_scoped'").fetchone()
        conn.execute("CREATE TABLE IF NOT EXISTS cognitive_state_scoped(scope_key TEXT NOT NULL,subject_key TEXT NOT NULL,revision INTEGER NOT NULL,event_id TEXT NOT NULL,operation TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(scope_key,subject_key))")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_appraisal_topic_goal ON cognitive_state_scoped(scope_key,"
                     + _APPRAISAL_GOAL_SQL + ") WHERE " + _TOPIC_WHERE_SQL)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_appraisal_topic_name ON cognitive_state_scoped(scope_key,"
                     + _APPRAISAL_TOPIC_PREFIX_SQL + ") WHERE " + _TOPIC_WHERE_SQL)
        if not exists:
            for row in conn.execute("SELECT DISTINCT subject_key,scope FROM cognitive_events").fetchall():
                scope = json.loads(row["scope"])
                if _scope_identity(scope) is not None:
                    _write_state(conn, row["subject_key"], scope)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _production_world_db() -> str:
    """仓库里的生产 world.db 绝对路径（不读配置，防止 monkeypatch 绕过）。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.realpath(os.path.join(root, "data", "world.db"))


def _blocked_under_tests(path: str) -> bool:
    """测试进程里禁止碰仓库生产库：读写都不行。

    2026-11-07 实测事故：既有测试没设 ``YINYUE_COGNITIVE_DB``，执行侧关闭
    直接把两条 close 写进了 ``data/world.db``。生产库一旦有测试行，读侧派生
    可能把真实目标误判成已完成——比测试失败严重得多。
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and os.path.realpath(
        str(path)
    ) == _production_world_db()


def connect(path: str | None = None) -> sqlite3.Connection:
    """打开认知库连接；默认 ``YINYUE_COGNITIVE_DB``，否则落在 ``world.db``。

    连接用 autocommit 语义（``isolation_level=None``），事务边界由本模块显式控制。
    """
    if path is None:
        path = _env_text("YINYUE_COGNITIVE_DB").strip() or None
    if path is None:
        from db import world_db  # 延迟导入：只读路径不该被这里拖起来

        path = str(world_db.PATH)
    if _blocked_under_tests(path):
        raise RuntimeError(
            "拒绝在测试里写生产认知库：请把 YINYUE_COGNITIVE_DB 指到临时路径"
        )
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # 不在这里切 WAL：2026-11-07 实测，本机建库后切 WAL，同一库上的**第二个**
    # 连接会在 BEGIN IMMEDIATE 报 "attempt to write a readonly database"。
    # 并发正确性由 BEGIN IMMEDIATE 保证，不依赖 journal 模式；要开 WAL 由部署侧
    # 在库级决定，别让一个连接顺手改掉整个库的模式。
    ensure_schema(conn)
    return conn


# --------------------------------------------------------------------------- ids


def event_id_for(scope: Scope, occurrence: str, subject_key: str, operation: str) -> str:
    """稳定事件 id：同一来源 + 同一主体 + 同一操作，重拉/重启后仍然相同。

    刻意不用随机数：随机 id 会让「重试」看起来像「新经历」，正是 P0 要消灭的东西。
    """
    material = "\x1f".join(
        (
            scope.platform,
            scope.account_id,
            scope.chat_id,
            str(occurrence or ""),
            str(subject_key or ""),
            str(operation or ""),
        )
    )
    return "cog:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- validation


def _scope_matches(given: Scope | None, expected: Scope) -> bool:
    return given is None or given == expected


def _validate(
    context: CommitContext,
    proposals: Sequence[Proposal],
    idempotency_key: str,
) -> list[tuple[str, str]]:
    """纯规则校验（不碰库）。返回空列表表示可以进入事务。"""
    rejected: list[tuple[str, str]] = []
    if not str(idempotency_key or "").strip():
        return [("", "idempotency_key_required")]
    if not context.scope.complete:
        return [("", "scope_incomplete")]
    if not proposals:
        return [("", "empty_batch")]
    seen: set[str] = set()
    for proposal in proposals:
        pid = str(proposal.proposal_id or "")
        if not pid:
            rejected.append(("", "proposal_id_required"))
            continue
        if pid in seen:
            rejected.append((pid, "duplicate_proposal_id"))
            continue
        seen.add(pid)
        if not str(proposal.subject_key or ""):
            rejected.append((pid, "subject_key_required"))
            continue
        if proposal.operation not in OPERATIONS:
            rejected.append((pid, "unknown_operation:%s" % proposal.operation))
            continue
        if not _scope_matches(proposal.scope, context.scope):
            rejected.append((pid, "scope_mismatch"))
            continue
        if context.allowed_subjects and proposal.subject_key not in context.allowed_subjects:
            rejected.append((pid, "subject_not_visible"))
            continue
        refs = tuple(str(ref) for ref in proposal.evidence_refs if str(ref))
        if context.require_evidence and proposal.operation in _EVIDENCE_REQUIRED and not refs:
            rejected.append((pid, "evidence_required"))
            continue
        if any(
            ref not in context.visible_event_ids for ref in refs
        ):
            rejected.append((pid, "evidence_not_visible"))
            continue
        if proposal.supersedes and proposal.supersedes == event_id_for(
            context.scope,
            proposal.resolved_occurrence(),
            proposal.subject_key,
            proposal.operation,
        ):
            rejected.append((pid, "supersedes_self"))
    return rejected


# --------------------------------------------------------------------------- projection


def _scope_key(scope: Any) -> str:
    identity = _scope_identity(scope)
    if identity is None:
        raise ValueError("scope_incomplete")
    return _json(identity)


def _subject_rows(conn: sqlite3.Connection, subject_key: str, scope: Any) -> list[dict]:
    rows = conn.execute("SELECT * FROM cognitive_events WHERE subject_key=? AND " + _SCOPE_SQL + "=? ORDER BY seq", (subject_key, _scope_key(scope))).fetchall()
    # 老日志的 revision 曾跨会话累计；只重建缓存，不改历史日志。
    return [dict(row, revision=index) for index, row in enumerate(rows, 1)]


def _revision(conn: sqlite3.Connection, subject_key: str, scope: Any) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM cognitive_events WHERE subject_key=? AND " + _SCOPE_SQL + "=?", (subject_key, _scope_key(scope))).fetchone()[0])


def _project_subject(conn: sqlite3.Connection, subject_key: str, scope: Any) -> dict[str, Any] | None:
    return _project_rows(_subject_rows(conn, subject_key, scope))


def _project_rows(rows: Iterable[Any]) -> dict[str, Any] | None:
    """纯投影：跳过被取代的事件，取最后一条有效事件。

    不合并多条 payload —— 第一版只保证「当前状态是哪条事件说的」可回答；
    更复杂的合并规则等有真实用法再加，避免现在拍脑袋。
    """
    materialized = list(rows)
    superseded = {str(row["supersedes"]) for row in materialized if row["supersedes"]}
    last = None
    for row in materialized:
        if str(row["event_id"]) in superseded:
            continue
        last = row
    if last is None:
        return None
    return {
        "event_id": str(last["event_id"]),
        "operation": str(last["operation"]),
        "payload": json.loads(str(last["payload"])),
        "evidence_refs": json.loads(str(last["evidence_refs"])),
        "revision": int(last["revision"]),
        "updated_at": str(last["committed_at"]),
    }


def state_from_events(rows: Iterable[Mapping[str, Any] | sqlite3.Row]) -> dict:
    """按完整会话和主体投影；单会话输入兼容旧主体键返回值。"""
    grouped = {}
    for row in rows:
        scope = json.loads(row["scope"]) if "scope" in row.keys() else None
        identity = _scope_identity(scope)
        grouped.setdefault((identity, str(row["subject_key"])), []).append(row)
    identities = {identity for identity, subject in grouped}
    result = {}
    for (identity, subject), subject_rows in grouped.items():
        normalized = [dict(row, revision=index) for index, row in enumerate(subject_rows, 1)] if identity is not None else subject_rows
        state = _project_rows(normalized)
        if state is not None:
            result[subject if len(identities) <= 1 else (identity, subject)] = state
    return result


def _write_state(conn: sqlite3.Connection, subject_key: str, scope: Any) -> None:
    state = _project_subject(conn, subject_key, scope)
    key = _scope_key(scope)
    if state is None:
        conn.execute("DELETE FROM cognitive_state_scoped WHERE scope_key=? AND subject_key=?", (key, subject_key))
        return
    conn.execute(
        "INSERT INTO cognitive_state_scoped(scope_key,subject_key,revision,event_id,operation,payload,updated_at) VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(scope_key,subject_key) DO UPDATE SET revision=excluded.revision,event_id=excluded.event_id,operation=excluded.operation,payload=excluded.payload,updated_at=excluded.updated_at",
        (key, subject_key, state["revision"], state["event_id"], state["operation"], _json(state["payload"]), state["updated_at"]),
    )


# --------------------------------------------------------------------------- commit


def commit_updates(
    conn: sqlite3.Connection,
    *,
    context: CommitContext,
    proposals: Sequence[Proposal],
    expected_revisions: Mapping[str, int] | None = None,
    idempotency_key: str,
    now: datetime | None = None,
) -> CommitResult:
    """唯一写入口。整批要么全提交，要么一条都不写。"""
    batch = tuple(proposals or ())
    rejected = _validate(context, batch, idempotency_key)
    if rejected:
        return CommitResult(rejected=tuple(rejected))

    original_key = idempotency_key
    idempotency_key = "scope-v2:" + hashlib.sha256((_scope_key(context.scope) + "\x00" + original_key).encode()).hexdigest()
    expectations = {str(k): int(v) for k, v in (expected_revisions or {}).items()}
    committed_at = _stamp(now)
    subjects = sorted({proposal.subject_key for proposal in batch})

    conn.execute("BEGIN IMMEDIATE")
    try:
        from core.memory.legacy_ownership import MEMORY_OPERATIONS, retired
        if any(p.operation in MEMORY_OPERATIONS for p in batch) and retired(conn, _scope_key(context.scope)):
            conn.execute("ROLLBACK")
            return CommitResult(rejected=tuple((p.proposal_id, "cognitive_memory_retired_use_legacy") for p in batch))
        # 幂等判定先于版本判定：重试一次已经成功的批次时，版本当然已经变了，
        # 但那是「同一件事」，不该报冲突。
        committed: list[str] = []
        duplicates: list[str] = []
        pending: list[Proposal] = []
        for proposal in batch:
            event_id = event_id_for(
                context.scope,
                proposal.resolved_occurrence(),
                proposal.subject_key,
                proposal.operation,
            )
            existing = conn.execute(
                "SELECT event_id, payload FROM cognitive_events"
                " WHERE idempotency_key IN (?,?) AND proposal_id = ? AND " + _SCOPE_SQL + "=?",
                (idempotency_key, original_key, proposal.proposal_id, _scope_key(context.scope)),
            ).fetchone()
            if existing is None:
                pending.append(proposal)
                continue
            if str(existing["event_id"]) != event_id or _json(dict(proposal.payload)) != str(
                existing["payload"]
            ):
                conn.execute("ROLLBACK")
                return CommitResult(
                    rejected=((proposal.proposal_id, "idempotency_conflict"),)
                )
            duplicates.append(event_id)

        if not pending:
            conn.execute("ROLLBACK")
            return CommitResult(
                duplicates=tuple(duplicates),
                revisions={subject: _revision(conn, subject, context.scope) for subject in subjects},
            )

        # 只对「这次真的要写」的主体做版本检查；纯重复的批次上面已经返回。
        stale: list[tuple[str, str]] = []
        for subject in sorted({proposal.subject_key for proposal in pending}):
            if subject not in expectations:
                continue
            current = _revision(conn, subject, context.scope)
            if current != expectations[subject]:
                stale.extend(
                    (proposal.proposal_id, "revision_conflict:%d" % current)
                    for proposal in pending
                    if proposal.subject_key == subject
                )
        if stale:
            conn.execute("ROLLBACK")
            return CommitResult(rejected=tuple(stale))

        for proposal in pending:
            event_id = event_id_for(
                context.scope,
                proposal.resolved_occurrence(),
                proposal.subject_key,
                proposal.operation,
            )
            if proposal.supersedes:
                prior = conn.execute(
                    "SELECT subject_key, scope FROM cognitive_events WHERE event_id = ?",
                    (proposal.supersedes,),
                ).fetchone()
                if prior is None or str(prior["subject_key"]) != proposal.subject_key or _scope_identity(json.loads(prior["scope"])) != context.scope.key:
                    conn.execute("ROLLBACK")
                    return CommitResult(
                        rejected=((proposal.proposal_id, "supersedes_scope_mismatch"),)
                    )

            revision = _revision(conn, proposal.subject_key, context.scope) + 1
            conn.execute(
                "INSERT INTO cognitive_events("
                " event_id, idempotency_key, proposal_id, subject_key, operation, payload,"
                " evidence_refs, scope, actor_key, source, supersedes, revision, occurred_at,"
                " committed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    idempotency_key,
                    proposal.proposal_id,
                    proposal.subject_key,
                    proposal.operation,
                    _json(dict(proposal.payload)),
                    _json([str(ref) for ref in proposal.evidence_refs if str(ref)]),
                    _json(context.scope.to_dict()),
                    str(context.actor_key or ""),
                    str(context.source or ""),
                    str(proposal.supersedes or ""),
                    revision,
                    str(proposal.occurred_at or ""),
                    committed_at,
                ),
            )
            committed.append(event_id)

        for subject in subjects:
            _write_state(conn, subject, context.scope)

        cursor = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM cognitive_events"
        ).fetchone()
        conn.execute(
            "INSERT INTO cognitive_cursors(consumer, seq) VALUES('commit', ?)"
            " ON CONFLICT(consumer) DO UPDATE SET seq=excluded.seq",
            (int(cursor["s"] if cursor else 0),),
        )
        revisions = {subject: _revision(conn, subject, context.scope) for subject in subjects}
        conn.execute("COMMIT")
        invalidate_closed_cache()
        return CommitResult(
            committed=tuple(committed),
            duplicates=tuple(duplicates),
            revisions=revisions,
        )
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            skips.skip("cognitive_commit.commit_updates:rollback", "degraded", repr(exc)[:120])
        raise


# --------------------------------------------------------------------------- reads


def _state_table_exists(conn: sqlite3.Connection) -> bool:
    """读路径先问「这张表存在吗」：表由首次写连接 ensure_schema 创建。"""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognitive_state_scoped'"
    ).fetchone() is not None


def read_state(conn: sqlite3.Connection, subject_key: str, *, scope: Any = None) -> dict[str, Any] | None:
    if not _state_table_exists(conn):
        return None
    if scope is None:
        rows = conn.execute("SELECT DISTINCT scope_key FROM cognitive_state_scoped WHERE subject_key=?", (subject_key,)).fetchall()
        if len(rows) > 1:
            raise ValueError("ambiguous_subject_scope")
        key = rows[0][0] if rows else ""
    else:
        key = _scope_key(scope)
    row = conn.execute(
        "SELECT revision,event_id,operation,payload,updated_at FROM cognitive_state_scoped WHERE scope_key=? AND subject_key=?",
        (key, subject_key),
    ).fetchone()
    if row is None:
        return None
    from core.memory.legacy_ownership import MEMORY_OPERATIONS, retired
    if str(row["operation"]) in MEMORY_OPERATIONS and retired(conn, key):
        return None
    return {
        "revision": int(row["revision"]),
        "event_id": str(row["event_id"]),
        "operation": str(row["operation"]),
        "payload": json.loads(str(row["payload"])),
        "updated_at": str(row["updated_at"]),
    }


def read_cursor(conn: sqlite3.Connection, consumer: str = "commit") -> int:
    row = conn.execute(
        "SELECT seq FROM cognitive_cursors WHERE consumer = ?", (consumer,)
    ).fetchone()
    return int(row["seq"] if row else 0)


def _scope_identity(scope: Any) -> tuple[str, str, str] | None:
    if isinstance(scope, dict):
        platform = str(scope.get("platform") or "")
        account = str(scope.get("account_id") or "")
        chat = str(scope.get("chat_id") or "")
    else:
        platform = str(getattr(scope, "platform", "") or "")
        account = str(getattr(scope, "account_id", "") or "")
        chat = str(getattr(scope, "chat_id", "") or "")
    if not (platform and account and chat):
        return None
    return (platform, account, chat)


def _closed_subjects(
    match: Any,
    *,
    cache_key: tuple[str, ...],
    path: str | None,
    ttl: float,
) -> frozenset[str]:
    resolved = str(path or _env_text("YINYUE_COGNITIVE_DB").strip() or "")
    if not resolved:
        from db import world_db

        resolved = str(world_db.PATH)
    full_key = (resolved,) + cache_key
    now = time.monotonic()
    hit = _CLOSED_CACHE.get(full_key)
    if hit is not None and now - hit[0] < max(0.0, float(ttl)):
        return hit[1]
    # 测试进程里不读生产库：别处的真实关闭会污染测试判定。
    if _blocked_under_tests(resolved):
        return frozenset()
    # 读侧绝不建库/建表：库不在就是「没有关闭记录」，不是「顺手把生产库动一下」。
    if not os.path.exists(resolved):
        return frozenset()

    conn = None
    try:
        conn = sqlite3.connect(
            "file:%s?mode=ro" % resolved.replace("?", "%3f"), uri=True, timeout=1.0
        )
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT subject_key, operation, scope FROM cognitive_events ORDER BY seq"
        ).fetchall()
        # 同名 goal 可能出现在不同会话（「到点:喝水」很常见），所以「最新一条」
        # 必须在**匹配的会话内**取，不能拿全局 MAX(seq) —— 否则别处的关闭会把
        # 这里的未关闭目标顶掉。
        latest: dict[str, str] = {}
        for row in rows:
            try:
                scope = json.loads(str(row["scope"]))
            except (TypeError, ValueError) as exc:
                skips.skip("cognitive_commit._closed_subjects:scope", "malformed", repr(exc)[:120])
                continue
            if match(scope):
                latest[str(row["subject_key"])] = str(row["operation"])
        closed = {subject for subject, operation in latest.items() if operation == "close"}
    except Exception:  # noqa: BLE001 - 读不到就不关闭
        _CLOSED_CACHE[full_key] = (now, frozenset())
        return frozenset()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as exc:  # noqa: BLE001
                skips.skip("cognitive_commit._closed_subjects:conn_close", "degraded", repr(exc)[:120])

    result = frozenset(closed)
    _CLOSED_CACHE[full_key] = (now, result)
    return result


def closed_subjects_for_chat(
    chat_id: str,
    *,
    path: str | None = None,
    ttl: float = 1.0,
) -> frozenset[str]:
    """这个会话（只按 chat_id）里「最后一条有效事件是 close」的 subject。

    过渡口径：旧世界模型文件名只按 chat_id，盘上没有 platform/account。
    新模型应优先用 :func:`closed_subjects_for_scope`。
    """
    key = str(chat_id or "")
    if not key:
        return frozenset()
    return _closed_subjects(
        lambda scope: str(scope.get("chat_id") or "") == key,
        cache_key=("chat", key),
        path=path,
        ttl=ttl,
    )


def closed_subjects_for_scope(
    scope: Any,
    *,
    path: str | None = None,
    ttl: float = 1.0,
) -> frozenset[str]:
    """按完整会话键（platform/account/chat）派生关闭状态；缺任一字段返回空。"""
    identity = _scope_identity(scope)
    if identity is None:
        return frozenset()
    return _closed_subjects(
        lambda row_scope: (
            str(row_scope.get("platform") or ""),
            str(row_scope.get("account_id") or ""),
            str(row_scope.get("chat_id") or ""),
        )
        == identity,
        cache_key=("scope",) + identity,
        path=path,
        ttl=ttl,
    )


def invalidate_closed_cache() -> None:
    """提交/重建后清空读侧缓存（同一进程内下一次加载立即看到新状态）。"""
    _CLOSED_CACHE.clear()


def rebuild(conn: sqlite3.Connection) -> dict[str, int]:
    """同一写事务内按会话重建投影；原始日志和旧投影保留供回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute("SELECT seq, event_id, subject_key, scope FROM cognitive_events ORDER BY seq").fetchall()
        groups = {}
        for row in rows:
            scope = json.loads(row["scope"])
            if _scope_identity(scope) is not None:
                groups[(_scope_key(scope), row["subject_key"])] = scope
        conn.execute("DELETE FROM cognitive_state_scoped")
        revisions = {}
        for (key, subject), scope in groups.items():
            _write_state(conn, subject, scope)
            revisions[(key, subject)] = _revision(conn, subject, scope)
        conn.execute("COMMIT")
        invalidate_closed_cache()
    except Exception:
        conn.execute("ROLLBACK")
        raise
    # 旧调用方在主体不重名时仍可按主体查询返回版本。
    result = {}
    for (key, subject), revision in revisions.items():
        result[subject if sum(s == subject for _, s in revisions) == 1 else (key, subject)] = revision
    return result


def closed_states_for_scope(scope: Any, *, path: str | None = None) -> dict[str, str]:
    """完整会话内的有效终态；与写侧共用 supersedes 投影规则。"""
    identity = _scope_identity(scope)
    resolved = str(path or _env_text("YINYUE_COGNITIVE_DB").strip() or "")
    if not resolved:
        from db import world_db
        resolved = str(world_db.PATH)
    if identity is None or _blocked_under_tests(resolved) or not os.path.isfile(resolved):
        return {}
    conn = sqlite3.connect(Path(resolved).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM cognitive_events ORDER BY seq").fetchall()
        states = state_from_events(row for row in rows if _scope_identity(json.loads(row["scope"])) == identity)
        closed = {}
        for subject, projection in states.items():
            if projection["operation"] != "close":
                continue
            body = projection["payload"]
            terminal = body.get("state")
            if not terminal:
                terminal = "abandoned" if str(body.get("result") or "").startswith(("expired", "abandoned", "gave_up")) else "completed"
            if terminal in {"completed", "abandoned"}:
                closed[subject] = terminal
        return closed
    finally:
        conn.close()
