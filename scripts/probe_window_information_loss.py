# -*- coding: utf-8 -*-
r"""探针：她实际读到的窗口 vs 库里真实发生过的事（只读，不改任何状态）。

问题：core/routing/turn_pipeline.py 的五个串行 await 每步只吃上一步的摘要，
      而第一步读到的 chat_window 本身也是降级渲染（最近 40 条原文 up to 2400 字，
      更早的逐条骨架 20/12 字，再早的每天一行，总额 3600 字）。
本探针量化「信息在哪一步丢了多少」：原始事件 -> 窗口渲染 的存活率与压缩比。

只读路径：全程走 db.write_guard.read_only_engine（不经过写警卫，也不写任何表）。

用法：
    .\.venv_it\Scripts\python.exe -X utf8 scripts\probe_window_information_loss.py
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TYPES = ("message", "question", "reply", "request")


def _engine():
    from core.bootstrap import get_meta_db_path
    from db.write_guard import read_only_engine

    return read_only_engine(get_meta_db_path())


def flat(text: object) -> str:
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = re.sub(r"\[(图片|表情包|视频|语音|文件|位置)[^\]]*\]", " ", t)
    t = re.sub(r"@\S+", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def anchor(content: object) -> str:
    t = flat(content)
    if not t:
        return ""
    return t if len(t) <= 14 else t[-12:]


def load_rows(engine, chat_id: str, days: int, now: datetime) -> list[dict]:
    from sqlalchemy import text as sql

    since = now - timedelta(days=max(1, int(days)))
    with engine.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                sql(
                    "select event_id, occurred_at, event_type, actor_person_id, content "
                    "from terminal_events where chat_id = :cid and occurred_at >= :since "
                    "and event_type in ('message','question','reply','request') "
                    "order by occurred_at, sequence"
                ),
                {"cid": str(chat_id), "since": since.strftime("%Y-%m-%d %H:%M:%S")},
            ).mappings()
        ]
        names: dict[str, str] = {}
        for r in conn.execute(
            sql(
                "select pi.identity_value, pe.display_name from person_identities pi "
                "join person_entities pe on pe.person_id = pi.person_id "
                "where pe.display_name is not null and pe.display_name != ''"
            )
        ).mappings():
            names.setdefault(str(r["identity_value"]), str(r["display_name"]))
    return rows, names


def build(engine, chat_id: str, days: int, now: datetime):
    from core.context.chat_window import (
        _dedupe_self_echoes,
        build_window_parts,
        messages_from_rows,
        window_budget,
        window_full,
    )

    rows, names = load_rows(engine, chat_id, days, now)
    for row in rows:
        if isinstance(row.get("occurred_at"), str):
            row["occurred_at"] = datetime.fromisoformat(row["occurred_at"])
    msgs = _dedupe_self_echoes(messages_from_rows(rows, names))
    parts = build_window_parts(chat_id, messages=msgs, days=days, now=now)
    return rows, parts, window_full(), window_budget()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", action="append", default=[])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dump", default="", help="把主会话窗口原文写到这个文件")
    args = ap.parse_args()

    logging.disable(logging.INFO)
    now = datetime.now()
    engine = _engine()
    chats = args.chat or [
        "10000000003@chatroom",
        "10000000001@chatroom",
        "10000000002@chatroom",
        "10000000006@chatroom",
        "10000000004@chatroom",
    ]
    try:
        for chat_id in chats:
            raw, parts, full_n, budget = build(engine, chat_id, args.days, now)
            if not raw:
                print("== %s: 窗口期内无事件\n" % chat_id)
                continue
            window = parts.text_full
            fw = flat(window)
            id_set = {str(getattr(m, "event_id", "") or "") for m in parts.recent}

            intact = truncated = dropped = 0
            raw_chars = kept_chars = 0
            for ev in raw:
                text = flat(ev["content"])
                raw_chars += len(text)
                if not text:
                    dropped += 1
                    continue
                an = anchor(text)
                if an in fw:
                    kept_chars += len(text)
                    intact += 1
                    if len(text) > 14 and text[:10] not in fw:
                        truncated += 1
                elif str(ev["event_id"]) in id_set:
                    kept_chars += min(len(text), 12)
                    truncated += 1
                else:
                    dropped += 1

            n = len(raw)
            print("== %s" % chat_id)
            print("   原始事件 %4d 条 / %6d 字（窗口期 %d 天）" % (n, raw_chars, args.days))
            print("   窗口渲染 %4d 字（预算 %d 字），最近原文段 %d 条（full 上限 %d）"
                  % (len(window), budget, len(parts.recent), full_n))
            print("   原文存活 %4d 条（%.0f%%）＝ 整条可见 %d + 被截短 %d ；完全没进窗口 %d"
                  % (intact, 100.0 * intact / max(1, n), intact - truncated, truncated, dropped))
            print("   字节存活 %.1f%%（%d / %d 字）→ 窗口对外部事实的压缩 %.1fx"
                  % (100.0 * kept_chars / max(1, raw_chars), kept_chars, raw_chars,
                     raw_chars / max(1, len(window))))
            if parts.recent:
                print("   最近原文覆盖 %s ~ %s"
                      % (parts.recent[0].occurred_at.strftime("%m-%d %H:%M"),
                         parts.recent[-1].occurred_at.strftime("%m-%d %H:%M")))
            print("   库中最早一条 %s" % str(raw[0]["occurred_at"])[:16])
            print()
            if args.dump and chat_id == chats[0]:
                Path(args.dump).write_text(window, encoding="utf-8")
                print("   [窗口原文已写到 %s]" % args.dump)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())