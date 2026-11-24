"""审核后的单来源/单会话 Belief 导入；沙箱默认 dry，生产目标另需绑定文件。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile

from core.context import cognitive_commit as cc
from core.context.situation import Belief
from core.memory.belief_commit import database_path, logical_subject
from core.memory.legacy_audit import read_snapshot
from core.memory.legacy_review import check_review, review_validity

ROOT = Path(__file__).resolve().parents[2]
SANDBOX_ROOTS = (ROOT / ".test_tmp", Path(tempfile.gettempdir()) / "yinyue-legacy-import")
SANDBOX_MARKER = "legacy_import_sandbox"


def import_domain(conn, *, schema: str = ""):
    # 读导入域标记：生产表优先、沙箱表兜底；没有标记表时返回 None。
    master = (schema + ".sqlite_master") if schema else "sqlite_master"
    for name in MARKERS:
        table = (schema + "." + name) if schema else name
        if not conn.execute("SELECT 1 FROM %s WHERE type='table' AND name=?" % master, (name,)).fetchone():
            continue
        row = conn.execute("SELECT source_id,scope_key FROM %s WHERE singleton=1" % table).fetchone()
        return None if row is None else tuple(row)
    return None


PRODUCTION_MARKER = "legacy_import_production"
MARKERS = (PRODUCTION_MARKER, SANDBOX_MARKER)


def _target(path, source, *, production: bool = False) -> Path:
    target = Path(path).resolve()
    if production:
        if target != Path(database_path()).resolve():
            raise ValueError("production_target_must_be_belief_reader")
    elif not any(target.is_relative_to(root.resolve()) for root in SANDBOX_ROOTS):
        raise ValueError("target_must_be_isolated_sandbox")
    if target == source or (target.exists() and os.path.samefile(target, source)):
        raise ValueError("source_and_target_must_differ")
    return target


def _domain(path: Path, *, strict: bool = True):
    if not path.exists():
        return None
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        domain = import_domain(conn)
        if domain is None:
            if strict:
                raise ValueError("target_is_not_import_sandbox")
            return None
        return domain
    finally:
        conn.close()


def _plan(source, *, source_id: str, review: dict, target) -> dict:
    """读快照 + 核对清单 + 组 proposals；不碰目标库，供沙箱/生产两条路复用。"""
    source = Path(source).resolve(strict=True)
    source_id = str(source_id).strip()
    snapshot = read_snapshot(source, source_id=source_id)
    records = {item["source_key"]: item for item in snapshot.report["records"]}
    checked = check_review(snapshot.report, review)
    if not checked["ok"]:
        raise ValueError(";".join(item["reason"] for item in checked["issues"] if item["blocking"]))
    selected = []
    for item in review["reviews"]:
        if item["decision"] != "approved":
            continue
        start, end = review_validity(item)
        selected.append((item, records[item["source_key"]], start, end))
    report = {"dry_run": True, "source_id": source_id, "selected": len(selected),
              "retained_in_source": snapshot.report["total"] + sum(snapshot.report["omitted_rows"].values()) - len(selected), "source_counts": snapshot.report["counts"],
              "omitted_rows": snapshot.report["omitted_rows"], "source_tables": snapshot.report["tables"],
              "committed": 0, "duplicates": 0, "target": str(target)}
    plan = {"source": source, "source_id": source_id, "snapshot": snapshot, "selected": selected,
            "report": report, "scope": None, "domain": None, "proposals": [],
            "refs": set(), "subjects": set()}
    if not selected:
        return plan
    identities = {tuple(record["scope"][field] for field in ("platform", "account_id", "chat_id")) for _, record, _, _ in selected}
    if len(identities) != 1:
        raise ValueError("one_conversation_per_import_batch")
    identity = next(iter(identities))
    is_group = any("group_members" in record["declared_visibility"] for _, record, _, _ in selected)
    scope = cc.Scope(*identity, chat_type="group" if is_group else "private", group_id=identity[2] if is_group else "")
    domain = (source_id, cc._scope_key(scope))
    existing_domain = _domain(target, marker=SANDBOX_MARKER) if False else None
    proposals = []
    refs = set()
    subjects = set()
    for item, record, start, end in selected:
        key = record["source_key"]
        raw = snapshot.rows[key]
        fact = {"key": key, "subject_key": key, "value": str(raw["content"]), "source": "chat_observed",
                "scope": ":".join(scope.key), "valid_from": start, "valid_to": end,
                "evidence_refs": record["evidence_refs"]}
        subject = logical_subject(Belief(key=key, subject_key=key))
        subjects.add(subject)
        refs.update(record["evidence_refs"])
        body = {"world_fact": fact, "at": "", "legacy": {
            "source_id": source_id, "source_key": key, "row_fingerprint": record["row_fingerprint"],
            "reviewer": str(item["reviewer"]).strip(), "evidence_fingerprint": record["evidence_fingerprint"], "declared_visibility": record["declared_visibility"],
            "source_visibility": record["source_visibility"], "original_validity": record["validity"]}}
        proposals.append(cc.Proposal(key, subject, "believe", body, tuple(record["evidence_refs"]), occurrence=key))
    plan.update(scope=scope, domain=domain, proposals=proposals, refs=refs, subjects=subjects)
    return plan


def _write(conn, plan: dict, *, marker: str) -> None:
    domain = plan["domain"]
    conn.execute("CREATE TABLE IF NOT EXISTS %s(singleton INTEGER PRIMARY KEY CHECK(singleton=1),source_id TEXT NOT NULL,scope_key TEXT NOT NULL)" % marker)
    conn.execute("INSERT OR IGNORE INTO %s VALUES(1,?,?)" % marker, domain)
    if tuple(conn.execute("SELECT source_id,scope_key FROM %s WHERE singleton=1" % marker).fetchone()) != domain:
        raise ValueError("target_source_or_scope_conflict")


def _commit(conn, plan: dict, *, marker: str) -> dict:
    _write(conn, plan, marker=marker)
    outcome = cc.commit_updates(
        conn,
        context=cc.CommitContext(
            plan["scope"], visible_event_ids=frozenset(plan["refs"]),
            allowed_subjects=frozenset(plan["subjects"]), source="legacy_import"),
        proposals=plan["proposals"],
        expected_revisions={subject: 0 for subject in plan["subjects"]},
        idempotency_key="legacy-import:" + plan["source_id"],
    )
    if not outcome.ok:
        raise ValueError("legacy_import_conflict:" + str(outcome.rejected))
    report = plan["report"]
    report.update(committed=len(outcome.committed), duplicates=len(outcome.duplicates))
    return report


def import_reviewed(source, *, source_id: str, review: dict, target, dry_run: bool = True) -> dict:
    """源行/证据来自同一只读快照；批准项有一项过期或不合格便整批拒绝。"""
    source = Path(source).resolve(strict=True)
    target = _target(target, source)
    plan = _plan(source, source_id=source_id, review=review, target=target)
    report = plan["report"]
    report["dry_run"] = dry_run
    if not plan["selected"]:
        return report
    existing_domain = _domain(target)
    if existing_domain is not None and existing_domain != plan["domain"]:
        raise ValueError("target_source_or_scope_conflict")
    if dry_run:
        return report
    target.parent.mkdir(parents=True, exist_ok=True)
    _target(target, source)
    conn = cc.connect(str(target))
    try:
        return _commit(conn, plan, marker=SANDBOX_MARKER)
    finally:
        conn.close()


def import_reviewed_production(source, *, source_id: str, review: dict, target, binding, dry_run: bool = True) -> dict:
    """写进生产 belief 库：只认读取器的真实库，并要求绑定文件钉死源/目标/来源。

    停写闸只能在导入之后建立——``freeze_projection_writes`` 先跑 ``verify_import``
    并要求目标里已有这一批。所以生产顺序是：导入 → 停写 → ``load_read_binding``
    复核 → ``verify_cutover`` → 切读。两步之间旧投影若又冒出新行，
    ``verify_cutover`` 会因 key 漂移拒绝切换；此时重跑本函数（幂等）或解冻回退，
    不会出现两份所有者。
    """
    source = Path(source).resolve(strict=True)
    target = Path(target).resolve()
    if target != Path(database_path()).resolve():
        raise ValueError("production_target_must_be_belief_reader")
    if target == source or (target.exists() and os.path.samefile(target, source)):
        raise ValueError("source_and_target_must_differ")
    bound = json.loads(Path(binding).read_text(encoding="utf-8"))
    if (not isinstance(bound, dict)
            or Path(str(bound.get("source") or "")).resolve() != source
            or str(bound.get("source_id") or "") != str(source_id).strip()
            or Path(str(bound.get("target") or "")).resolve() != target):
        raise ValueError("production_binding_mismatch")
    plan = _plan(source, source_id=source_id, review=review, target=target)
    report = plan["report"]
    report.update(dry_run=dry_run, target_kind="production")
    if not plan["selected"]:
        return report
    existing_domain = _domain(target, strict=False)
    if existing_domain is not None and existing_domain != plan["domain"]:
        raise ValueError("target_source_or_scope_conflict")
    if dry_run:
        return report
    conn = cc.connect(str(target))
    try:
        return _commit(conn, plan, marker=PRODUCTION_MARKER)
    finally:
        conn.close()
