#!/usr/bin/env python
"""批2b 收口 · 引用与存量归因体检（只读）。

回答三个问题：
  1. terminal_events 里还有多少行**归因缺失**（说话人解析不出来）？
     按天分布 —— 判断「存量老行」还差多少。
  2. 引用现在是什么形态？有多少条带 [引用 …]、作者能不能拆出来？
     **判定走 canonical**（``core.conversation.addressing.quote_of``）：本脚本
     量的是「线上那套解析认得出多少作者」，所以不能再自带一份引用正则——
     自带一份就等于拿另一个口径去给线上打分（数字对不上还查不出来）。
  3. 窗口 vs buffer 在真实数据上的一致的覆盖面（顺带统计）。

用法：
  python scripts/audit_attribution_quotes.py
  python scripts/audit_attribution_quotes.py --days 7
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.conversation.addressing import quote_of  # noqa: E402

META_DB = ROOT / "data" / "yinyue_meta.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="引用与存量归因体检（只读）")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    if not META_DB.exists():
        print(f"没有找到 {META_DB}")
        return 1

    con = sqlite3.connect(f"file:{META_DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    names: dict[str, str] = {}
    for row in con.execute(
        "select pi.identity_value, pe.display_name from person_identities pi "
        "join person_entities pe on pe.person_id = pi.person_id "
        "where pe.display_name is not null and pe.display_name != ''"
    ):
        names.setdefault(str(row[0]), str(row[1]))
    print(f"身份映射 {len(names)} 条")

    since = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = con.execute(
        "select occurred_at, event_type, actor_person_id, content, metadata_json, chat_type "
        "from terminal_events where occurred_at >= ? order by occurred_at asc",
        (since,),
    ).fetchall()
    con.close()

    total = len(rows)
    unresolved: Counter[str] = Counter()
    unresolved_by_type: Counter[str] = Counter()
    unresolved_by_chat: Counter[str] = Counter()
    quote_total = 0
    quote_attributable = 0
    quote_broken = 0
    chat_type_counts: Counter[str] = Counter()

    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except Exception:
            meta = {}
        event_type = str(row["event_type"] or "")
        actor = str(row["actor_person_id"] or "")
        chat_type = str(row["chat_type"] or "?")
        chat_type_counts[chat_type] += 1

        is_bot = event_type == "reply" or bool(meta.get("is_self"))
        sender_name = str(meta.get("sender_name") or "")
        resolved = bool(is_bot or sender_name or names.get(actor))
        if not resolved:
            day = str(row["occurred_at"])[:10]
            unresolved[day] += 1
            unresolved_by_type[event_type] += 1
            unresolved_by_chat[str(row["metadata_json"] or "")[:0] or actor] += 1

        content = str(row["content"] or "")
        quoted = quote_of(content)
        if quoted.present:
            quote_total += 1
            if quoted.resolvable:
                quote_attributable += 1
            else:
                quote_broken += 1

    print()
    print(f"窗口 {args.days} 天，事件 {total} 条")
    print(f"  按会话类型：{dict(chat_type_counts)}")
    print()
    print("── 归因 ──")
    un_total = sum(unresolved.values())
    print(f"  解析不出说话人：{un_total} 条"
          f"（{un_total / total * 100:.2f}%）" if total else "  无事件")
    if unresolved:
        print(f"  按事件类型：{dict(unresolved_by_type)}")
        print("  按天（最近 10 天）：")
        for day in sorted(unresolved)[-10:]:
            print(f"    {day}  {unresolved[day]} 条")
    print()
    print("── 引用 ──")
    print(f"  带 [引用 …] 的内容：{quote_total} 条")
    if quote_total:
        print(f"    能拆出作者：{quote_attributable}"
              f"（{quote_attributable / quote_total * 100:.1f}%）")
        print(f"    拆不出作者：{quote_broken}"
              f"（{quote_broken / quote_total * 100:.1f}%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
