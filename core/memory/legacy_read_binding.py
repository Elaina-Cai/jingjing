"""显式旧库读取权绑定；只支持隔离目标，未配置时不认领任何来源。"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from core.memory.legacy_acceptance import verify_import
from core.memory.legacy_import import import_domain
from core.memory.legacy_audit import _hash


@dataclass(frozen=True)
class ReadBinding:
    source: Path
    source_id: str
    target: Path
    scope_key: str
    initial_keys: frozenset[str]
    file_identity: tuple[int, int]

    def snapshot(self, repository) -> frozenset[str]:
        from core.memory.belief_commit import database_path
        if Path(database_path()).resolve() != self.target:
            raise ValueError("belief_reader_target_changed")
        engine = repository.read_engine()
        url = getattr(engine, "url", None)
        if url is None or not url.drivername.startswith("sqlite") or not url.database or Path(url.database).resolve() != self.source:
            raise ValueError("legacy_reader_source_mismatch")
        stat = self.source.stat()
        if (stat.st_dev, stat.st_ino) != self.file_identity:
            raise ValueError("legacy_reader_source_replaced")
        from core.memory.legacy_write_freeze import verify_freeze_owner
        frozen = verify_freeze_owner(self.source, source_id=self.source_id, scope_key=self.scope_key, target=self.target)
        from core.memory.legacy_ownership import binding_scope
        rolled_back = binding_scope(self.source, self.target, self.source_id)
        if rolled_back:
            if rolled_back != self.scope_key or frozen:
                raise ValueError("legacy_rollback_owner_conflict")
            return frozenset()
        conn = sqlite3.connect(self.target.as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            domain = import_domain(conn)
            if domain != (self.source_id, self.scope_key):
                raise ValueError("legacy_reader_target_domain_changed")
            from core.context import cognitive_commit as cc
            rows = conn.execute("SELECT payload FROM cognitive_events WHERE source='legacy_import' AND operation='believe' AND " + cc._SCOPE_SQL + "=? ORDER BY seq", (self.scope_key,)).fetchall()
            keys = set()
            for (raw,) in rows:
                body = json.loads(raw)
                legacy = body.get("legacy") if isinstance(body, dict) else None
                fact = body.get("world_fact") if isinstance(body, dict) else None
                if not isinstance(legacy, dict) or not isinstance(fact, dict) or legacy.get("source_id") != self.source_id or not isinstance(legacy.get("source_key"), str) or not legacy["source_key"] or legacy["source_key"] != fact.get("key") or fact.get("subject_key") != legacy["source_key"] or fact.get("scope") != ":".join(json.loads(self.scope_key)):
                    raise ValueError("invalid_legacy_reader_journal")
                keys.add(legacy["source_key"])
            if not self.initial_keys <= keys:
                raise ValueError("legacy_reader_ownership_disappeared")
            return frozenset(keys)
        finally:
            conn.close()

    def owns(self, memory_id: str, snapshot: frozenset[str]) -> bool:
        return "legacy:" + _hash([self.source_id, "memory_projections", memory_id]) in snapshot


def load_read_binding(path: str | Path) -> ReadBinding:
    from core.context.cognitive_commit import _scope_key, Scope
    from core.memory.belief_commit import database_path
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    fields = {"schema_version", "source", "source_id", "target"}
    if not isinstance(config, dict) or set(config) != fields or config["schema_version"] != 1 or any(not isinstance(config[key], str) or not config[key].strip() for key in fields - {"schema_version"}):
        raise ValueError("invalid_legacy_read_binding")
    if not Path(config["source"]).is_absolute() or not Path(config["target"]).is_absolute():
        raise ValueError("legacy_binding_requires_absolute_paths")
    source = Path(config["source"]).resolve(strict=True)
    target = Path(config["target"]).resolve(strict=True)
    if target != Path(database_path()).resolve():
        raise ValueError("legacy_binding_must_match_belief_reader")
    before = source.stat()
    from core.memory.legacy_ownership import binding_scope
    rolled_back = binding_scope(source, target, config["source_id"])
    if rolled_back:
        return ReadBinding(source, config["source_id"], target, rolled_back,
                           frozenset(), (source.stat().st_dev, source.stat().st_ino))
    report = verify_import(source, source_id=config["source_id"], target=target, production=True)
    if not report["integrity_ok"]:
        raise ValueError("legacy_binding_import_integrity_failed")
    scope = report["scope"]
    from core.memory.legacy_write_freeze import verify_freeze_owner
    verify_freeze_owner(source, source_id=report["source_id"], scope_key=_scope_key(Scope(scope["platform"],scope["account_id"],scope["chat_id"])), target=target)
    stat = source.stat()
    if (before.st_dev, before.st_ino) != (stat.st_dev, stat.st_ino):
        raise ValueError("legacy_reader_source_replaced_during_binding")
    return ReadBinding(source, report["source_id"], target,
        _scope_key(Scope(scope["platform"],scope["account_id"],scope["chat_id"])),
        frozenset(item["source_key"] for item in report["records"]), (stat.st_dev, stat.st_ino))
