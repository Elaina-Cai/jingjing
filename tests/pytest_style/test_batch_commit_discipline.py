"""一次提交还是分批提交：判据是「这个东西崩了以后，已经落库的部分算不算完成品」。

一次性迁移（`scripts/backfill_*` / `scripts/migrate_*` / `scripts/reimport_*`）：崩在中途 = 半个迁移，
没有任何人会知道它改到哪。所以它们只能「算完 → 一次提交」，中途退出等于什么都没改。
长期运行的增量处理（解密、同步、发件箱确认）不一样：每一批本身就是自洽的完成品，中断后重跑会跳过
已处理的部分——这类允许在循环里提交，但必须在这里登记理由。
"""
from __future__ import annotations

import ast
import os

# 读源码一律用 utf-8-sig：本仓有文件带 BOM（`\ufeff# -*- coding: utf-8 -*-`），
# Python 自己编译没问题，ast.parse 会抛 SyntaxError: invalid non-printable character U+FEFF。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 允许在循环里提交的：每一批都是自洽完成品，重跑幂等。
LOOP_COMMIT_ALLOWED = {
    "db/decrypt.py": "按表解密：每张表是一个独立完成品，崩溃后重跑跳过已解密表",
    "db/sync_service.py": "按会话/消息增量入库：每批自洽，中断即下次继续",
    # 2026-11-09 移出 loop/desk.py：发件箱已改逐条 settle，commit 在循环外，不再算批量提交。
    "agents/late_vision_followup.py": "补发前先落标记再发：每条自洽，宁可少补一次也不重复打扰",
}

SCAN_DIRS = ("scripts", "db", "core", "loop", "console", "scheduler", "agents")
MIGRATION_PREFIXES = ("backfill_", "migrate_", "reimport_")


def _files():
    for sub in SCAN_DIRS:
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                yield f"{sub}/{name}"


def _tree(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8-sig", errors="replace") as fh:
        return ast.parse(fh.read())


def loop_commits(rel):
    """提交发生在 for/while 里面（含嵌套函数体）的行号。"""
    lines = []
    for node in ast.walk(_tree(rel)):
        if isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    nm = getattr(sub.func, "attr", getattr(sub.func, "id", ""))
                    if nm == "commit":
                        lines.append(sub.lineno)
    return sorted(set(lines))


def counts(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8-sig", errors="replace") as fh:
        text = fh.read()
    return text.count(".commit("), text.count(".close(")


def migration_scripts():
    return [rel for rel in _files()
            if rel.startswith("scripts/")
            and os.path.basename(rel).startswith(MIGRATION_PREFIXES)]


def test_runtime_loop_commits_match_the_ledger():
    found = {rel: loop_commits(rel) for rel in _files()}
    found = {k: v for k, v in found.items() if v}
    assert set(found) == set(LOOP_COMMIT_ALLOWED), (
        "循环里提交的文件与登记不一致。\n"
        f"未登记（新增的批量提交，先问「崩了以后已落库的部分算完成品吗」）：\n"
        + "\n".join(f"  {k}: 行 {v}" for k, v in found.items() if k not in LOOP_COMMIT_ALLOWED)
        + "\n登记了但已经不这样做了（把条目删掉）：\n"
        + "\n".join(f"  {k}" for k in LOOP_COMMIT_ALLOWED if k not in found)
    )


def test_ledger_entries_explain_why():
    for rel, why in LOOP_COMMIT_ALLOWED.items():
        assert len(why) >= 10, f"{rel} 的理由太短，写清「为什么每一批都是完成品」"


def test_migrations_never_commit_inside_a_loop():
    bad = {rel: loop_commits(rel) for rel in migration_scripts() if loop_commits(rel)}
    assert not bad, "一次性迁移不许在循环里提交（崩在中途＝半个迁移）：\n" + "\n".join(
        f"  {k}: 行 {v}" for k, v in bad.items())


def test_migrations_commit_at_most_once_per_connection():
    bad = []
    for rel in migration_scripts():
        commits, closes = counts(rel)
        if commits > max(closes, 1):
            bad.append(f"  {rel}: commit {commits} 次 / close {closes} 次")
    assert not bad, (
        "迁移脚本每个连接只许提交一次（一次连接＝一个事务单元）：\n" + "\n".join(bad))


def test_scan_is_not_vacuous():
    scripts = migration_scripts()
    assert len(scripts) >= 15, f"迁移脚本只找到 {len(scripts)} 个，扫描范围不对"
    total = sum(counts(rel)[0] for rel in _files())
    assert total >= 10, f"全仓只数到 {total} 个提交点，扫描范围不对"
    assert len(LOOP_COMMIT_ALLOWED) == 3, "登记表变了就更新这条断言，别让它变成不检查任何东西"
