#!/usr/bin/env python
"""社交代价看板（P3.1 · 先看板不拦）：log_only 命中记录 + 离线校准。

在线模式（默认）：读 outbound_audit 里 decision='log_only' 的记录——
规则以 log_only 上线后，命中**不会拦消息**，只在这里留痕。
    python scripts/cost_gate_board.py [--days 3] [--rule social_cost_self_enlist]

离线模式：用种子规则的正则扫 terminal_events 里她近 N 天的发言（校准用，
不依赖线上是否已有命中）。
    python scripts/cost_gate_board.py --history-days 3

只读，不写任何东西。校准结论 = 每条命中列出来人工看，误报多就收紧正则。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.context.chat_window import _dedupe_self_echoes, messages_from_rows  # noqa: E402

META_DB = ROOT / "data" / "yinyue_meta.db"
SEED = ROOT / "configs" / "speech_seed" / "policy_seed.json"


def _cost_rules() -> list[tuple[str, str, list[str]]]:
    """[(key, name, patterns)] —— 从种子文件读 social_cost_* 规则。"""
    data = json.loads(SEED.read_text(encoding="utf-8"))
    out: list[tuple[str, str, list[str]]] = []
    for rule in data:
        key = str(rule.get("key") or "")
        if not key.startswith("social_cost_"):
            continue
        payload = rule.get("payload") or {}
        if str(payload.get("action")) != "log_only":
            continue
        patterns = [str(p) for p in (payload.get("patterns") or []) if str(p).strip()]
        if patterns:
            out.append((key, str(rule.get("name") or key), patterns))
    return out


def _show_online(args: argparse.Namespace) -> int:
    since = (datetime.now() - timedelta(days=max(1, args.days))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    con = sqlite3.connect(f"file:{META_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select id, created_at, chat_id, rule_ids_json, content_preview, error "
            "from outbound_audit where decision = 'log_only' and created_at >= :since "
            "order by id desc limit :limit",
            {"since": since, "limit": max(1, args.limit)},
        ).fetchall()
    finally:
        con.close()
    print(f"[cost_gate_board] 在线看板 · 最近 {args.days} 天 · log_only 命中 {len(rows)} 条")
    for rid, created, chat, rules_json, preview, error in rows:
        try:
            rules = ",".join(json.loads(rules_json or "[]"))
        except Exception:
            rules = str(rules_json or "")
        if args.rule and args.rule not in rules:
            continue
        text = " ".join(str(preview or "").split())[:80]
        print(f"  #{rid} {created} [{rules}] {str(chat)[:22]}")
        print(f"      {text}")
    if not rows:
        print("  （还没有命中——群聊里出现对应句式时会自动留痕）")
    return 0


def _show_history(args: argparse.Namespace) -> int:
    rules = _cost_rules()
    if not rules:
        print("种子文件里没有 social_cost_* 规则")
        return 1
    since = (datetime.now() - timedelta(days=max(1, args.history_days))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    con = sqlite3.connect(f"file:{META_DB}?mode=ro", uri=True)
    try:
        raw = [
            dict(
                zip(
                    ("occurred_at", "event_type", "actor_person_id", "content", "metadata_json"),
                    row,
                )
            )
            for row in con.execute(
                "select occurred_at, event_type, actor_person_id, content, metadata_json "
                "from terminal_events where occurred_at >= :since "
                "and event_type in ('message','question','reply','request') "
                "order by occurred_at, sequence",
                {"since": since},
            )
        ]
    finally:
        con.close()
    msgs = _dedupe_self_echoes(messages_from_rows(raw))
    hers = [
        m
        for m in msgs
        if m.is_self
        and len("".join(str(m.content or "").split())) >= 4
        and not str(m.content or "").startswith(
            ("[表情包", "[图片", "[媒体消息", "[动画表情")
        )
    ]
    print(
        f"[cost_gate_board] 离线校准 · 她近 {args.history_days} 天发言 {len(hers)} 条"
    )
    for key, name, patterns in rules:
        hits = []
        for msg in hers:
            text = " ".join(str(msg.content or "").split())
            for pattern in patterns:
                try:
                    found = re.search(pattern, text)
                except re.error:
                    continue
                if found:
                    hits.append((msg.occurred_at, text, found.group(0)))
                    break
        print(f"  ── {name}（{key}）：命中 {len(hits)} 条")
        for stamp, text, frag in hits[: args.limit]:
            print(f"     {stamp:%m-%d %H:%M} [{frag[:28]}] {text[:66]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=3, help="在线模式回看天数")
    parser.add_argument(
        "--history-days", type=int, default=0, help=">0 时走离线校准模式"
    )
    parser.add_argument("--rule", default="", help="只看某条规则 key")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.history_days > 0:
        return _show_history(args)
    return _show_online(args)


if __name__ == "__main__":
    raise SystemExit(main())
