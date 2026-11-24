#!/usr/bin/env python
"""``reminders`` → ``work_items`` 一次性迁移（P1；B2 对照表的消费方）。

默认 **dry-run**：不写任何库，只输出逐字段对照 + 计数 + 预览。``--apply`` 才写，
写序与线上双写完全一致（``core.tasks.store.record_migrated``：``work_items`` 行 +
``work_item_created`` 事件在 meta.db 里**同库单事务**提交，§7.1 ①）。

- 扫描范围：META 库 + 已配置且 ``account.db`` 存在的账号库（谁有 ``reminders``
  表就读谁）；**WorkItem 一律写进 META**——Migration 52 只在 META 建这两张表，
  账号库只盖章；
- 幂等：``(legacy_reminder_id, spec_json.legacy.source_db)``——META 与各账号库的
  ``reminders`` 各自自增，单看 id 会撞；重跑不重复插入；
- 映射口径：``core/tasks/mapping.py`` 的 ``map_reminder_row``
  （未识别的 ``repeat_rule`` → 一次性 + 原文留在 ``spec_json.legacy_repeat_rule``
  + 告警，**不跳过不丢**）；
- ``--json``：**stdout 只有一份 JSON**（structlog / root logger 启动即绑到 stderr，
  见 :func:`bind_logs_to_stderr`；否则 ``--apply`` 的插入日志会把 JSON 弄坏）。

退出码：``0`` 正常（含「0 行可迁」，这是当前生产读数）；``1`` 有行迁移失败；
``2`` 过滤条件没命中任何库（操作失误）；``3`` META 里两张表还不存在
（先让 Migration 52 落地：重启服务，见 ``docs/恢复手册.md``）。

示例：
    python scripts/migrate_reminders_to_work_items.py
    python scripts/migrate_reminders_to_work_items.py --apply
    python scripts/migrate_reminders_to_work_items.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PREVIEW_LIMIT = 5
META_SOURCE = "meta"


def bind_logs_to_stderr() -> None:
    """把 structlog 与 root logger 绑到 **stderr**：stdout 只留给 ``--json``。

    structlog 的默认 ``PrintLogger`` 打 **stdout**，而本脚本不会走
    ``core.infra.logging.init_logging``（那是 scheduler / web 的入口），于是
    ``--apply`` 首次插入时 ``record_migrated`` 的 ``work_item_migrated`` /
    映射告警行会混进 stdout，把 ``--json`` 的输出弄成不可 ``json.load`` 的文本。
    非 JSON 模式不受影响（人读的提示照旧在 stdout）。
    """
    import logging

    import structlog

    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        # 默认配置会缓存首次解析结果；这里显式关掉，避免与调用顺序耦合。
        cache_logger_on_first_use=False,
    )
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    logging.basicConfig(level=logging.INFO, handlers=[console], format="%(message)s")


def _table_names(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)


def scan_sources(account_filter: list[str]) -> list[dict]:
    """要扫的库：META + 已配置且 ``account.db`` 存在的账号库。"""
    from core.config.settings import config

    out = [{
        "key": META_SOURCE,
        "label": "meta",
        "path": Path(config.paths.meta_db_path),
    }]
    for account in config.all_account_configs():
        if account_filter and account.account_id not in account_filter:
            continue
        path = Path(account.db_path)
        if not path.exists():
            continue
        out.append({
            "key": f"{account.platform}/{account.account_id}",
            "label": f"{account.platform}/{account.account_id}",
            "path": path,
        })
    return out


def read_reminders(path: Path) -> tuple[list[dict], str]:
    """只读读 ``reminders``（返回 ``(rows, note)``；没有表/没有文件时 rows 为空）。"""
    if not path.exists():
        return [], "库文件不存在"
    try:
        con = _connect_readonly(path)
    except sqlite3.Error as exc:
        return [], f"只读打开失败：{exc}"
    try:
        con.row_factory = sqlite3.Row
        if "reminders" not in _table_names(con):
            return [], "没有 reminders 表"
        rows = [dict(row) for row in con.execute("SELECT * FROM reminders ORDER BY id")]
        return rows, ""
    except sqlite3.Error as exc:
        raise SystemExit(f"读取 {path} 失败: {exc}") from exc
    finally:
        con.close()


def meta_ledger_ready(path: Path) -> bool:
    """META 里 ``work_items`` / ``work_item_runs`` 是否都在（Migration 52 是否落地）。"""
    if not path.exists():
        return False
    try:
        con = _connect_readonly(path)
    except sqlite3.Error:
        return False
    try:
        tables = _table_names(con)
        return "work_items" in tables and "work_item_runs" in tables
    finally:
        con.close()


def existing_keys(path: Path) -> set[tuple[int, str]]:
    """已投影的 ``(legacy_reminder_id, source_db)``（dry-run 也靠它算「已迁移」）。"""
    if not meta_ledger_ready(path):
        return set()
    con = _connect_readonly(path)
    try:
        out: set[tuple[int, str]] = set()
        for legacy_id, spec_json in con.execute(
            "SELECT legacy_reminder_id, spec_json FROM work_items "
            "WHERE legacy_reminder_id IS NOT NULL"
        ):
            source = META_SOURCE
            try:
                spec = json.loads(str(spec_json or "{}"))
                legacy = spec.get("legacy") if isinstance(spec, dict) else None
                if isinstance(legacy, dict):
                    source = str(legacy.get("source_db") or META_SOURCE)
            except ValueError:
                source = META_SOURCE
            out.add((int(legacy_id), source))
        return out
    finally:
        con.close()


def print_field_table() -> None:
    """逐字段对照（与 ``docs/任务模型.md`` §11.4 同一份口径）。"""
    from core.tasks.mapping import REMINDER_COLUMN_TARGETS

    print("逐字段对照（reminders 18 列 → work_items，docs/任务模型.md §11.4）：")
    for column, target in REMINDER_COLUMN_TARGETS.items():
        print(f"  {column:18s} → {target}")


def preview_row(row: dict) -> dict:
    """一条预览（用真映射算，不碰库）。"""
    from core.tasks.mapping import map_reminder_row

    mapped = map_reminder_row(row)
    due_at = mapped.due_at.isoformat(sep=" ") if mapped.due_at else ""
    return {
        "legacy_reminder_id": row.get("id"),
        "kind": mapped.kind,
        "trigger": mapped.trigger,
        "status": mapped.status,
        "due_at": due_at,
        "priority": mapped.priority,
        "goal": mapped.goal,
        "target": (
            f"{mapped.target_platform}/{mapped.target_bot_account_id}/"
            f"{mapped.target_chat_id}({mapped.target_chat_type})"
        ),
        "repeat_spec": mapped.repeat_spec,
        "warnings": list(mapped.warning_codes),
    }


def print_preview(sample: dict) -> None:
    print(
        "  - #{legacy_reminder_id} {kind}/{trigger}/{status} due={due_at} "
        "priority={priority} goal={goal} → {target} repeat={repeat_spec} "
        "warnings={warnings}".format(
            **{**sample, "warnings": ",".join(sample["warnings"]) or "-"},
        )
    )


def migrate(rows: list[dict], *, engine, source: str, seen: set[tuple[int, str]]) -> dict:
    """``--apply``：逐行写 META（幂等靠 ``record_migrated`` 内部同键查重）。"""
    from core.tasks import store

    result = {"inserted": 0, "skipped": 0, "failed": 0}
    for row in rows:
        legacy_id = row.get("id")
        key = (int(legacy_id), source) if legacy_id is not None else None
        if key is not None and key in seen:
            result["skipped"] += 1
            continue
        try:
            work_item_id = store.record_migrated(engine, row, source_db=source)
        except Exception as exc:  # noqa: BLE001 - 逐行记账，不中断整批
            result["failed"] += 1
            print(f"    [failed] legacy_reminder_id={legacy_id}: {exc}", file=sys.stderr)
            continue
        if work_item_id and key is not None:
            seen.add(key)
        result["inserted"] += 1 if work_item_id else 0
    return result


def emit_json(payload: dict) -> None:
    """``--json`` 的唯一 stdout 出口（提示一律走 stderr）。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    # 第一件事：日志改道 stderr。任何 import / 写库日志都不能进 stdout（P1-a）。
    bind_logs_to_stderr()

    parser = argparse.ArgumentParser(description="reminders → work_items 迁移（P1）")
    parser.add_argument("--account", action="append", default=[], help="只看某个账号（可多次）")
    parser.add_argument("--apply", action="store_true", help="真的写库（默认 dry-run）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（stdout 只有 JSON）")
    parser.add_argument("--no-fields", action="store_true", help="不打印逐字段对照")
    args = parser.parse_args()

    from core.tasks.mapping import REMINDER_COLUMN_TARGETS, REMINDER_COLUMNS

    sources = scan_sources(args.account)
    if not sources:
        print("没有命中任何库（--account 过滤写错了？）", file=sys.stderr)
        if args.json:
            emit_json({"sources": [], "hint": "no_sources"})
        return 2

    meta_path = sources[0]["path"]
    ready = meta_ledger_ready(meta_path)
    if args.apply and not ready:
        print(
            f"META 里还没有 work_items / work_item_runs：先让 Migration 52 落地"
            f"（重启服务），再跑 --apply。库：{meta_path}",
            file=sys.stderr,
        )
        if args.json:
            emit_json({"sources": [], "hint": "ledger_missing", "meta": str(meta_path)})
        return 3

    seen = existing_keys(meta_path) if ready else set()
    engine = None
    if args.apply:
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{meta_path}")

    if not args.json:
        print("reminders → work_items / work_item_runs 迁移（P1）")
        print(f"模式：{'APPLY' if args.apply else 'DRY-RUN（不写库；--apply 才写）'}")
        if not args.no_fields:
            print_field_table()
        if not ready:
            print("[note] META 里还没有这两张表（Migration 52 未落地）："
                  "「已迁移」计数不可用，--apply 会被拒绝。")

    reports = []
    previews: list[dict] = []
    total = {"read": 0, "migratable": 0, "already": 0, "inserted": 0, "failed": 0}
    exit_code = 0
    try:
        for source in sources:
            rows, note = read_reminders(source["path"])
            report = {
                "source": source["key"],
                "path": str(source["path"]),
                "columns": sorted(rows[0].keys()) if rows else list(REMINDER_COLUMNS),
                "read": len(rows),
                "migratable": 0,
                "already": 0,
                "skipped": 0,
                "inserted": 0,
                "failed": 0,
                "note": note,
            }
            if rows:
                pending = []
                for row in rows:
                    key = (int(row["id"]), source["key"]) if row.get("id") is not None else None
                    if key is not None and key in seen:
                        report["already"] += 1
                        continue
                    pending.append(row)
                    if len(previews) < PREVIEW_LIMIT:
                        previews.append({"source": source["key"], **preview_row(row)})
                report["migratable"] = len(pending)
                if args.apply and pending:
                    result = migrate(
                        pending, engine=engine, source=source["key"], seen=seen
                    )
                    report.update(result)
                    if result["failed"]:
                        exit_code = 1
            total["read"] += report["read"]
            total["migratable"] += report["migratable"]
            total["already"] += report["already"]
            total["inserted"] += report["inserted"]
            total["failed"] += report["failed"]
            reports.append(report)
            if not args.json:
                tail = f"（{note}）" if note else ""
                print(
                    f"[{source['key']}] 读 {report['read']} | 可迁 {report['migratable']} | "
                    f"已迁移 {report['already']} | 写入 {report['inserted']} | "
                    f"失败 {report['failed']}{tail}"
                )
    finally:
        if engine is not None:
            engine.dispose()

    if args.json:
        emit_json({
            "mode": "apply" if args.apply else "dry-run",
            "meta": str(meta_path),
            "ledger_ready": ready,
            "field_targets": dict(REMINDER_COLUMN_TARGETS),
            "sources": reports,
            "previews": previews,
            "totals": total,
        })
    else:
        if previews:
            print(f"预览（前 {len(previews)} 条）：")
            for sample in previews:
                print(f"  [{sample['source']}]", end="")
                print_preview(sample)
        print(
            f"合计: 读 {total['read']} | 可迁 {total['migratable']} | "
            f"已迁移 {total['already']} | 写入 {total['inserted']} | "
            f"失败 {total['failed']}"
        )
        if not args.apply:
            print("DRY-RUN：没有写任何库。要落库加 --apply（建议先重启服务让 Migration 52 生效）。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
