r"""合并跨群重复的 person_entities（强标识唯一化）。

背景：`db/repo_people.py` 的查重键曾把 `chat_id` 也带上，同一个 wxid 在多个群
被建成多个 person。写侧（`resolve_identity`）已改为强标识全局唯一、`chat_id`
只记「在哪见过」；本脚本处理存量。

默认 dry-run：打印分组、每组 person_id、每张引用表的行数、保留谁、以及合并后
唯一键冲突预告。`--apply` 才写库；写前 `sqlite3.Connection.backup()` 到
`backups/` 并打印路径。

用法::

    .\.venv_it\Scripts\python.exe -X utf8 scripts\merge_person_entities.py
    .\.venv_it\Scripts\python.exe -X utf8 scripts\merge_person_entities.py --apply
    ... --include-placeholders   # 默认只报表 unknown/self 组，不合并

规则（与写侧一致，单一来源 `db/repo_people.py`）：
- 强标识 `(platform, account_id, identity_type, identity_value)` 相同 => 同一人；
- 弱标识 / 占位符（unknown/self/none/null）保持 chat 作用域，禁止按昵称合并；
- 不跨 platform、不跨 account；
- 保留者 = 被引用行数最多（同表多列按行去重取 max），平票取 `created_at` 最早；
- 唯一键冲突：`person_profile_snapshots` 保留信息量最大的一条，
  `person_observation_links` 保留最早一条，其余表默认 **abort**（不写）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.repo_people import PLACEHOLDER_IDENTITY_VALUES, is_strong_identity  # noqa: E402

DEFAULT_DB = ROOT / "data" / "yinyue_meta.db"
BACKUP_DIR = ROOT / "backups"

# 合并后可能撞唯一键的表：冲突处理策略（未列出的表 = abort）
UNIQUE_POLICIES = {
    "person_profile_snapshots": "richest",
    "person_observation_links": "first",
}
DEFAULT_UNIQUE_POLICY = "abort"

# 历史重复强标识组（见 docs/decisions/身份基质收敛.md 第三节）。--verify 逐个核对。
TRACKED_IDENTITIES = (
    ("wxid_seed06", "P6"),
    ("wxid_seed02", "鲸鲸"),
    ("wxid_seed01", "主人"),
    ("wxid_seed16", "忽念"),
)


def _connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """打开连接；临时表放内存。

    某些环境（服务账号 / 受限临时目录）`%TEMP%` 不可写，SQLite 一旦需要临时文件
    就报 `unable to open database file`；合并是个大事务，必须避开这个坑。
    """
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _text_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    out = []
    for col in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
        name, ctype = col[1], (col[2] or "").upper()
        if any(k in ctype for k in ("CHAR", "TEXT", "CLOB")):
            out.append(name)
    return out


def _rows(conn: sqlite3.Connection, table: str) -> tuple[list[str], list[tuple]]:
    cur = conn.execute(f'SELECT rowid, * FROM "{table}"')
    names = [d[0] for d in cur.description]
    return names, cur.fetchall()


def discover_reference_columns(
    conn: sqlite3.Connection, candidate_ids: set[str]
) -> dict[tuple[str, str], dict]:
    """数据驱动：找出值**真的等于**某个 person_id 的列（不靠表名清单）。"""
    found: dict[tuple[str, str], dict] = {}
    ids = sorted(candidate_ids)
    if not ids:
        return found
    marks = ",".join("?" * len(ids))
    for table in _tables(conn):
        for name in _text_columns(conn, table):
            try:
                rows = conn.execute(
                    f'SELECT "{name}", COUNT(*) FROM "{table}" '
                    f'WHERE "{name}" IN ({marks}) GROUP BY "{name}"',
                    ids,
                ).fetchall()
            except sqlite3.Error:
                continue
            if rows:
                found[(table, name)] = {
                    "rows": sum(int(r[1]) for r in rows),
                    "ids": {str(r[0]) for r in rows},
                }
    return found


def discover_json_columns(
    conn: sqlite3.Connection, candidate_ids: set[str]
) -> dict[tuple[str, str], dict]:
    """`*_json` 列里以字符串元素形式出现的 person_id（形如 ["p1","p2"]）。"""
    found: dict[tuple[str, str], dict] = {}
    for table in _tables(conn):
        for name in _text_columns(conn, table):
            if not (name.endswith("_json") or name.endswith("_ids")):
                continue
            hits = 0
            ids: set[str] = set()
            try:
                rows = conn.execute(
                    f'SELECT "{name}" FROM "{table}" WHERE "{name}" LIKE \'%["%\''
                ).fetchall()
            except sqlite3.Error:
                continue
            for (value,) in rows:
                if not isinstance(value, str) or not value.strip().startswith("["):
                    continue
                try:
                    items = json.loads(value)
                except Exception:
                    continue
                if not isinstance(items, list):
                    continue
                matched = {str(x) for x in items if isinstance(x, str) and x in candidate_ids}
                if matched:
                    hits += 1
                    ids |= matched
            if hits:
                found[(table, name)] = {"rows": hits, "ids": ids}
    return found


def build_groups(conn: sqlite3.Connection, include_placeholders: bool):
    entities = {
        str(r[0]): {
            "person_id": str(r[0]),
            "display_name": r[1] or "",
            "status": r[2] or "",
            "created_at": r[3] or "",
        }
        for r in conn.execute(
            "SELECT person_id, display_name, status, created_at FROM person_entities"
        ).fetchall()
    }
    identities = conn.execute(
        "SELECT person_id, platform, account_id, identity_type, identity_value, chat_id "
        "FROM person_identities"
    ).fetchall()

    strong_groups: dict[tuple, list[str]] = {}
    placeholder_groups: dict[tuple, list[str]] = {}
    for person_id, platform, account_id, itype, ivalue, _chat in identities:
        person_id = str(person_id or "")
        if not person_id or person_id not in entities:
            continue
        key = (str(platform or ""), str(account_id or ""), str(itype or ""), str(ivalue or ""))
        if is_strong_identity(key[2], key[3]):
            strong_groups.setdefault(key, []).append(person_id)
        elif str(ivalue or "").strip().lower() in PLACEHOLDER_IDENTITY_VALUES:
            placeholder_groups.setdefault(key, []).append(person_id)

    def _dups(groups, allowed: bool):
        out = []
        for key, pids in sorted(groups.items()):
            unique = sorted(set(pids))
            if len(unique) < 2:
                continue
            out.append({"key": key, "person_ids": unique, "merge": allowed})
        return out

    return entities, _dups(strong_groups, True), _dups(placeholder_groups, include_placeholders)


def person_unique_indexes(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """所有含 person_id 的唯一索引：{table: [{cols, person_col}]}。"""
    out: dict[str, list[dict]] = {}
    for table in _tables(conn):
        if table == "person_entities":
            continue  # 本表就是折叠对象：源行最终整行删除，不算唯一键冲突
        for idx in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
            if not idx[2]:
                continue
            cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{idx[1]}")').fetchall()]
            pcols = [c for c in cols if c and c.endswith("person_id")]
            if not pcols:
                continue
            out.setdefault(table, []).append({"cols": cols, "person_col": pcols[0]})
    return out


def _group_by_unique(
    rows: list[tuple], names: list[str], cols: list[str], person_col: str, mapping: dict[str, str]
) -> dict[tuple, list[tuple]]:
    idx = {n: i for i, n in enumerate(names)}
    groups: dict[tuple, list[tuple]] = {}
    for row in rows:
        rec = {n: row[i] for n, i in idx.items()}
        pid = rec.get(person_col)
        key = tuple(mapping.get(pid, pid) if c == person_col else rec.get(c) for c in cols)
        groups.setdefault(key, []).append(row)
    return groups


def plan_collisions(
    conn: sqlite3.Connection, mapping: dict[str, str], table_indexes: dict[str, list[dict]]
) -> dict[tuple, dict]:
    report: dict[tuple, dict] = {}
    for table, specs in table_indexes.items():
        names, rows = _rows(conn, table)
        for spec in specs:
            groups = _group_by_unique(rows, names, spec["cols"], spec["person_col"], mapping)
            dup_groups = [g for g in groups.values() if len(g) > 1]
            if dup_groups:
                report[(table, tuple(spec["cols"]))] = {
                    "groups": len(dup_groups),
                    "extra": sum(len(g) - 1 for g in dup_groups),
                    "person_col": spec["person_col"],
                }
    return report


def resolve_collision(
    conn: sqlite3.Connection,
    table: str,
    cols: list[str],
    person_col: str,
    mapping: dict[str, str],
    policy: str,
) -> int:
    names, rows = _rows(conn, table)
    idx = {n: i for i, n in enumerate(names)}
    groups = _group_by_unique(rows, names, cols, person_col, mapping)
    deleted = 0
    for items in groups.values():
        if len(items) < 2:
            continue
        if policy == "richest":
            items.sort(
                key=lambda row: (
                    row[idx.get("observation_count", 0)] or 0,
                    row[idx.get("version", 0)] or 0,
                    str(row[idx.get("updated_at", 0)] or ""),
                    row[0],
                )
            )
            keep = items[-1][0]
        else:  # "first"
            keep = min(items, key=lambda row: row[0])[0]
        for row in items:
            if row[0] != keep:
                conn.execute(f'DELETE FROM "{table}" WHERE rowid=?', (row[0],))
                deleted += 1
    return deleted


def run_verify(
    conn: sqlite3.Connection,
    expect_entities: int | None = None,
    expect_self_rows: int | None = None,
) -> int:
    """apply 后的一次性校验（只读）：总数 / 4 组收敛 / 三人各 1 person / actor=self / integrity。

    ``actor_person_id='self'`` 是 task-49 之后要盯的指标：self 是"她自己"，
    ``unknown`` 是"真不知道"，两者语义不同，不能合并统计。``self-unmapped`` 是
    "self 行连她的 person 都定位不到"的子集（运行期计数 ``actor_self_unmapped`` 的
    库侧代理；存量已回填，剩 0 才正常）。
    """
    problems: list[str] = []
    total = conn.execute("SELECT COUNT(*) FROM person_entities").fetchone()[0]
    if expect_entities is not None:
        mark = "OK" if total == expect_entities else "MISMATCH"
        print(f"[verify] person_entities = {total} (expected {expect_entities}) -> {mark}")
        if total != expect_entities:
            problems.append(f"person_entities {total} != expected {expect_entities}")
    else:
        print(f"[verify] person_entities = {total}")

    _entities, groups, _placeholders = build_groups(conn, include_placeholders=False)
    print(f"[verify] duplicate strong-identity groups = {len(groups)} (expected 0) -> "
          f"{'OK' if not groups else 'MISMATCH'}")
    if groups:
        problems.append(f"{len(groups)} duplicate strong-identity group(s) remain")
        for g in groups:
            print(f"         still split: {g['key']} -> {[p[:12] for p in g['person_ids']]}")

    for value, label in TRACKED_IDENTITIES:
        rows = conn.execute(
            "SELECT DISTINCT i.person_id, e.display_name, "
            "(SELECT COUNT(*) FROM person_identities x WHERE x.person_id=i.person_id), "
            "(SELECT COUNT(*) FROM message_evidence m WHERE m.person_id=i.person_id) "
            "FROM person_identities i LEFT JOIN person_entities e ON e.person_id=i.person_id "
            "WHERE i.platform='wechat' AND i.identity_value=? ORDER BY i.person_id",
            (value,),
        ).fetchall()
        if len(rows) == 1:
            pid, name, n_id, n_msg = rows[0]
            print(f"[verify] {label:4s} wxid={value}: 1 person {str(pid)[:12]} "
                  f"({name or '-'}, identities={n_id}, msg_ev={n_msg}) -> OK")
        else:
            print(f"[verify] {label:4s} wxid={value}: {len(rows)} persons -> MISMATCH")
            problems.append(f"{label} spans {len(rows)} persons")

    dup_memberships = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM group_memberships "
        "GROUP BY person_id, platform, account_id, chat_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    print(f"[verify] duplicate (person,chat) memberships = {dup_memberships} (expected 0) -> "
          f"{'OK' if not dup_memberships else 'MISMATCH'}")
    if dup_memberships:
        problems.append(f"{dup_memberships} duplicate membership group(s)")

    self_rows = conn.execute(
        "SELECT COUNT(*) FROM terminal_events WHERE actor_person_id='self'"
    ).fetchone()[0]
    self_unmapped = conn.execute(
        "SELECT COUNT(*) FROM terminal_events e WHERE e.actor_person_id='self' "
        "AND NOT EXISTS (SELECT 1 FROM person_identities i "
        "WHERE i.platform=e.platform AND i.identity_value=COALESCE(e.bot_account_id,''))"
    ).fetchone()[0]
    self_account = conn.execute(
        "SELECT COUNT(*) FROM terminal_events WHERE actor_account_id='self'"
    ).fetchone()[0]
    if expect_self_rows is not None:
        mark = "OK" if self_rows == expect_self_rows else "MISMATCH"
        print(f"[verify] actor_person_id='self' rows = {self_rows} "
              f"(expected {expect_self_rows}) -> {mark}")
        if self_rows != expect_self_rows:
            problems.append(f"actor_person_id='self' rows {self_rows} != {expect_self_rows}")
    else:
        print(f"[verify] actor_person_id='self' rows = {self_rows}")
    print(f"[verify] self-unmapped rows = {self_unmapped} (自解析不出的 self 行)")
    print(f"[verify] actor_account_id='self' rows = {self_account}")

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"[verify] PRAGMA integrity_check = {integrity}")
    if integrity != "ok":
        problems.append(f"integrity_check: {integrity}")

    print("[verify] RESULT:", "PASS" if not problems else "FAIL: " + "; ".join(problems))
    return 0 if not problems else 1

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="合并跨群重复 person_entities（默认 dry-run）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只报表）")
    ap.add_argument("--include-placeholders", action="store_true", help="把 unknown/self 组也合并")
    ap.add_argument("--backup-dir", default=str(BACKUP_DIR), help="备份目录（默认 backups/）")
    ap.add_argument("--verify", action="store_true",
                    help="只读校验（apply 后跑）：总数/4 组/三人唯一/integrity_check")
    ap.add_argument("--expect-entities", type=int, default=None,
                    help="配合 --verify：期望的 person_entities 总数")
    ap.add_argument("--expect-self-rows", type=int, default=None,
                    help="配合 --verify：期望的 actor_person_id='self' 行数（回填后应为 0）")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return 2

    if args.verify:
        vconn = _connect(db_path, readonly=True)
        try:
            return run_verify(vconn, args.expect_entities, args.expect_self_rows)
        finally:
            vconn.close()

    conn = _connect(db_path, readonly=True)
    conn.row_factory = sqlite3.Row
    try:
        entities, groups, placeholder_groups = build_groups(conn, args.include_placeholders)
        candidate_ids: set[str] = set()
        for g in groups:
            candidate_ids |= set(g["person_ids"])
        ref_cols = discover_reference_columns(conn, candidate_ids)
        json_cols = discover_json_columns(conn, candidate_ids)
        # 这两张是主表，由 apply 阶段显式搬迁/去重，不计入「被引用行数」
        ref_cols.pop(("person_entities", "person_id"), None)
        ref_cols.pop(("person_identities", "person_id"), None)

        ids = sorted(candidate_ids)
        marks = ",".join("?" * len(ids))
        direct_counts: dict[tuple[str, str], dict[str, int]] = {}
        for (table, name) in ref_cols:
            for pid, n in conn.execute(
                f'SELECT "{name}", COUNT(*) FROM "{table}" WHERE "{name}" IN ({marks}) '
                f'GROUP BY "{name}"',
                ids,
            ).fetchall():
                direct_counts.setdefault((table, str(pid)), {})[name] = int(n)
        json_counts: dict[tuple[str, str], int] = {}
        for (table, name) in json_cols:
            for (value,) in conn.execute(
                f'SELECT "{name}" FROM "{table}" WHERE "{name}" LIKE \'%["%\''
            ).fetchall():
                if not isinstance(value, str):
                    continue
                try:
                    items = json.loads(value)
                except Exception:
                    continue
                if not isinstance(items, list):
                    continue
                for pid in {str(x) for x in items if isinstance(x, str)} & candidate_ids:
                    json_counts[(table, pid)] = json_counts.get((table, pid), 0) + 1
        rows_by_id: dict[str, int] = {pid: 0 for pid in candidate_ids}
        for (table, pid), cols in direct_counts.items():
            rows_by_id[pid] = rows_by_id.get(pid, 0) + max(cols.values())
        for (table, pid), n in json_counts.items():
            rows_by_id[pid] = rows_by_id.get(pid, 0) + n

        print(f"db: {db_path}")
        print(f"mode: {'APPLY' if args.apply else 'DRY-RUN'}")
        print(f"person_entities: {len(entities)}  person_identities: "
              f"{conn.execute('SELECT COUNT(*) FROM person_identities').fetchone()[0]}")
        print(f"groups(strong, mergeable): {len(groups)}   "
              f"groups(placeholder, reported only): {len(placeholder_groups)}")
        print(f"candidate persons in mergeable groups: {len(candidate_ids)}")
        print("")

        plan = []
        for idx, g in enumerate(groups, 1):
            members = sorted(
                g["person_ids"],
                key=lambda pid: (-rows_by_id.get(pid, 0), entities[pid]["created_at"], pid),
            )
            survivor, sources = members[0], members[1:]
            plan.append({"group": g, "survivor": survivor, "sources": sources})
            platform, account_id, itype, ivalue = g["key"]
            print(f"[G{idx}] ({platform}, {account_id}, {itype}={ivalue}) "
                  f"persons={len(members)} -> keep {survivor[:12]}")
            for pid in members:
                ent = entities[pid]
                per_table = []
                for (table, name) in sorted(ref_cols):
                    n = direct_counts.get((table, pid), {}).get(name, 0)
                    if n:
                        per_table.append(f"{table}.{name}={n}")
                for (table, name) in sorted(json_cols):
                    n = json_counts.get((table, pid), 0)
                    if n:
                        per_table.append(f"{table}.{name}[json]={n}")
                tag = "KEEP" if pid == survivor else "merge"
                name = ent["display_name"] or "-"
                print(f"   - {pid[:12]} {tag:5s} {name[:16]:16s} total={rows_by_id.get(pid, 0):6d} "
                      f"{'; '.join(per_table) if per_table else '(no refs)'}")

        if placeholder_groups:
            print("")
            print(f"[placeholder groups: {len(placeholder_groups)}] 默认只报表，不合并")
            for g in placeholder_groups:
                key = g["key"]
                print(f"   - ({key[0]}, {key[1]}, {key[2]}={key[3]}) persons={len(g['person_ids'])}: "
                      f"{[p[:12] for p in g['person_ids']]}")

        print("")
        print("[tables to rewrite]")
        for (table, name), info in sorted(ref_cols.items()):
            print(f"   {table}.{name}: {info['rows']} rows")
        for (table, name), info in sorted(json_cols.items()):
            print(f"   {table}.{name} [json element]: {info['rows']} rows")

        after = len(entities) - sum(len(p['sources']) for p in plan)
        print("")
        print(f"[summary] groups={len(plan)} merged_entities="
              f"{sum(len(p['sources']) for p in plan)} person_entities {len(entities)} -> {after}")

        mapping: dict[str, str] = {}
        for p in plan:
            for src in p["sources"]:
                mapping[src] = p["survivor"]

        # 唯一键冲突预告（合并会撞 person_profile_snapshots 等唯一索引）
        table_indexes = person_unique_indexes(conn)
        collisions = plan_collisions(conn, mapping, table_indexes)
        abort_tables = []
        if collisions:
            print("")
            print("[unique-key collisions after merge]")
            for (table, cols), info in sorted(collisions.items()):
                policy = UNIQUE_POLICIES.get(table, DEFAULT_UNIQUE_POLICY)
                print(f"   {table} ({', '.join(cols)}): {info['groups']} groups / "
                      f"{info['extra']} rows -> policy={policy}")
                if policy == "abort":
                    abort_tables.append(table)

        if not plan:
            # 幂等：强标识已收敛时，dry-run 与 --apply 都必须 0 组、0 写入、0 备份。
            print("\nnothing to merge: 0 groups. (dry-run and apply both no-op)")
            return 0

        if abort_tables:
            print(f"\nabort: tables with unresolvable unique-key collisions: {sorted(set(abort_tables))}")
            return 3

        if not args.apply:
            print("\ndry-run only. nothing written. re-run with --apply to execute.")
            return 0

        # ---- apply ----
        backup_dir = Path(args.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"yinyue_meta_before_merge_{stamp}.db"
        conn.close()
        ro = _connect(db_path, readonly=True)
        dst = _connect(backup_path)
        with dst:
            ro.backup(dst)
        dst.close()
        ro.close()
        print(f"\nbackup: {backup_path}")

        conn = _connect(db_path)
        try:
            with conn:
                # 1) 先解唯一键冲突，再搬 person_id。顺序不能反：先 UPDATE 会在
                #    person_profile_snapshots 等唯一索引上立刻撞 IntegrityError。
                for (table, cols), _info in collisions.items():
                    policy = UNIQUE_POLICIES.get(table, DEFAULT_UNIQUE_POLICY)
                    person_col = table_indexes[table][0]["person_col"]
                    deleted = resolve_collision(conn, table, list(cols), person_col, mapping, policy)
                    print(f"[resolve] {table}: deleted {deleted} duplicate row(s) (policy={policy})")
                for src_id, target in mapping.items():
                    conn.execute(
                        "UPDATE person_identities SET person_id=? WHERE person_id=?",
                        (target, src_id),
                    )
                for (table, name), _info in ref_cols.items():
                    for src, target in mapping.items():
                        conn.execute(
                            f'UPDATE "{table}" SET "{name}"=? WHERE "{name}"=?', (target, src)
                        )
                for (table, name), _info in json_cols.items():
                    for (rowid, value) in conn.execute(
                        f'SELECT rowid, "{name}" FROM "{table}" WHERE "{name}" LIKE \'%["%\''
                    ).fetchall():
                        try:
                            items = json.loads(value)
                        except Exception:
                            continue
                        if not isinstance(items, list):
                            continue
                        changed = False
                        new_items = []
                        for x in items:
                            if isinstance(x, str) and x in mapping:
                                new_items.append(mapping[x])
                                changed = True
                            else:
                                new_items.append(x)
                        if changed:
                            conn.execute(
                                f'UPDATE "{table}" SET "{name}"=? WHERE rowid=?',
                                (json.dumps(new_items, ensure_ascii=False), rowid),
                            )
                # 防御：唯一键若仍有重复（例如 5 列 scope 键），保留最早一条
                for dup in conn.execute(
                    "SELECT person_id, platform, account_id, identity_type, identity_value, chat_id, "
                    "COUNT(*) c, MIN(id) keep FROM person_identities "
                    "GROUP BY person_id, platform, account_id, identity_type, identity_value, chat_id "
                    "HAVING c > 1"
                ).fetchall():
                    conn.execute(
                        "DELETE FROM person_identities WHERE person_id=? AND platform=? AND account_id=? "
                        "AND identity_type=? AND identity_value=? AND chat_id=? AND id<>?",
                        (dup[0], dup[1], dup[2], dup[3], dup[4], dup[5], dup[7]),
                    )
                for src in mapping:
                    conn.execute("DELETE FROM person_entities WHERE person_id=?", (src,))
            left = conn.execute("SELECT COUNT(*) FROM person_entities").fetchone()[0]
            print(f"[applied] person_entities now {left} (expected {after})")
            print(f"[applied] person_identities now "
                  f"{conn.execute('SELECT COUNT(*) FROM person_identities').fetchone()[0]}")
            print("[applied] integrity_check: "
                  f"{conn.execute('PRAGMA integrity_check').fetchone()[0]}")
            for p_ in plan:
                platform, account_id, itype, ivalue = p_["group"]["key"]
                left_pids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT person_id FROM person_identities "
                        "WHERE platform=? AND account_id=? AND identity_type=? AND identity_value=?",
                        (platform, account_id, itype, ivalue),
                    ).fetchall()
                ]
                status = "OK" if left_pids == [p_["survivor"]] else f"MISMATCH {left_pids}"
                print(f"[verify] {itype}={ivalue}: {len(left_pids)} person -> {status}")
            dup_memberships = conn.execute(
                "SELECT person_id, chat_id, COUNT(*) c FROM group_memberships "
                "GROUP BY person_id, chat_id HAVING c > 1"
            ).fetchall()
            print(f"[verify] duplicate (person,chat) memberships: {len(dup_memberships)}")
        finally:
            conn.close()
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())