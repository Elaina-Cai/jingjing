#!/usr/bin/env python
"""Daily backup for yinyue: consistent snapshots, one zip, a manifest.

What it backs up (by default):

* ``data/yinyue_meta.db`` -- facts + projections (snapshotted with SQLite's
  online backup API, safe while the assistant is running);
* every ``data/platforms/<platform>/<account>/account.db``;
* ``.env`` (keys / owner ids) and ``configs/`` (persona seeds, policies);
* a ``manifest.json`` with sizes, sha256 and key row counts.

``data/chroma`` is a *derived* index: restore rebuilds it with
``scripts/rebuild_memory_index.py``.  Use ``--include-chroma`` to zip it anyway.

Usage:
    python scripts/backup_all.py                 # write one backup
    python scripts/backup_all.py --dry-run
    python scripts/backup_all.py --keep 30
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 临时目录的唯一入口（见 tests/_tempdirs.py）。以前这里用
# ``tempfile.mkdtemp``：本机沙箱下系统 TEMP 不可写时 ``gettempdir()`` 会静默
# 回落到仓库根，staging（含它里面的 sqlite 库）就正好落在仓库里。基座要么是
# ``YINYUE_TEST_TMP``，要么是仓库内的 ``.test_tmp``，两者都一定有写权。
sys.path.insert(0, str(ROOT / "tests"))
from _tempdirs import make_db_dir  # noqa: E402

DEFAULT_OUT = ROOT / "backups" / "auto"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_snapshot(src: Path, dest: Path) -> None:
    """Consistent copy even while writers are active (SQLite backup API)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(str(dest))
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()


def _meta_counts(meta_path: Path) -> dict:
    counts: dict[str, int] = {}
    try:
        con = sqlite3.connect(f"file:{meta_path.as_posix()}?mode=ro", uri=True)
        try:
            for table in ("terminal_events", "memory_projections", "analysis_projections"):
                try:
                    counts[table] = int(
                        con.execute(f"select count(*) from {table}").fetchone()[0]
                    )
                except sqlite3.OperationalError:
                    counts[table] = -1
        finally:
            con.close()
    except Exception:
        pass
    return counts


def _copy_tree(src: Path, dest: Path) -> None:
    for item in src.rglob("*"):
        if item.is_dir() or "__pycache__" in item.parts:
            continue
        target = dest / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-chroma", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    meta_db = ROOT / "data" / "yinyue_meta.db"
    account_dbs = sorted((ROOT / "data" / "platforms").glob("*/*/account.db"))

    plan: list[tuple[Path, str]] = [(meta_db, "meta")]
    for db in account_dbs:
        plan.append((db, "account"))
    env_file = ROOT / ".env"
    configs_dir = ROOT / "configs"

    if args.dry_run:
        print(f"[dry-run] out_dir={out_dir}")
        for path, kind in plan:
            print(f"  {kind:8s} {path} ({path.stat().st_size / 1024:.0f} KB)")
        if env_file.exists():
            print(f"  config   {env_file}")
        if configs_dir.exists():
            print(f"  config   {configs_dir}/")
        if args.include_chroma:
            print(f"  chroma   {ROOT / 'data' / 'chroma'}/ (derived)")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = out_dir / f"yinyue_backup_{stamp}.zip"
    staging = make_db_dir("yinyue_backup_")
    files: list[dict] = []
    try:
        for path, kind in plan:
            if not path.exists():
                continue
            rel = (
                Path("meta") / path.name
                if kind == "meta"
                else Path("accounts") / path.parent.name / path.name
            )
            target = staging / rel
            _sqlite_snapshot(path, target)
            files.append(
                {"path": rel.as_posix(), "bytes": target.stat().st_size, "sha256": _sha256(target)}
            )

        if env_file.exists():
            target = staging / "config" / ".env"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(env_file, target)
            files.append(
                {"path": "config/.env", "bytes": target.stat().st_size, "sha256": _sha256(target)}
            )
        if configs_dir.exists():
            _copy_tree(configs_dir, staging / "config" / "configs")
            for item in (staging / "config" / "configs").rglob("*"):
                if item.is_file():
                    files.append(
                        {
                            "path": item.relative_to(staging).as_posix(),
                            "bytes": item.stat().st_size,
                            "sha256": _sha256(item),
                        }
                    )
        if args.include_chroma and (ROOT / "data" / "chroma").exists():
            _copy_tree(ROOT / "data" / "chroma", staging / "chroma")

        manifest = {
            "created_at": datetime.now().isoformat(),
            "root": str(ROOT),
            "counts": _meta_counts(meta_db),
            "files": files,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(staging.rglob("*")):
                if item.is_file():
                    archive.write(item, item.relative_to(staging).as_posix())
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # retention
    kept = sorted(out_dir.glob("yinyue_backup_*.zip"))
    removed = 0
    for old in kept[: max(0, len(kept) - max(1, int(args.keep)))]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(
        f"backup ok: {zip_path.name} ({size_mb:.1f} MB, "
        f"{len(files)} files, counts={manifest['counts']}, removed={removed})"
    )
    if args.notify:
        try:
            from notify.push import send_notification

            send_notification(
                "鲸鲸备份完成",
                f"{zip_path.name}（{size_mb:.1f} MB）counts={manifest['counts']}",
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
