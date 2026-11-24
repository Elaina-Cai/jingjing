#!/usr/bin/env python
"""复读看板（复读治理 P2.5 · 层0）：只读统计她的发言里「复读」的规模。

用法：
  python scripts/repeat_board.py                          # 最近 1 天，全部会话
  python scripts/repeat_board.py --days 2 --chat 10000000001@chatroom
  python scripts/repeat_board.py --gap-minutes 30 --cos 0.45

口径（只读，不写任何东西）：
  * 只统计她的**实质发言**（跳过表情包/图片占位、工具 JSON 与 <4 字噪声）；
  * 按"活跃段"切分（相邻发言间隔 > gap-minutes 即新段）；
  * **强复读对**（主指标）：段内两条发言的 bigram 余弦 >= --cos（默认 0.45）；
    归一化后完全相同且 15 分钟内的（双写回声）不计；
  * **粘连组**（辅助）：段内共享实义 2-gram 的折叠组（人眼扫"换说法的复读"，
    如「供着」这类字面不像、事情一样的），只展示不计数。

用途：改动前后跑同一口径，比较强复读对的规模；粘连组供人工抽查。
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.context.chat_window import _dedupe_self_echoes, messages_from_rows  # noqa: E402
from core.context.room_state import (  # noqa: E402
    _fold_echo_texts,
    _is_media_placeholder,
)

META_DB = ROOT / "data" / "yinyue_meta.db"
_PUNCT = re.compile(r"[\s，。！？；：、（）【】《》…—～·,.!?;:'\"()\[\]<>-]+")


def _norm(text: str) -> str:
    return _PUNCT.sub("", str(text or ""))


def _bigrams(text: str) -> list[str]:
    flat = re.sub(r"\s+", "", str(text or ""))
    return [flat[i : i + 2] for i in range(max(0, len(flat) - 1))] or [flat]


def _cosine(a: str, b: str) -> float:
    ca, cb = Counter(_bigrams(a)), Counter(_bigrams(b))
    keys = set(ca) | set(cb)
    dot = sum(ca.get(key, 0) * cb.get(key, 0) for key in keys)
    na = math.sqrt(sum(v * v for v in ca.values()))
    nb = math.sqrt(sum(v * v for v in cb.values()))
    return dot / (na * nb) if na and nb else 0.0


def _speakable(text: str) -> bool:
    flat = "".join(str(text or "").split())
    if not flat or len(flat) < 4:
        return False
    if _is_media_placeholder(str(text)):
        return False
    if flat.startswith('{"name"') or flat.startswith("{'name'"):
        return False  # 工具调用 JSON（存进 message 的执行记录）
    return True


def _load(chat: str, days: int) -> list:
    since = (datetime.now() - timedelta(days=max(1, int(days)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    con = sqlite3.connect(f"file:{META_DB}?mode=ro", uri=True)
    try:
        sql = (
            "select occurred_at, event_type, actor_person_id, content, metadata_json "
            "from terminal_events where occurred_at >= :since "
            "and event_type in ('message','question','reply','request')"
        )
        params = {"since": since}
        if chat:
            sql += " and chat_id = :chat"
            params["chat"] = chat
        sql += " order by occurred_at, sequence"
        rows = [
            dict(
                zip(
                    ("occurred_at", "event_type", "actor_person_id", "content", "metadata_json"),
                    row,
                )
            )
            for row in con.execute(sql, params)
        ]
    finally:
        con.close()
    return messages_from_rows(rows)


def _sessions(hers: list, gap_minutes: float) -> list[list]:
    gap = timedelta(minutes=float(gap_minutes))
    segs: list[list] = []
    cur: list = []
    for msg in hers:
        if not _speakable(str(msg.content or "")):
            continue
        if cur and msg.occurred_at - cur[-1].occurred_at > gap:
            segs.append(cur)
            cur = []
        cur.append(msg)
    if cur:
        segs.append(cur)
    return segs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", default="", help="只看某个会话（空=全部）")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--gap-minutes", type=float, default=30.0)
    parser.add_argument("--cos", type=float, default=0.45, help="强复读对阈值")
    parser.add_argument("--top", type=int, default=6, help="每段最多列几条")
    args = parser.parse_args()

    msgs = _dedupe_self_echoes(_load(args.chat, args.days))
    segments = _sessions([m for m in msgs if m.is_self], args.gap_minutes)

    total = sum(len(seg) for seg in segments)
    strong_total = 0
    strong_lines: set = set()
    rows_out: list[tuple] = []
    for seg in segments:
        texts = [(str(m.content or ""), m.occurred_at) for m in seg]
        strong: list[tuple[float, str, str, str, str]] = []
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                a, ta = texts[i]
                b, tb = texts[j]
                if _norm(a) == _norm(b) and abs((ta - tb).total_seconds()) <= 900:
                    continue  # 双写/近时同句
                score = _cosine(a, b)
                if score >= args.cos:
                    strong.append(
                        (score, ta.strftime("%H:%M"), tb.strftime("%H:%M"), a, b)
                    )
                    strong_total += 1
                    strong_lines.add(i)
                    strong_lines.add(j)
        if strong:
            rows_out.append((seg, strong))

    print(
        f"[repeat_board] 最近 {args.days} 天 · chat={args.chat or '(全部)'} · "
        f"gap={args.gap_minutes:g}min · cos>={args.cos:g}"
    )
    print(f"  她的实质发言: {total} 条 / {len(segments)} 段")
    rate = (len(strong_lines) / total * 100) if total else 0.0
    print(f"  强复读对: {strong_total} 对；涉及发言 {len(strong_lines)} 条（{rate:.1f}%）")
    for seg, strong in rows_out:
        span = f"{seg[0].occurred_at:%m-%d %H:%M}~{seg[-1].occurred_at:%H:%M}"
        print(f"  ── 段 {span}（{len(seg)} 条，强对 {len(strong)}）")
        for score, t1, t2, a, b in sorted(strong, reverse=True)[: args.top]:
            print(f"     {score:.2f} [{t1}] {' '.join(a.split())[:46]}")
            print(f"            [{t2}] {' '.join(b.split())[:46]}")

    print("  · 粘连组（宽口径，供人工扫；含换说法的复读）")
    for seg in segments:
        groups = _fold_echo_texts([str(m.content or "") for m in seg])
        hot = [(text, count) for text, count in groups if count >= 3]
        if hot:
            print(
                f"     段 {seg[0].occurred_at:%H:%M}~{seg[-1].occurred_at:%H:%M}: "
                + "；".join(f"×{c} {t[:24]}" for t, c in sorted(hot, key=lambda x: -x[1])[:3])
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
