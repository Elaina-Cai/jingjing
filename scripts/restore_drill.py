#!/usr/bin/env python
"""Restore drill: prove a backup can actually be restored and used.

Runs against the newest (or a given) ``backups/auto/*.zip``:

1. extracts to a temp workdir and verifies every manifest sha256;
2. opens the restored meta DB (integrity_check + row counts vs manifest);
3. replays meta migrations on the restored copy (proves the file is a real DB);
4. runs the hard-rule gate checks (provenance + non-owner recall) on the copy;
5. reports PASS/FAIL with timings.  Nothing outside the temp dir is touched.

Usage:
    python scripts/restore_drill.py
    python scripts/restore_drill.py --zip backups/auto/yinyue_backup_x.zip
    python scripts/restore_drill.py --keep-temp
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：系统 TEMP 不可写时
# ``tempfile.mkdtemp`` 会静默把解包目录建到仓库根，解出来的 sqlite 库跟着落进
# 仓库（并可能 CANTOPEN）。基座固定在 ``YINYUE_TEST_TMP`` 或仓库内 ``.test_tmp``。
sys.path.insert(0, str(ROOT / "tests"))
from _tempdirs import make_db_dir  # noqa: E402

AUTO_DIR = ROOT / "backups" / "auto"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_gate():
    import importlib.util

    path = ROOT / "scripts" / "gate_hard_rules.py"
    spec = importlib.util.spec_from_file_location("gate_hard_rules", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_drill(zip_path: Path, *, keep_temp: bool = False) -> bool:
    checks: list[tuple[str, bool, str]] = []
    started = time.time()
    workdir = make_db_dir("restore_drill_")
    ok = True
    try:
        # 1) extract + manifest verification
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(workdir)
        manifest_path = workdir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        bad = []
        for entry in manifest.get("files", []):
            target = workdir / entry["path"]
            if not target.exists():
                bad.append(f"missing {entry['path']}")
                continue
            if _sha256(target) != entry.get("sha256"):
                bad.append(f"sha mismatch {entry['path']}")
        checks.append(("manifest sha256", not bad, "; ".join(bad[:3])))
        ok &= not bad

        # 2) meta DB integrity + counts
        meta_copy = workdir / "meta" / "yinyue_meta.db"
        if not meta_copy.exists():
            candidates = list((workdir / "meta").glob("*.db"))
            meta_copy = candidates[0] if candidates else meta_copy
        integrity = "missing"
        counts: dict[str, int] = {}
        if meta_copy.exists():
            con = sqlite3.connect(f"file:{meta_copy.as_posix()}?mode=ro", uri=True)
            try:
                integrity = con.execute("pragma integrity_check").fetchone()[0]
                for table in ("terminal_events", "memory_projections"):
                    try:
                        counts[table] = int(
                            con.execute(f"select count(*) from {table}").fetchone()[0]
                        )
                    except sqlite3.OperationalError:
                        counts[table] = -1
            finally:
                con.close()
        expected = manifest.get("counts") or {}
        counts_ok = all(
            counts.get(key, -1) == int(value)
            for key, value in expected.items()
            if key in ("terminal_events", "memory_projections") and int(value) >= 0
        )
        checks.append(("integrity_check", integrity == "ok", integrity))
        checks.append(("row counts vs manifest", counts_ok, f"{counts} vs {expected}"))
        ok &= integrity == "ok" and counts_ok

        # 3) migrations replay on the restored copy
        if meta_copy.exists():
            try:
                from db.migrations import run_meta_migrations

                run_meta_migrations(str(meta_copy))
                checks.append(("meta migrations replay", True, "ok"))
            except Exception as exc:  # noqa: BLE001
                checks.append(("meta migrations replay", False, str(exc)[:120]))
                ok = False

        # 4) hard-rule gate on the restored copy
        if meta_copy.exists():
            gate = _load_gate()
            con = sqlite3.connect(str(meta_copy))
            try:
                violations = gate.check_projection_provenance(
                    con, table="memory_projections", id_column="memory_id"
                )
            finally:
                con.close()
            try:
                from db.meta_repo import MetaRepositoryManager

                meta = MetaRepositoryManager(str(meta_copy))
                try:
                    violations += gate.check_non_owner_recall(meta)
                finally:
                    meta.engine.dispose()
            except Exception as exc:  # noqa: BLE001
                violations.append(f"recall check failed: {exc}")
            checks.append(("hard rules on restored copy", not violations, "; ".join(violations[:3])))
            ok &= not violations
    finally:
        if keep_temp:
            print(f"[drill] workdir kept: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    elapsed = time.time() - started
    for name, passed, detail in checks:
        print(f"  {'ok  ' if passed else 'FAIL'} {name} ({detail})")
    print(f"restore drill: {'PASSED' if ok else 'FAILED'} in {elapsed:.1f}s  [{zip_path.name}]")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", default="")
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    if args.zip:
        zip_path = Path(args.zip)
    else:
        candidates = sorted(AUTO_DIR.glob("yinyue_backup_*.zip"))
        if not candidates:
            print(f"[err] no backup found in {AUTO_DIR}; run scripts/backup_all.py first")
            return 2
        zip_path = candidates[-1]
    if not zip_path.exists():
        print(f"[err] backup not found: {zip_path}")
        return 2
    return 0 if run_drill(zip_path, keep_temp=args.keep_temp) else 1


if __name__ == "__main__":
    raise SystemExit(main())
