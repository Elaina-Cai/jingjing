# -*- coding: utf-8 -*-
"""话题聚类**不许落库**：``CoarseAssigner`` 退役后的静态守卫。

背景（``docs/架构审计/A-入站与事件.md`` §4.2、``G2-2``）：

    ``core/preprocess/relevance.py:327 CoarseAssigner``（281 行）曾是 ``threads`` /
    ``thread_msg_ref`` 两张表的**第二个写方**（``commit_assignments`` 里 create 新
    thread + 写引用行 + update predictor_snapshot），而它**全仓零调用方**；它做的
    「话题聚类」又与 R-T5「除焦点状态外仍不落库」正面相反。所以整类删除，只保留
    ``ThreadPredictor`` / ``_extract_keywords`` / ``_semantic_text``（有真实消费方）。

本文件守两件事：

* ① ``core/`` 下**不许再有**对 ``threads`` / ``thread_msg_ref`` 的写语句——话题
  聚类的唯一落库口径在 N3 焦点状态（``core/context/threads.py``）落地前保持"不落库"；
* ② 扫描器不许退化成空断言：``agents/`` 里仍有真实写方（当前 ``agents/auto_dream.py``），
  扫描器必须能命中它们；再带一次注入自检。

Run directly: python tests/test_topic_no_persist_guard.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent

#: 不许落库的范围：产品代码 core/（N3 落地时也在这里，届时把 owner 加白名单并写理由）。
_NO_PERSIST_ROOTS = ("core",)
#: 真实写方仍存在的范围（扫描器非空锚点）。
_ANCHOR_ROOTS = ("agents", "web")
_SCAN_ROOTS = _NO_PERSIST_ROOTS + _ANCHOR_ROOTS
_SCAN_SKIP_DIRS = frozenset({"__pycache__", "build", "site-packages", "_scratch"})
#: 仓储对象上的写方法（读方法 ``get_*`` / ``find_*`` / ``list_*`` 不在内）。
_WRITE_METHODS = frozenset({
    "add", "create", "delete", "insert", "remove", "update", "update_status", "upsert",
})
#: 两张表的仓储属性名。
_THREAD_REPOS = frozenset({"threads", "thread_refs"})
#: 原始 SQL 写法里的表名（大写比对）。
_SQL_TABLES = ("THREADS", "THREAD_MSG_REF")


def _sql_write(snippet: str) -> str:
    """这段 SQL 常量是不是在写两张表之一（不是就返回空串）。"""
    sql = snippet.upper()
    for table in _SQL_TABLES:
        for verb in ("INSERT INTO", "UPDATE", "DELETE FROM", "REPLACE INTO"):
            if f"{verb} {table}" in sql:
                return f"sql:{verb} {table}"
    return ""


def _thread_write_sites(source: str, rel: str) -> list[tuple[str, int, str]]:
    """一段源码里对 ``threads`` / ``thread_msg_ref`` 的写语句（AST + 原始 SQL）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if (
                node.func.attr in _WRITE_METHODS
                and isinstance(owner, ast.Attribute)
                and owner.attr in _THREAD_REPOS
            ):
                found.append((rel, node.lineno, f"{owner.attr}.{node.func.attr}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            hit = _sql_write(node.value)
            if hit:
                found.append((rel, node.lineno, hit))
    return found


def _thread_write_sites_in(root: str) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted((ROOT / root).rglob("*.py")):
        if _SCAN_SKIP_DIRS & set(path.parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        found.extend(
            _thread_write_sites(path.read_text(encoding="utf-8-sig"), rel)
        )
    return found


# ------------------------------------------------------------------ ① 不落库
def test_no_topic_clustering_write_in_core() -> None:
    """①``core/`` 下没有对 ``threads`` / ``thread_msg_ref`` 的写语句。"""
    sites = [item for root in _NO_PERSIST_ROOTS for item in _thread_write_sites_in(root)]
    assert not sites, (
        "core/ 下出现了话题簇落库写语句（R-T5：除焦点状态外不落库；"
        f"话题聚类的 owner 是 N3 core/context/threads.py）：{sorted(sites)}"
    )


def test_coarse_assigner_symbols_are_gone() -> None:
    """①′退役的类与它的三个入口在全仓产品代码里都不存在了。"""
    retired = ("CoarseAssigner", "commit_assignments", "assign_with_embedding")
    hits: list[tuple[str, int, str]] = []
    for root in _SCAN_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if _SCAN_SKIP_DIRS & set(path.parts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                name = (
                    node.id if isinstance(node, ast.Name)
                    else node.attr if isinstance(node, ast.Attribute)
                    else node.func.id if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    else ""
                )
                if name in retired:
                    hits.append((rel, node.lineno, name))
    assert not hits, f"CoarseAssigner 一族的符号又出现了：{sorted(hits)}"


# ------------------------------------------------------------------ ② 非空锚点
def test_scanner_still_sees_real_writers() -> None:
    """②``agents/`` / ``web/`` 里仍有真实写方 → 扫描器口径没退化成空断言。"""
    sites = [item for root in _ANCHOR_ROOTS for item in _thread_write_sites_in(root)]
    assert sites, (
        "全仓已经没有 threads/thread_refs 的真实写方了？那本守卫等于空断言——"
        "若确实都退役了，请连同本文件一起删掉（并在报告里写明）"
    )
    files = {rel for rel, _, _ in sites}
    assert "agents/auto_dream.py" in files, (
        f"锚点文件变了（当前预期 agents/auto_dream.py 仍在写）：{sorted(files)}"
    )


def test_scanner_detects_injected_write() -> None:
    """②′注入自检：把 ``commit_assignments`` 那段写库代码塞回来，扫描器必须命中。"""
    source = (
        "def commit(self, msg):\n"
        "    thread = self.repo.threads.create(Thread(chat_id='c'))\n"
        "    self.repo.thread_refs.add(ThreadMsgRef(thread_id=thread.id))\n"
        "    self.repo.threads.update(thread.id, msg_count=1)\n"
    )
    sites = _thread_write_sites(source, "injected.py")
    kinds = {kind for _, _, kind in sites}
    assert kinds == {"threads.create", "thread_refs.add", "threads.update"}, sites
    # 原始 SQL 也扫得到。
    assert _sql_write("INSERT INTO thread_msg_ref (thread_id) VALUES (1)")
    assert _sql_write("UPDATE threads SET status='merged'")
    # 读语句不许误报。
    assert _thread_write_sites(
        "def f(self, msg_id):\n"
        "    refs = self.repo.thread_refs.get_by_msg(msg_id)\n"
        "    rows = self.repo.threads.get_recent_active('c')\n"
        "    return refs, rows\n",
        "reads.py",
    ) == []


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 脚本模式下也要看清是哪条炸了
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
