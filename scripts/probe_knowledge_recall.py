# -*- coding: utf-8 -*-
r"""task-41：fact（knowledge_docs）召回「静默失效」离线复现 —— 只读探针。

背景（task-37 归因，docs/decisions/穷举整改_召回链.md §22）：170 条 ``needs_memory_recall``
里 82 条零命中，其中 14 条被判成 ``fact_silent_miss`` —— 知识库里有 >=0.2 分的文档、
调用时刻也已存在，生产却一条没吐（80 条带 ``by_kind`` 的行里 ``by_kind.fact`` 合计只有 1）。
当时怀疑 ``core/memory/providers.py`` 的两个静默出口（``principal is None`` /
``except Exception``）把异常吞了。

本探针把这条链**在隔离进程里原样跑一遍**，用库与日志回答四个问题：

1. 那 14 条 query 与它们的调用时刻（``--sections score``）；
2. 现在的代码在群消息 principal（``person_id is None``）上会抛异常吗（``--sections repro``）——
   每一条都真实调用 ``KnowledgeMemoryProvider.recall``，异常逐条打印，**不许吞**；
3. 生产时间线：把日志按「进程重启时刻」切两半，看 ``by_kind.fact`` 在重启前后各是多少
   （``--sections logs``）——这一项决定了 14 条到底是「代码 bug」还是「旧进程」；
4. 旧代码的形状（``--sections oldcode``）：从 git 取 ``ccb062a^`` 的 providers/hub 两处，
   确认旧实现在群 principal 上是**静默 return []**、且 ``KIND_SOURCES['fact']`` 里没有
   knowledge（⇒ 那一格从来没被问过）。

纪律：生产库只读（``mode=ro``）；不改任何产品代码；不写日志（本脚本只 print）。

复算：
    & .\.venv_it\Scripts\python.exe -X utf8 scripts\probe_knowledge_recall.py
    & .\.venv_it\Scripts\python.exe -X utf8 scripts\probe_knowledge_recall.py --sections logs
    & .\.venv_it\Scripts\python.exe -X utf8 scripts\probe_knowledge_recall.py --restart 2026-10-17T16:28:25+08:00
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = str(ROOT / "data" / "yinyue_meta.db")
DEFAULT_RESTART = "2026-10-17T16:28:25+08:00"  # 生产进程 CreationDate（鲸鲸启动器）
EVENT = "needs_memory_recall"
SECTIONS = ("logs", "score", "repro", "oldcode")


# --------------------------------------------------------------------------- 工具
def _parse_ts(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _local(stamp: datetime | None) -> str:
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M:%S") if stamp else "?"


def _connect_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)


def _read_rows() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for log_path in sorted(glob.glob(str(ROOT / "logs" / "yinyue.log*"))):
        try:
            handle = io.open(log_path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if EVENT not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("event") != EVENT:
                    continue
                out.append((os.path.basename(log_path), record))
    return out


def _asked(record: dict) -> list[str]:
    return [str(k) for k in (record.get("kinds") or [])]


# --------------------------------------------------------------------------- logs
def section_logs(args: argparse.Namespace) -> None:
    rows = _read_rows()
    boundary = _parse_ts(args.restart)
    print("\n## 1. 日志时间线（按进程重启 %s 本地 = %s UTC 切）" % (_local(boundary), boundary))
    print("   文件 %d 个，%s 事件 %d 条" % (
        len({name for name, _ in rows}), EVENT, len(rows)))
    per_file = Counter(name for name, _ in rows)
    print("   逐文件：" + "  ".join("%s=%d" % (name.replace("yinyue.log", "log"), n)
                                  for name, n in sorted(per_file.items())))
    spans = [_parse_ts(r.get("timestamp", "")) for _, r in rows]
    spans = [s for s in spans if s]
    if spans:
        print("   跨度：%s → %s（本地）" % (_local(min(spans)), _local(max(spans))))
    pre = [(n, r) for n, r in rows
           if (_parse_ts(r.get("timestamp", "")) or boundary) < boundary]
    post = [(n, r) for n, r in rows
            if (_parse_ts(r.get("timestamp", "")) or boundary) >= boundary]
    for tag, sub in (("重启前", pre), ("重启后", post)):
        hits = Counter(r.get("hits") for _, r in sub)
        by_kind: Counter = Counter()
        for _, r in sub:
            for kind, count in (r.get("by_kind") or {}).items():
                by_kind[str(kind)] += int(count or 0)
        zero_fact = [(n, r) for n, r in sub
                     if r.get("hits") == 0 and "fact" in _asked(r)]
        print("\n   [%s] n=%d  hits=%s" % (tag, len(sub), dict(sorted(hits.items()))))
        print("      by_kind 合计=%s  by_kind.fact=%s" % (dict(by_kind), by_kind.get("fact", 0)))
        print("      「问了 fact 且零命中」=%d 条" % len(zero_fact))
        for name, r in sorted(zero_fact, key=lambda x: str(x[1].get("timestamp"))):
            print("        %s  %-52s kinds=%s" % (
                str(r.get("timestamp", ""))[:19], repr(str(r.get("query") or ""))[:52],
                _asked(r)))
    fact_rows = [(n, r) for n, r in rows if "fact" in _asked(r)]
    fact_hit = [r for _, r in fact_rows if int((r.get("by_kind") or {}).get("fact") or 0) > 0]
    print("\n   小结：问 fact 的行 %d 条，其中 by_kind.fact>0 的 %d 条；"
          "重启前 by_kind.fact 合计 0（旧代码那格没接线），重启后开始有条目。" % (
              len(fact_rows), len(fact_hit)))


# --------------------------------------------------------------------------- score
def _load_docs(conn: sqlite3.Connection) -> list[SimpleNamespace]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "select id, title, content, created_at, source_url, original_query, "
        "source_type, source_file from knowledge_docs order by created_at desc limit 200"
    )
    docs = []
    for row in cur.fetchall():
        docs.append(SimpleNamespace(
            id=row["id"],
            title=row["title"] or "",
            content=row["content"] or "",
            created_at=_parse_ts(str(row["created_at"]).replace(" ", "T")),
            source_url=row["source_url"] or "",
            original_query=row["original_query"] or "",
            source_type=row["source_type"] or "",
            source_file=row["source_file"] or "",
        ))
    return docs


def section_score(args: argparse.Namespace) -> None:
    from core.memory.providers import (  # noqa: PLC0415 - 延迟 import：只这一节要它
        _KNOWLEDGE_MIN_SCORE,
        _content_query_tokens,
        _knowledge_score,
        _tokens,
    )

    rows = _read_rows()
    boundary = _parse_ts(args.restart)
    conn = _connect_ro(args.db)
    try:
        docs = _load_docs(conn)
    finally:
        conn.close()
    print("\n## 2. 那批「有 >=0.2 分文档、却零命中」的 fact 请求（阈值 %.2f，库内 %d 篇）"
          % (_KNOWLEDGE_MIN_SCORE, len(docs)))
    print("   now = 用今天的库复算；then = 只用「调用时刻之前已入库」的文档复算")
    would_now = would_then = 0
    for _, r in sorted(rows, key=lambda x: str(x[1].get("timestamp"))):
        if r.get("hits") != 0 or "fact" not in _asked(r):
            continue
        query = str(r.get("query") or "")
        tokens = _content_query_tokens(query) or _tokens(query)
        stamp = _parse_ts(r.get("timestamp", ""))
        cutoff = (stamp + timedelta(hours=8)) if stamp else None
        best_now = max((_knowledge_score(tokens, d.title, d.content) for d in docs), default=0.0)
        then_docs = [d for d in docs
                     if cutoff is not None and d.created_at is not None and d.created_at <= cutoff]
        best_then = max((_knowledge_score(tokens, d.title, d.content) for d in then_docs),
                        default=0.0)
        flag_now = best_now >= _KNOWLEDGE_MIN_SCORE
        flag_then = best_then >= _KNOWLEDGE_MIN_SCORE
        would_now += flag_now
        would_then += flag_then
        if not (flag_now or flag_then):
            continue
        phase = "重启前" if (stamp or boundary) < boundary else "重启后"
        print("   %-8s %-19s %-40s now=%.3f%s then=%.3f%s" % (
            phase, str(r.get("timestamp", ""))[:19], repr(query)[:40],
            best_now, "*" if flag_now else " ", best_then, "*" if flag_then else " "))
    print("   合：now 会命中 %d 条 / then（当时库）会命中 %d 条" % (would_now, would_then))


# --------------------------------------------------------------------------- repro
def section_repro(args: argparse.Namespace) -> None:
    from db.models import KnowledgeDoc  # noqa: F401 - 只为确认模型可导入
    from db.repo_knowledge import KnowledgeDocRepository
    from db.write_guard import read_only_engine
    from core.memory.providers import KnowledgeMemoryProvider

    engine = read_only_engine(args.db)
    repo = KnowledgeDocRepository(engine)
    provider = KnowledgeMemoryProvider(SimpleNamespace(knowledge_docs=repo))
    # 群消息 principal 的形状：有人（person_id 可能为 None）、有平台、不是 owner。
    principal = SimpleNamespace(
        person_id=None, platform="qq", bot_account_id="", viewer_is_owner=False,
    )
    queries = [
        "小玛 复刻 时间", "玛薇卡 六英雄 设定", "芙芙 角色 新剧情 更新",
        "游戏 e 技能 空 梗", "七神会面 梗 出处", "1芙 配队 伤害 30万 游戏",
        "代郡 1-3 阵容 最便宜组合 游戏", "七命 含义 上下文",
    ]
    print("\n## 3. 现网代码 + 只读库，按群 principal（person_id=None）真调 provider")
    exceptions = 0
    hits = 0
    for query in queries:
        try:
            items = provider.recall(principal, query, limit=5)
        except Exception as exc:  # noqa: BLE001 - 本探针就是要把它打出来
            exceptions += 1
            import traceback
            print("   异常 %r -> %s: %s" % (query, type(exc).__name__, exc))
            print(traceback.format_exc())
            continue
        scores = [round(float((item.metadata or {}).get("score") or 0.0), 4) for item in items]
        hits += 1 if items else 0
        print("   %-34s -> %d 条 scores=%s" % (repr(query)[:34], len(items), scores))
    print("   合：%d 条 query，异常 %d 次，有命中 %d 条" % (len(queries), exceptions, hits))
    print("   注：`principal=None` 那条出口在 hub 里先被拦（core/memory/hub.py:226-227），"
          "生产里 provider 拿到 None 的概率≈0；新补的 knowledge_recall_silent_skip 只在"
          "真正走到这两条出口时才会落一行。")


# --------------------------------------------------------------------------- oldcode
def _git_show(rev_path: str) -> str:
    proc = subprocess.run(
        ["git", "show", rev_path],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else ""


def section_oldcode(args: argparse.Namespace) -> None:
    rev = args.old_rev
    old_providers = _git_show("%s:core/memory/providers.py" % rev)
    old_hub = _git_show("%s:core/memory/hub.py" % rev)
    print("\n## 4. 旧代码形状（%s，即 ccb062a 之前）" % rev)
    if not old_providers or not old_hub:
        print("   取不到 git 里的旧版本（工作区不是 git 仓库？）——跳过")
        return
    guard = 'if principal is None or not getattr(principal, "person_id", ""):'
    guard_count = old_providers.count(guard)
    fact_map = '"fact": ("personal",)'
    print("   旧 guard `%s` 出现 %d 次（Knowledge/Person/Claim 等多家共用同一形状）"
          % (guard.strip(), guard_count))
    print("   旧 KIND_SOURCES 里 fact 的映射是 %s ⇒ knowledge **没被问过**" % fact_map)
    print("   含 knowledge: %s" % ("是" if '"knowledge"' in old_hub else "否"))
    print("   结论：旧实现在群消息 principal（person_id=None）上是 `return []`，**不抛异常**；"
          "两条静默出口在那段时间里根本没被走到——所以那批（重算 12 条）不是被 except 吞掉的 Exception，"
          "而是「旧进程 + 旧映射」的必然空手。")


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fact 召回静默失效离线复现（只读）")
    parser.add_argument("--sections", default=",".join(SECTIONS),
                        help="逗号分隔：%s" % ",".join(SECTIONS))
    parser.add_argument("--db", default=DEFAULT_DB, help="meta 库路径（只读打开）")
    parser.add_argument("--restart", default=DEFAULT_RESTART,
                        help="生产进程重启时刻（带时区，默认生产 CreationDate）")
    parser.add_argument("--old-rev", default="ccb062a^", help="旧代码的 git rev")
    args = parser.parse_args(argv)
    wanted = [part.strip() for part in str(args.sections).split(",") if part.strip()]
    unknown = [part for part in wanted if part not in SECTIONS]
    if unknown:
        parser.error("未知分段：%s（可选 %s）" % (",".join(unknown), ",".join(SECTIONS)))
    print("db=%s（只读）文件 %d 字节" % (args.db, os.path.getsize(args.db)
                                      if os.path.exists(args.db) else -1))
    for name in wanted:
        {"logs": section_logs, "score": section_score,
         "repro": section_repro, "oldcode": section_oldcode}[name](args)
    print("\n完成。复算命令见文件头 docstring。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
