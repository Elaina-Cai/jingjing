"""隔离的旧库迁移/切换/回滚演练；只操作 .test_tmp 下的合成数据。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_source(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE memory_projections(memory_id TEXT PRIMARY KEY, memory_type TEXT, content TEXT, platform TEXT, bot_account_id TEXT, metadata_json TEXT, source_event_ids_json TEXT, visibility_scope_json TEXT, sensitivity TEXT, valid_from TEXT)")
        conn.execute("CREATE TABLE terminal_events(event_id TEXT PRIMARY KEY,platform TEXT,bot_account_id TEXT,chat_id TEXT,event_type TEXT,content TEXT,sensitivity TEXT,visibility_scope_json TEXT)")
        for index in range(2):
            conn.execute("INSERT INTO memory_projections VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (f"old-{index}", "fact", f"旧地址记录-{index}", "wechat", "a",
                          json.dumps({"chat_id": "same"}), json.dumps([f"event-{index}"]),
                          '["public"]', "normal", "2026-03-01"))
            conn.execute("INSERT INTO terminal_events VALUES(?,?,?,?,?,?,?,?)",
                         (f"event-{index}", "wechat", "a", "same", "message",
                          "原始聊天证据", "normal", '["public"]'))
        conn.commit()
    finally:
        conn.close()


def _approved_review(report: dict) -> dict:
    reviews = []
    for index, record in enumerate(report["records"]):
        reviews.append({"source_key": record["source_key"],
                        "row_fingerprint": record["row_fingerprint"],
                        "evidence_fingerprint": record.get("evidence_fingerprint", ""),
                        "decision": "approved" if index == 0 else "defer",
                        "reviewer": "drill-reviewer" if index == 0 else "",
                        "valid_from": "2026-01-01" if index == 0 else "",
                        "valid_to": ""})
    return {"schema_version": 2, "source_id": report["source_id"], "reviews": reviews}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True, help="位于 .test_tmp 或系统临时 yinyue-legacy-import 下的空目录")
    parser.add_argument("--source-id", default="drill-meta")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).resolve()
    allowed = (ROOT / ".test_tmp", Path(tempfile.gettempdir()) / "yinyue-legacy-import")
    if not any(workdir == root or root in workdir.parents for root in allowed):
        print(json.dumps({"error": "drill_workdir_must_be_isolated", "workdir": str(workdir)}, ensure_ascii=False))
        return 2
    workdir.mkdir(parents=True, exist_ok=True)
    source, target, binding_path = workdir / "source.db", workdir / "target.db", workdir / "binding.json"
    if any(path.exists() for path in (source, target, binding_path)):
        print(json.dumps({"error": "drill_workdir_not_empty", "workdir": str(workdir)}, ensure_ascii=False))
        return 2
    os.environ["YINYUE_COGNITIVE_DB"] = str(target)
    os.environ["YINYUE_WORLD_MODEL_DIR"] = str(workdir / "world")

    from sqlalchemy import create_engine
    from core.memory.legacy_acceptance import verify_cutover, verify_import
    from core.memory.legacy_audit import audit_database
    from core.memory.legacy_import import import_reviewed
    from core.memory.legacy_read_binding import load_read_binding
    from core.memory.legacy_review import check_review, review_template
    from core.memory.legacy_write_freeze import freeze_projection_writes
    from db.repo_memory_projections import MemoryProjectionRepository

    steps = []

    def record(name, ok, **observed):
        steps.append({"step": name, "ok": bool(ok), "observed": observed})
        if not ok:
            raise AssertionError("drill_step_failed:" + name)

    _make_source(source)
    report = audit_database(source, source_id=args.source_id)
    record("audit", report["total"] == 2 and report["counts"] == {"eligible_for_review": 2} and report["read_only"],
           total=report["total"], counts=report["counts"], read_only=report["read_only"])

    template = review_template(report)
    (workdir / "review-template.json").write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    review = _approved_review(report)
    checked = check_review(report, review)
    if not checked["ok"]:
        raise ValueError("drill_review_invalid:" + json.dumps(checked, ensure_ascii=False))
    record("review", checked["approved"] == 1 and checked["deferred"] == 1 and not checked["unreviewed_source_keys"],
           approved=checked["approved"], deferred=checked["deferred"], unreviewed=len(checked["unreviewed_source_keys"]))

    dry = import_reviewed(source, source_id=args.source_id, review=review, target=target, dry_run=True)
    record("import_dry", dry["selected"] == 1 and dry["committed"] == 0 and not target.exists(),
           selected=dry["selected"], committed=dry["committed"], target_created=target.exists())

    applied = import_reviewed(source, source_id=args.source_id, review=review, target=target, dry_run=False)
    record("import_apply", applied["selected"] == 1 and applied["committed"] == 1 and applied["duplicates"] == 0,
           selected=applied["selected"], committed=applied["committed"], duplicates=applied["duplicates"])

    acceptance = verify_import(source, source_id=args.source_id, target=target)
    record("acceptance", acceptance["integrity_ok"] is True and acceptance["cutover_ready"] is False
           and acceptance["counts"]["imported"] == 1,
           integrity_ok=acceptance["integrity_ok"], cutover_ready=acceptance["cutover_ready"],
           pending=acceptance["pending"], imported=acceptance["counts"]["imported"])

    binding_path.write_text(json.dumps({"schema_version": 1, "source": str(source),
                                        "source_id": args.source_id, "target": str(target)}, ensure_ascii=False), encoding="utf-8")
    binding = load_read_binding(binding_path)
    engine = create_engine("sqlite:///" + str(source))
    repository = MemoryProjectionRepository(engine)
    try:
        frozen = freeze_projection_writes(binding, dry_run=False)
        record("freeze", frozen["frozen"] is True and frozen["already_marked"] is False
               and frozen["preserved_projection_rows"] == 2,
               frozen=frozen["frozen"], already_marked=frozen["already_marked"],
               preserved_projection_rows=frozen["preserved_projection_rows"])

        proof = verify_cutover(source, source_id=args.source_id, target=target,
                               binding=binding, repository=repository)
        record("cutover_proof", proof["cutover_ready"] is True and not proof["pending"]
               and proof["read_binding"].get("imported") == 1 and proof["read_binding"].get("keys") == 1,
               cutover_ready=proof["cutover_ready"], pending=proof["pending"],
               imported=proof["read_binding"].get("imported"), keys=proof["read_binding"].get("keys"))

        engine.dispose()
        engine = create_engine("sqlite:///" + str(source))
        repository = MemoryProjectionRepository(engine)
        restarted = load_read_binding(binding_path)
        keys = restarted.snapshot(repository)
        replay = verify_cutover(source, source_id=args.source_id, target=target,
                                binding=restarted, repository=repository)
        record("restart", len(keys) == 1 and replay["cutover_ready"] is True,
               binding_keys=len(keys), cutover_ready=replay["cutover_ready"])

        from core.memory.legacy_write_freeze import unfreeze_projection_writes, assert_projection_writable
        from core.context import cognitive_commit as cc
        from core.memory.legacy_ownership import retired
        conn = sqlite3.connect(target)
        before = conn.execute("SELECT * FROM cognitive_events ORDER BY seq").fetchall()
        conn.close()
        rollback = unfreeze_projection_writes(restarted, dry_run=False,
                    reason="isolated ownership handback drill", confirm="unfreeze:" + restarted.scope_key)
        assert_projection_writable(repository.read_engine())
        conn = sqlite3.connect(target)
        after = conn.execute("SELECT * FROM cognitive_events ORDER BY seq").fetchall()
        target_retired = retired(conn, restarted.scope_key)
        conn.close()
        restored = load_read_binding(binding_path)
        old_read_restored = restored.snapshot(repository) == frozenset()
        record("rollback", before == after and target_retired and old_read_restored and rollback["unfrozen"],
               target_journal_preserved=before == after, target_memory_retired=target_retired,
               legacy_reads_restored=old_read_restored, legacy_writes_restored=True,
               binding_removed=False, automatic_legacy_resurrection=False)
    finally:
        engine.dispose()

    print(json.dumps({"ok": True, "workdir": str(workdir), "steps": steps}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
