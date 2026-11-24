"""只读：把「这句是不是在跟我说」跑到真库上，出一张基线表。

它不改任何东西、不写任何库，只回答三个问题：
1. 现有 ``loop/ports.py::address_tier`` 认出了多少（@/拍/引/名字）；
2. 新的 ``loop/addressing.py::verdict`` 多认出多少（called/following/questioned）；
3. 每类长什么样（抽样，人工能不能认）。

用法：
    .venv_it\\Scripts\\python.exe scripts\\addressing_report.py --days 200 --samples 10
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loop import addressing  # noqa: E402


class Row:
    """最小行对象：够 address_tier / verdict 用，不依赖 loop.ports。"""

    __slots__ = ("chat_id", "speaker", "text", "raw", "is_self", "at", "quote_user")

    def __init__(self, chat_id, speaker, text, is_self, at, quote_user=""):
        self.chat_id = str(chat_id or "")
        self.speaker = str(speaker or "")
        self.text = str(text or "")
        self.raw = str(text or "")
        self.is_self = int(is_self or 0)
        self.at = str(at or "")
        self.quote_user = str(quote_user or "")


def _dbs() -> list:
    return sorted(glob.glob(str(ROOT / "data" / "platforms" / "*" / "*" / "account.db")))


def _is_group(chat_id: str) -> bool:
    c = str(chat_id or "")
    return c.endswith("@chatroom") or c.startswith("group:")


def _load(db: str, days: int, limit: int) -> dict:
    con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True, timeout=20.0)
    out: dict = {}
    try:
        cur = con.cursor()
        sql = (
            "select chat_id, ifnull(sender_name,''), ifnull(content,''), ifnull(is_self,0), "
            "ifnull(created_at,''), ifnull(quote_reply_user,'') from messages "
            "where is_self in (0,1) "
            "and (chat_id like '%@chatroom' or chat_id like 'group:%') "
            "and (created_at is null or created_at = '' or created_at >= datetime('now','localtime', ?)) "
            "order by chat_id, created_at limit ?"
        )
        for chat_id, who, text, is_self, at, quote in cur.execute(sql, ("-%d days" % int(days), int(limit))):
            out.setdefault(str(chat_id), []).append(Row(chat_id, who, text, is_self, at, quote))
    finally:
        con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--limit", type=int, default=20000, help="每个库最多读多少条")
    ap.add_argument("--samples", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    names = addressing._aliases()
    kinds = {"named": 0, "called": 0, "following": 0, "questioned": 0, "": 0}
    tier_cnt = {"ask": 0, "mention": 0, "": 0}
    per_chat: dict = {}
    samples: dict = {k: [] for k in kinds}
    total_peer = 0

    for db in _dbs():
        for chat_id, rows in _load(db, args.days, args.limit).items():
            if not _is_group(chat_id):
                continue
            seen: list = []
            for r in rows:
                seen.append(r)
                if r.is_self:
                    continue
                total_peer += 1
                # 只看最近 15 条：与真实一拍里视界的口径一致，也避免 O(n²)
                v = addressing.verdict(seen[-15:], names=names)
                kinds[v.kind] = kinds.get(v.kind, 0) + 1
                per_chat.setdefault(chat_id, {"named": 0, "called": 0, "following": 0, "questioned": 0, "": 0})
                per_chat[chat_id][v.kind] = per_chat[chat_id].get(v.kind, 0) + 1
                if len(samples[v.kind]) < args.samples:
                    samples[v.kind].append((chat_id, v.who or r.speaker, (v.sample or r.text)[:60], v.why))
            if rows:
                try:
                    from loop import ports as _p

                    t, n = _p.address_tier(rows, False)
                except Exception as exc:  # noqa: BLE001 - 只为对比，取不到就记空
                    t, n = "?", str(exc)[:40]
                tier_cnt[t] = tier_cnt.get(t, 0) + 1

    out = {
        "names": list(names),
        "group_peer_messages": total_peer,
        "verdict_kinds": kinds,
        "old_address_tier_per_chat": tier_cnt,
        "per_chat": per_chat,
        "samples": samples,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    print("名单 =", list(names))
    print("群入站消息 =", total_peer)
    print("\n新判定器逐条分类：")
    for k in ("named", "called", "following", "questioned", ""):
        pct = (100.0 * kinds[k] / total_peer) if total_peer else 0.0
        print("  %-10s %6d  %5.1f%%" % (k or "(闲聊)", kinds[k], pct))
    print("\n现有 address_tier（每个会话最后那一屏）：", tier_cnt)
    print("\n抽样：")
    for k in ("named", "called", "following", "questioned", ""):
        print(" --- %s ---" % (k or "(闲聊)"))
        for chat, who, text, why in samples[k][:4]:
            print("   [%s] %s: %s   <= %s" % (str(chat)[:20], who[:8], text.replace("\n", " "), why))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
