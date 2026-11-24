#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""梗与群术语看板（M3，只读）：卡片 / 提案漏斗 / 学得会率 / 过度解释抽检 / 私聊隔离。

用法：
    python scripts/glossary_board.py                 # 近 14 天
    python scripts/glossary_board.py --days 30
    python scripts/glossary_board.py --chat 10000000001@chatroom
    python scripts/glossary_board.py --json

口径（近似，供人工复核；不写库、不调 LLM）：
  * 卡片 = `person_notes` 群级行（person_key=''）里的「词=释义」行；
  * 学得会率 = 词条批准后再次出现时，是否还在现查同一词条（web_search/acg_lookup）；
  * 过度解释抽检 = 她的 reply 含词条 + 解释性措辞，但前一条入站没人问「什么意思」。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

from core.context.person_notes import is_lexicon_part, lexicon_term  # noqa: E402
from db.models import is_group_chat_id  # noqa: E402

EXPLAIN_MARKERS = ("意思是", "指的是", "的含义", "含义是", "的意思")
ASK_MARKERS = ("什么意思", "啥意思", "什么梗", "啥梗", "是啥", "含义", "什么来头", "怎么来的")
_TERM_RE = re.compile(r"「([^」]{1,20})」")


def _conn() -> sqlite3.Connection:
    from core.config.settings import config

    db = Path(config.paths.meta_db_path)
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _since(days: int) -> str:
    return (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")


def collect_cards(con: sqlite3.Connection, chat: str = "") -> list[dict]:
    sql = (
        "select id, chat_id, content, source, updated_at from person_notes "
        "where person_key='' and active=1"
    )
    params: tuple = ()
    if chat:
        sql += " and chat_id=?"
        params = (chat,)
    cards: list[dict] = []
    for row in con.execute(sql, params):
        for part in str(row[2] or "").replace("\n", "|").split("|"):
            part = part.strip()
            if is_lexicon_part(part):
                cards.append(
                    {
                        "chat_id": str(row[1] or ""),
                        "term": lexicon_term(part),
                        "line": part,
                        "source": str(row[3] or ""),
                        "updated_at": str(row[4] or ""),
                    }
                )
    return cards


def collect_proposals(con: sqlite3.Connection, days: int, chat: str = "") -> list[dict]:
    sql = (
        "select id, status, chat_id, content, created_at, decided_at "
        "from memory_proposals where slot='lexicon' and created_at >= ?"
    )
    params: list = [_since(days)]
    if chat:
        sql += " and chat_id = ?"
        params.append(chat)
    sql += " order by id"
    cols = ("id", "status", "chat_id", "content", "created_at", "decided_at")
    return [dict(zip(cols, row)) for row in con.execute(sql, params)]


def proposal_term(text: str) -> str:
    match = _TERM_RE.search(str(text or ""))
    return match.group(1).strip() if match else ""


def learned_stats(con: sqlite3.Connection, proposals: list[dict], days: int) -> list[dict]:
    out: list[dict] = []
    fallback = _since(days)
    for p in proposals:
        if str(p.get("status")) != "approved":
            continue
        term = proposal_term(p.get("content"))
        if not term:
            continue
        base = str(p.get("decided_at") or "") or fallback
        chat = str(p.get("chat_id") or "")
        appears = con.execute(
            "select count(*) from terminal_events where chat_id=? and occurred_at>=? "
            "and content like ?",
            (chat, base, f"%{term}%"),
        ).fetchone()[0]
        lookups = con.execute(
            "select count(*) from terminal_events where chat_id=? and occurred_at>=? "
            "and event_type='action' and content in ('web_search','acg_lookup') "
            "and action_payload_json like ?",
            (chat, base, f"%{term}%"),
        ).fetchone()[0]
        out.append(
            {
                "term": term,
                "chat_id": chat,
                "appears": appears,
                "lookups": lookups,
                "verdict": "未复现" if appears == 0 else ("还在查" if lookups > 0 else "未再查"),
            }
        )
    return out


def over_explain_samples(con: sqlite3.Connection, cards: list[dict], days: int,
                         limit: int = 10) -> list[dict]:
    by_chat: dict[str, set] = {}
    for c in cards:
        by_chat.setdefault(c["chat_id"], set()).add(c["term"])
    samples: list[dict] = []
    for chat, terms in by_chat.items():
        rows = con.execute(
            "select id, occurred_at, content from terminal_events "
            "where chat_id=? and occurred_at>=? and event_type='reply' order by occurred_at",
            (chat, _since(days)),
        ).fetchall()
        for _rid, at, content in rows:
            text = str(content or "")
            hit = next((t for t in terms if t and t.casefold() in text.casefold()), "")
            if not hit or not any(m in text for m in EXPLAIN_MARKERS):
                continue
            try:
                prev_from = (
                    datetime.fromisoformat(str(at)) - timedelta(seconds=120)
                ).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:  # noqa: BLE001
                prev_from = _since(days)
            prev = con.execute(
                "select content from terminal_events where chat_id=? and occurred_at<? "
                "and occurred_at>=? and event_type in ('message','question','request') "
                "order by occurred_at desc limit 1",
                (chat, at, prev_from),
            ).fetchone()
            prev_text = str(prev[0] or "") if prev else ""
            if any(m in prev_text for m in ASK_MARKERS):
                continue
            samples.append(
                {
                    "chat_id": chat,
                    "at": str(at),
                    "term": hit,
                    "prev": prev_text[:60],
                    "reply": text[:80],
                }
            )
            if len(samples) >= limit:
                return samples
    return samples


def isolation_check(cards: list[dict]) -> list[dict]:
    return [c for c in cards if not is_group_chat_id(c["chat_id"])]


def main() -> int:
    ap = argparse.ArgumentParser(description="梗与群术语看板（只读）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--chat", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    con = _conn()
    try:
        cards = collect_cards(con, args.chat)
        proposals = collect_proposals(con, args.days, args.chat)
        learned = learned_stats(con, proposals, args.days)
        samples = over_explain_samples(con, cards, args.days)
        bad_isolation = isolation_check(cards)
    finally:
        con.close()

    stats = {
        "days": args.days,
        "cards": len(cards),
        "chats_with_cards": len({c["chat_id"] for c in cards}),
        "multi_sense": sum(
            1
            for c in cards
            if sum(1 for x in cards if x["chat_id"] == c["chat_id"] and x["term"] == c["term"]) > 1
        ),
        "proposals": {
            s: sum(1 for p in proposals if p["status"] == s)
            for s in ("pending", "approved", "rejected", "revoked")
        },
        "learned": learned,
        "over_explain_samples": samples,
        "isolation_bad": bad_isolation,
    }

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    print(f"[glossary_board] 近 {args.days} 天 · chat={args.chat or '(全部)'}")
    print(f"卡片：{stats['cards']} 条 / {stats['chats_with_cards']} 个群（多义卡 {stats['multi_sense']} 条）")
    for c in cards:
        print(f"  - [{c['chat_id']}] {c['line']}  (source={c['source']}, {c['updated_at']})")
    pv = stats["proposals"]
    decided = pv["approved"] + pv["rejected"]
    rate = (pv["approved"] / decided * 100) if decided else 0.0
    print(
        f"提案：pending={pv['pending']} approved={pv['approved']} rejected={pv['rejected']}"
        f"（采纳率 {rate:.0f}%，样本 {decided}）"
    )
    print("学得会（批准后）：")
    if not learned:
        print("  （暂无可评估的词条）")
    for item in learned:
        print(
            f"  - [{item['chat_id']}] 「{item['term']}」出现 {item['appears']} 次 · "
            f"后续现查 {item['lookups']} 次 → {item['verdict']}"
        )
    print(f"过度解释抽检：{len(samples)} 条")
    for s in samples:
        print(f"  - [{s['chat_id']}] {s['at']} 「{s['term']}」前句: {s['prev']!r} | 回复: {s['reply']!r}")
    print(
        "隔离检查：OK（词条都在群里）"
        if not bad_isolation
        else f"隔离检查：异常 {bad_isolation}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
