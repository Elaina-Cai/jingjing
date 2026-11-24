#!/usr/bin/env python
"""对账器 CLI（任务模型 §7.1 ④，2026-09-28）。

扫 account.db 的 messages 与 meta 的 terminal_events，按
(platform, bot_account_id, chat_id, external_msg_id) 找缺口。

- 默认 **dry-run**：只报告，不写任何东西；
- ``--apply``：幂等补写，补完再查库复核（inserted / failed）；
- 另外附带 **sequence 体检**（B6）：报告白名单外的新缺口与重号，**只报告不修**
  （``--no-sequence`` 可关掉）。``--json`` 输出从「数组」变成
  ``{"sequences": …, "accounts": [...]}``。

退出码：``0`` 正常；``1`` 有回归（补写失败 / 白名单外新缺口 / 新重号）；
``2`` **没有命中任何账号**（过滤条件写错是操作失误，不是「一切正常」）。
``--json`` 时 **stdout 只有 JSON**：提示与人类可读输出一律走 stderr。

示例：
    python scripts/reconcile_terminal_events.py
    python scripts/reconcile_terminal_events.py --account wxid_seed02
    python scripts/reconcile_terminal_events.py --account wxid_seed02 --since 2026-09-28 --apply
    python scripts/reconcile_terminal_events.py --no-sequence
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def parse_since(text: str) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return datetime.strptime(text, "%Y-%m-%d")
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError as exc:
        raise SystemExit(f"--since 解析失败: {text!r}（支持 2026-09-28 或 ISO）") from exc


def build_store():
    from core.config.settings import config
    from core.terminal.event_store_db import TerminalEventStore
    from db.meta_repo import MetaRepositoryManager

    meta = MetaRepositoryManager(str(config.paths.meta_db_path))
    return meta, TerminalEventStore(meta.engine)


def build_identity(meta, disabled: bool):
    if disabled:
        return None
    try:
        from core.terminal.adapters import build_identity_service

        # runtime=None：退回 config 里的 owner/授权来源，避免拉起整个 bootstrap。
        return build_identity_service(meta_repo=meta)
    except Exception as exc:  # noqa: BLE001 - 没有 identity 也允许补写
        print(
            f"[warn] identity service 构建失败，将不带 principal 补写：{exc}",
            file=sys.stderr,
        )
        return None


def list_accounts(platform_filter: str, account_filter: list[str]) -> list[tuple[str, str, Path]]:
    from core.config.settings import config

    out: list[tuple[str, str, Path]] = []
    for ac in config.all_account_configs():
        if platform_filter and ac.platform != platform_filter:
            continue
        if account_filter and ac.account_id not in account_filter:
            continue
        db_path = Path(ac.db_path)
        if db_path.exists():
            out.append((ac.platform, ac.account_id, db_path))
    return out


def print_report(report, apply: bool) -> None:
    mode = "APPLY" if apply and report.applied else ("DRY-RUN" if not apply else "APPLY(no-op)")
    print(
        f"[{report.platform}/{report.account_id}] {mode} "
        f"扫描 {report.scanned} | 已配对 {report.matched} | 缺口 {report.missing} | "
        f"补入 {report.inserted} | 失败 {report.failed} | 无msg_id {report.skipped_no_msg_id}"
    )
    if report.by_chat:
        top = sorted(report.by_chat.items(), key=lambda kv: -kv[1])[:8]
        print("  缺口分布: " + ", ".join(f"{chat}={count}" for chat, count in top))
    for sample in report.samples:
        print(
            "  - {chat} msg={mid} {when} [{kind}] {content}".format(
                chat=sample.get("chat_id", ""),
                mid=sample.get("msg_id", ""),
                when=sample.get("created_at", ""),
                kind=sample.get("msg_type_name", ""),
                content=str(sample.get("content", "")).replace("\n", " ")[:40],
            )
        )


def print_sequence_report(report) -> None:
    """sequence 体检（只读）：新缺口 / 重号写在最前面，一眼能看出有没有回归。"""
    status = "OK" if report.ok else "REGRESSION"
    print(
        f"[sequence] {status} 行 {report.rows} | 最大号 {report.max_sequence} | "
        f"新缺口 {report.sequence_gaps} 号 | 白名单存量空缺 "
        f"{report.sequence_whitelisted_gaps} 号 | 重号 {report.sequence_duplicates} 个"
        f"（新增 {report.sequence_new_duplicates}）"
    )
    if report.error:
        print(f"  读取失败: {report.error}")
    for sample in report.sequence_gap_ranges:
        print("  - 新缺口 {start}–{end}（{count} 号）".format(**sample))
    for sample in report.duplicate_samples:
        kind = "存量" if sample.get("whitelisted") else "新增"
        print(
            f"  - {kind}重号 {sample.get('sequence')} ×{sample.get('count')} "
            f"event_ids={','.join(sample.get('event_ids') or [])}"
        )


def emit_json(payload: dict) -> None:
    """``--json`` 的唯一 stdout 出口：提示/人读输出绝不能混进来。"""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="terminal_events 与 account messages 对账器")
    parser.add_argument("--platform", default="", help="只看某个平台（默认全部配置账号）")
    parser.add_argument("--account", action="append", default=[], help="只看某个账号（可多次）")
    parser.add_argument("--chat", default="", help="只看某个会话")
    parser.add_argument("--since", default="", help="只扫该时间之后的消息（2026-09-28 或 ISO）")
    parser.add_argument("--page-size", type=int, default=2000, help="分页大小（默认 2000）")
    parser.add_argument("--max-messages", type=int, default=0, help="最多扫多少条（0=不限）")
    parser.add_argument("--samples", type=int, default=10, help="报告里最多给几个样例")
    parser.add_argument("--apply", action="store_true", help="真的补写（默认只报告）")
    parser.add_argument("--no-identity", action="store_true", help="不构建 identity service")
    parser.add_argument("--no-sequence", action="store_true", help="跳过 sequence 体检")
    parser.add_argument("--json", action="store_true", help="输出 JSON（对象：sequences + accounts）")
    args = parser.parse_args()

    since = parse_since(args.since)
    accounts = list_accounts(args.platform, args.account)
    if not accounts:
        # 过滤条件没命中账号 = 操作失误，退出码 2（旧行为是 0，等于谎报「一切正常」）。
        # 不碰 meta 库：这一路只是「你给的条件不对」，没必要为它跑全表 sequence 体检。
        print(
            "没有找到「配置内且 account.db 已存在」的账号（可用 --platform/--account 过滤）",
            # --json：提示走 stderr，stdout 只留 JSON（人读模式照旧走 stdout）。
            file=sys.stderr if args.json else sys.stdout,
        )
        if args.json:
            emit_json({"sequences": None, "accounts": [], "hint": "no_accounts"})
        return 2

    from core.terminal.reconciler import reconcile_account, reconcile_sequences
    from db.account_repo import AccountRepositoryManager

    meta, store = build_store()
    identity = build_identity(meta, args.no_identity)

    sequence_report = None
    if not args.no_sequence:
        sequence_report = reconcile_sequences(
            store, sample_limit=max(0, int(args.samples))
        )

    results = []
    exit_code = 0
    for platform, account_id, db_path in accounts:
        repo = AccountRepositoryManager(str(db_path), platform, account_id)
        try:
            report = reconcile_account(
                repo=repo,
                platform=platform,
                account_id=account_id,
                store=store,
                identity_service=identity,
                apply=bool(args.apply),
                chat_id=args.chat,
                since=since,
                page_size=args.page_size,
                max_messages=args.max_messages or None,
                sample_limit=max(0, int(args.samples)),
            )
        finally:
            try:
                repo.engine.dispose()
            except Exception:  # noqa: BLE001
                pass
        results.append(report)
        if report.failed:
            exit_code = 1
        if not args.json:
            print_report(report, bool(args.apply))

    if args.json:
        emit_json(
            {
                "sequences": (
                    sequence_report.as_dict() if sequence_report else None
                ),
                "accounts": [r.as_dict() for r in results],
            }
        )
    else:
        if sequence_report is not None:
            print_sequence_report(sequence_report)
        total_scanned = sum(r.scanned for r in results)
        total_matched = sum(r.matched for r in results)
        total_missing = sum(r.missing for r in results)
        total_inserted = sum(r.inserted for r in results)
        total_failed = sum(r.failed for r in results)
        total_skipped = sum(r.skipped_no_msg_id for r in results)
        print(
            f"合计: 扫描 {total_scanned} | 已配对 {total_matched} | 缺口 {total_missing} | "
            f"补入 {total_inserted} | 失败 {total_failed} | 无msg_id {total_skipped}"
        )
    if sequence_report is not None and not sequence_report.ok:
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
