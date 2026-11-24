r"""task-49：把历史 ``terminal_events.actor_person_id`` 从平台号回填成 person id。

背景：写侧归一只影响**新**事件（``core/terminal/event_store_db.py::_event_to_values``），
存量 1.6 万行仍是 wxid / QQ uin，所以读侧（visibility / memory_gateway / 名册）看到的
仍是平台号。这个脚本是那次归一的**存量回填**通道。

用法（默认 dry-run，只读）::

    .venv_it\Scripts\python.exe -X utf8 scripts/backfill_actor_person_ids.py --dry-run
    .venv_it\Scripts\python.exe -X utf8 scripts/backfill_actor_person_ids.py --dry-run --off-diff
    .venv_it\Scripts\python.exe -X utf8 scripts/backfill_actor_person_ids.py --apply --yes

口径（与诊断文档一致）：
* **unique**    —— ``(platform, account_id, identity_value)`` 唯一命中 person；
* **account-wide** —— 上面没命中、但 ``(platform, identity_value)`` 唯一命中（同一 wxid 换过 bot 账号）；
* **ambiguous** —— 同一个键对应 >1 个 person（不猜，不动）；
* **none**      —— 没有 identity 行（QQ uin 之类，不动）；
* **placeholder** —— ``unknown`` / ``self`` / ``unresolved:*``（不动）。

--apply 的三条硬约束：
1. 先备份（``backups/yinyue_meta_before_actor_backfill_<ts>.db``）；
2. 只改 unique / account-wide 两类，且 ``WHERE actor_person_id = <原值>``（并发安全）；
3. 幂等：第二次 apply 更新 0 行、库 SHA256 不变。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "yinyue_meta.db"
PLACEHOLDERS = frozenset({"", "unknown", "self", "system", "系统"})
BATCH = 500


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    # 本机 %TEMP% 不可写：sqlite 在排序/游标溢出时开临时文件会报
    # "unable to open database file"（与 merge_person_entities.py 同因）。
    conn.execute("pragma temp_store=memory")
    conn.row_factory = sqlite3.Row
    return conn


def _open_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("pragma temp_store=memory")
    conn.row_factory = sqlite3.Row
    return conn


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _load_map(conn: sqlite3.Connection) -> tuple[dict, dict, dict]:
    by_account: dict[tuple[str, str, str], str | None] = {}
    by_platform: dict[tuple[str, str], str | None] = {}
    display: dict[str, str] = {}
    for platform, account_id, identity_value, person_id, name in conn.execute(
        "select i.platform, i.account_id, i.identity_value, i.person_id, e.display_name "
        "from person_identities i left join person_entities e on e.person_id = i.person_id "
        "where i.identity_type = 'wxid'"
    ):
        person = str(person_id or "").strip()
        value = str(identity_value or "").strip()
        if not person or not value:
            continue
        platform = str(platform or "").strip()
        account_id = str(account_id or "").strip()
        scoped = (platform, account_id, value)
        if scoped in by_account and by_account[scoped] != person:
            by_account[scoped] = None
        else:
            by_account.setdefault(scoped, person)
        wide = (platform, value)
        if wide in by_platform and by_platform[wide] != person:
            by_platform[wide] = None
        else:
            by_platform.setdefault(wide, person)
        if name:
            display.setdefault(person, str(name))
    return by_account, by_platform, display


def _resolve(
    by_account,
    by_platform,
    person_ids,
    platform,
    account_id,
    raw,
    self_to_person: bool = False,
):
    """→ (person_id | None, kind)。kind ∈ unique/account-wide/ambiguous/none/placeholder/
    already-person/self-unique/self-unmapped。"""
    value = str(raw or "").strip()
    if not value:
        return None, "placeholder"
    if value in person_ids:
        return None, "already-person"
    platform = str(platform or "").strip()
    account_id = str(account_id or "").strip()
    if value == "self":
        if not self_to_person:
            return None, "placeholder"
        if account_id:
            person = by_account.get((platform, account_id, account_id))
            if person:
                return person, "self-unique"
            person = by_platform.get((platform, account_id))
            if person:
                return person, "self-unique"
        return None, "self-unmapped"
    if value.startswith("unresolved:") or value.lower() in PLACEHOLDERS:
        return None, "placeholder"
    if account_id:
        key = (platform, account_id, value)
        person = by_account.get(key)
        if person:
            return person, "unique"
        if key in by_account:
            return None, "ambiguous"
    person = by_platform.get((platform, value))
    if person:
        return person, "account-wide"
    if (platform, value) in by_platform:
        return None, "ambiguous"
    return None, "none"


def _rows(conn: sqlite3.Connection, since: str, limit: int) -> list[sqlite3.Row]:
    sql = (
        "select event_id, occurred_at, platform, bot_account_id, actor_person_id, "
        "actor_account_id, event_type, sequence from terminal_events"
    )
    args: list[Any] = []
    if since:
        sql += " where occurred_at >= ?"
        args.append(since)
    sql += " order by sequence"
    if limit:
        sql += " limit ?"
        args.append(limit)
    return list(conn.execute(sql, args))


def _off_diff(rows: Iterable[sqlite3.Row]) -> tuple[int, int, list[str]]:
    """off 档逐字校验：``_event_to_values`` 算出的两列必须等于库里的两列。"""
    import os

    from core.terminal import event_store_db as store

    os.environ["YINYUE_FEATURE_ACTOR_IDENTITY_NORMALIZE"] = "off"
    store.reset_actor_identity_state()
    cases = 0
    diffs = 0
    samples: list[str] = []
    for row in rows:
        event = store._event_from_row(
            SimpleNamespace(
                occurred_at=row["occurred_at"],
                platform=row["platform"],
                bot_account_id=row["bot_account_id"],
                chat_id="",
                chat_type="",
                actor_person_id=row["actor_person_id"],
                actor_account_id=row["actor_account_id"],
                event_type=row["event_type"],
            )
        )
        values = store._event_to_values(event)
        cases += 1
        if (
            values["actor_person_id"] != str(row["actor_person_id"] or "")
            or values["actor_account_id"] != str(row["actor_account_id"] or "")
        ):
            diffs += 1
            if len(samples) < 5:
                samples.append(
                    f"seq={row['sequence']} db=({row['actor_person_id']!r},{row['actor_account_id']!r}) "
                    f"calc=({values['actor_person_id']!r},{values['actor_account_id']!r})"
                )
    return cases, diffs, samples


def _dry_run(
    db: Path, since: str, limit: int, sample_n: int, with_off_diff: bool,
    self_to_person: bool = False,
) -> int:
    conn = _open_ro(db)
    try:
        by_account, by_platform, display = _load_map(conn)
        person_ids = set(display)
        rows = _rows(conn, since, limit)
        total = conn.execute("select count(*) from terminal_events").fetchone()[0]
        print(f"[db] {db}")
        print(f"[map] scoped={len(by_account)} wide={len(by_platform)} persons={len(person_ids)}")
        print(f"[scan] rows={len(rows)} total_in_db={total} since={since or '-'} limit={limit or '-'}")

        counts = Counter()
        per_platform = Counter()
        plans: list[tuple[sqlite3.Row, str, str]] = []
        for row in rows:
            person, kind = _resolve(
                by_account,
                by_platform,
                person_ids,
                row["platform"],
                row["bot_account_id"],
                row["actor_person_id"],
                self_to_person,
            )
            counts[kind] += 1
            per_platform[(str(row["platform"]), str(row["bot_account_id"]))] += 1
            if person:
                account = str(row["actor_account_id"] or "")
                if kind == "self-unique":
                    account = str(row["bot_account_id"] or "") or account
                plans.append((row, person, kind, account))

        print(
            "[dist] unique={} account-wide={} ambiguous={} none={} placeholder={} already-person={} "
            "self-unique={} self-unmapped={}".format(
                counts["unique"],
                counts["account-wide"],
                counts["ambiguous"],
                counts["none"],
                counts["placeholder"],
                counts["already-person"],
                counts["self-unique"],
                counts["self-unmapped"],
            )
        )
        self_rows = counts["self-unique"] + counts["self-unmapped"]
        if self_rows:
            flag = "--self-to-person 已开" if self_to_person else "未开（--self-to-person 才处理）"
            print(f"[self] actor_person_id='self' 行={self_rows}；self→她 person：{flag}")
        for (platform, account), count in per_platform.most_common():
            print(f"[dist:source] {platform}/{account}: {count}")
        print(f"[plan] would_update={len(plans)} rows")
        print(f"[sample] 前 {min(sample_n, len(plans))} 条 before → after（display_name 来自 person_entities）")
        for row, person, kind, account in plans[:sample_n]:
            print(
                f"  seq={row['sequence']:>6} {row['platform']}/{row['bot_account_id']} "
                f"{row['actor_person_id']!r} → {person!r} ({display.get(person, '?')}) [{kind}]"
                + (f" account→{account!r}" if kind == "self-unique" else "")
            )
        empty = sum(1 for row in rows if not str(row["actor_account_id"] or ""))
        print(f"[account] actor_account_id 为空的行={empty}（--fill-account 才会补成平台号）")

        if with_off_diff:
            cases, diffs, samples = _off_diff(rows)
            print(f"[off-diff] cases={cases} diffs={diffs}")
            for line in samples:
                print(f"  diff {line}")
            if diffs:
                print("[off-diff] FAIL：off 档不是逐字旧行为")
                return 1
    finally:
        conn.close()
    return 0


def _apply(
    db: Path, since: str, limit: int, backup_dir: Path, fill_account: bool,
    self_to_person: bool = False,
) -> int:
    before = _sha256(db)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"yinyue_meta_before_actor_backfill_{stamp}.db"
    shutil.copy2(db, backup)
    print(f"[backup] {backup}")
    print(f"[sha256] before={before}")

    updates: list[tuple[str, str, str, str]] = []
    conn = _open_rw(db)
    try:
        by_account, by_platform, display = _load_map(conn)
        person_ids = set(display)
        rows = _rows(conn, since, limit)
        kinds = Counter()
        for row in rows:
            person, kind = _resolve(
                by_account,
                by_platform,
                person_ids,
                row["platform"],
                row["bot_account_id"],
                row["actor_person_id"],
                self_to_person,
            )
            kinds[kind] += 1
            if person:
                account = str(row["actor_account_id"] or "")
                if kind == "self-unique":
                    account = str(row["bot_account_id"] or "") or account
                updates.append(
                    (person, account, row["event_id"], str(row["actor_person_id"] or ""))
                )
        print(f"[plan] update={len(updates)} {dict(kinds)}")

        changed = 0
        started = time.time()
        # 一次性迁移必须一次提交（PIT-051）：中途崩了要么全落、要么全不落。
        # 分批只用来限制单条 SQL 的参数个数，不再是提交边界。
        try:
            for start in range(0, len(updates), BATCH):
                chunk = updates[start : start + BATCH]
                changed += conn.executemany(
                    "update terminal_events set actor_person_id = ?, actor_account_id = ? "
                    "where event_id = ? and actor_person_id = ?",
                    chunk,
                ).rowcount
            if fill_account:
                filled = conn.execute(
                    "update terminal_events set actor_account_id = actor_person_id "
                    "where coalesce(actor_account_id, '') = '' and coalesce(actor_person_id, '') <> ''"
                ).rowcount
                print(f"[fill-account] rows={filled}")
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()
        print(f"[apply] changed={changed} in {time.time() - started:.1f}s")
    finally:
        pass
    after = _sha256(db)
    print(f"[sha256] after={after}")
    if updates:
        print("[sha256] changed (expected when rows were updated)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="task-49 存量 actor_person_id 回填")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since", default="", help="只处理 occurred_at >= 该时刻（'2026-10-14 00:00:00'）")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--off-diff", action="store_true", help="逐行校验 off 档两列与库里逐字相同")
    parser.add_argument("--dry-run", action="store_true", help="默认行为；显式写出来只是让命令自解释")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true", help="与 --apply 同用，确认已看过 dry-run 报表")
    parser.add_argument("--fill-account", action="store_true", help="把空的 actor_account_id 补成原平台号")
    parser.add_argument(
        "--self-to-person",
        action="store_true",
        help="把 actor_person_id='self'（她自己）回填成她的 person id；解析不出保持 self",
    )
    parser.add_argument("--backup-dir", default=str(ROOT / "backups"))
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"[error] 库不存在: {db}")
        return 2
    if args.apply:
        if not args.yes:
            print("[refuse] --apply 需要同时给 --yes（先跑 --dry-run 让 lead 看过报表）")
            return 2
        return _apply(
            db, args.since, args.limit, Path(args.backup_dir), args.fill_account,
            args.self_to_person,
        )
    return _dry_run(
        db, args.since, args.limit, args.sample, args.off_diff, args.self_to_person
    )


if __name__ == "__main__":
    raise SystemExit(main())
