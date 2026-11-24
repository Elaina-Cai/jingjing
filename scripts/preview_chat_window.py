#!/usr/bin/env python
"""预览：按 docs/记忆的定义.md 的窗口规则拼一段群聊上下文。

只读、不接线上、不改任何现有代码。用途是"先看效果，再决定接不接"。

用法：
    python scripts/preview_chat_window.py                      # 默认 示例宿舍群
    python scripts/preview_chat_window.py 10000000001@chatroom
    python scripts/preview_chat_window.py --full 40 --days 7

归属：P0 只读预览工具（看窗口长什么样），不接线上。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
META = ROOT / "data" / "yinyue_meta.db"
EVENT_TYPES = ("message", "question", "reply", "request")
DEFAULT_CHAT = "10000000002@chatroom"  # 示例宿舍群


def approx_tokens(text: str) -> int:
    """与 core/context/budget.estimate_tokens 同口径：中文≈1 token/字，其他≈1/4。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(text) - cjk) // 4


def one_line(text: str, limit: int = 20) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def load_rows(chat_id: str, days: int) -> list[dict]:
    con = sqlite3.connect(META)
    con.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in EVENT_TYPES)
    sql = (
        "select occurred_at, actor_person_id, event_type, content, metadata_json "
        "from terminal_events "
        f"where chat_id = ? and event_type in ({placeholders}) "
        "and occurred_at >= datetime('now', 'localtime', ?) "
        "order by occurred_at"
    )
    rows = [dict(r) for r in con.execute(sql, (chat_id, *EVENT_TYPES, f"-{days} days"))]
    con.close()

    names: dict[str, str] = {}
    bot_wxid = ""
    con = sqlite3.connect(META)
    con.row_factory = sqlite3.Row
    for r in con.execute(
        "select pi.identity_value, pe.display_name from person_identities pi "
        "join person_entities pe on pe.person_id = pi.person_id "
        "where pe.display_name is not null and pe.display_name != ''"
    ):
        names.setdefault(str(r["identity_value"]), str(r["display_name"]))
    for r in con.execute("select account_id from account_registry where role = 'yinyue'"):
        bot_wxid = str(r["account_id"] or "")
    con.close()

    for r in rows:
        try:
            md = json.loads(r["metadata_json"] or "{}")
        except Exception:
            md = {}
        actor = str(r["actor_person_id"] or "")
        is_self = md.get("is_self")
        # 归因规则：reply/thought 是她的输出（actor 存的是收件人）；is_self=1 是她自己发的
        if r["event_type"] in ("reply", "thought") or is_self == 1 or (bot_wxid and actor == bot_wxid):
            r["speaker"] = "鲸鲸"
        else:
            r["speaker"] = (
                md.get("display_name")
                or md.get("sender_name")
                or names.get(actor)
                or (actor[:8] if actor else "?")
            )
        try:
            r["dt"] = datetime.fromisoformat(str(r["occurred_at"]))
        except ValueError:
            r["dt"] = None
    return rows


def fmt_full(r: dict) -> str:
    return f"[{r['dt']:%H:%M}] {r['speaker']}: {one_line(r['content'], 400)}"


def fmt_skeleton(r: dict, limit: int = 20) -> str:
    return f"[{r['dt']:%H:%M}] {r['speaker']}: {one_line(r['content'], limit)}"


def bucket_lines(
    rows: list[dict], minutes: int = 10, limit: int = 12, max_chars: int = 220
) -> list[str]:
    """档 3：按时间桶降密度（首尾相接、无重叠无空隙），桶内按人列片段。

    单行超 ``max_chars`` 时保留前若干片段并标注还剩几条（宁缺清晰，勿塞长行）。
    """
    buckets: list[tuple[datetime, list[dict]]] = []
    for r in rows:
        if r["dt"] is None:
            continue
        slot = r["dt"].replace(
            minute=(r["dt"].minute // minutes) * minutes, second=0, microsecond=0
        )
        if buckets and buckets[-1][0] == slot:
            buckets[-1][1].append(r)
        else:
            buckets.append((slot, [r]))
    lines = []
    for slot, items in buckets:
        parts: list[str] = []
        used = 0
        for r in items:
            piece = f"{r['speaker']}: {one_line(r['content'], limit)}"
            if used + len(piece) + 3 > max_chars:
                parts.append(f"…（+{len(items) - len(parts)} 条）")
                break
            parts.append(piece)
            used += len(piece) + 3
        lines.append(f"[{slot:%H:%M}] " + " ／ ".join(parts))
    return lines


def taper_lines(rows: list[dict], limits: tuple[int, int, int] = (12, 8, 4)) -> list[str]:
    """片段长度随距离衰减 + 同一分钟共用行首时间戳——**一条不漏**。

    "越远越模糊"落在"内容密度"上，而不是落在"丢消息"上：
    最远处仍保留"时间 + 说话人"的骨架；同一分钟的多条消息共用行首时间戳。
    """
    n = len(rows)
    if n == 0:
        return []
    near, mid, far = limits
    out: list[str] = []
    slot = None
    parts: list[str] = []

    def flush() -> None:
        if parts:
            out.append(f"[{slot:%H:%M}] " + " ／ ".join(parts))

    for i, r in enumerate(rows):  # i=0 最老 → i=n-1 最新
        frac = i / max(1, n - 1)
        limit = far if frac < 1 / 3 else (mid if frac < 2 / 3 else near)
        current = r["dt"].replace(second=0, microsecond=0)
        if slot is None or current != slot:
            flush()
            parts = []
            slot = current
        parts.append(f"{r['speaker']}: {one_line(r['content'], limit)}")
    flush()
    return out


def shrink(rows: list[dict], budget_chars: int) -> tuple[list[str], str]:
    """按 docs §1.2 的降级顺序：骨架(20) → 骨架(10) → 距离锥形 → 时间桶（不丢条）。"""
    for limit in (20, 10):
        lines = [fmt_skeleton(r, limit) for r in rows]
        used = sum(len(x) + 3 for x in lines)
        if used <= budget_chars:
            return lines, f"逐条骨架（每题 {limit} 字）"
    lines = taper_lines(rows)
    used = sum(len(x) + 3 for x in lines)
    if used <= budget_chars:
        return lines, "距离锥形（近12/中8/远4字，一条不漏）"
    lines = bucket_lines(rows, minutes=10, limit=12, max_chars=10**9)
    return lines, "时间桶（每 10 分钟 1 行，桶内不丢条）"


def fmt_day(day: str, rows: list[dict]) -> str:
    speakers: dict[str, int] = {}
    for r in rows:
        speakers[r["speaker"]] = speakers.get(r["speaker"], 0) + 1
    top = "、".join(
        name for name, _ in sorted(speakers.items(), key=lambda x: -x[1])[:3]
    )
    head = one_line(rows[0]["content"], 24)
    tail = one_line(rows[-1]["content"], 24)
    return f"[{day}] {len(rows)} 条 · 主要在说：{top} · 起：{head} · 止：{tail}"


def build_window(chat_id: str, full: int, days: int, skeleton_budget: int = 700) -> dict:
    rows = load_rows(chat_id, days)
    today = datetime.now().strftime("%Y-%m-%d")
    today_rows = [r for r in rows if r["dt"] and r["dt"].strftime("%Y-%m-%d") == today]
    older = [r for r in rows if r not in today_rows]

    full_part = today_rows[-full:]
    skeleton_part = today_rows[: max(0, len(today_rows) - full)]

    by_day: dict[str, list[dict]] = {}
    for r in older:
        if r["dt"] is None:
            continue
        key = r["dt"].strftime("%m-%d")
        by_day.setdefault(key, []).append(r)

    lines_full = [fmt_full(r) for r in full_part]
    lines_skel, skel_mode = shrink(skeleton_part, skeleton_budget)
    lines_day = [fmt_day(day, items) for day, items in sorted(by_day.items())]

    # 守恒断言（docs §1.3）
    assert len(full_part) + len(skeleton_part) == len(today_rows), "条数不守恒"
    assert [r["occurred_at"] for r in skeleton_part + full_part] == [
        r["occurred_at"] for r in today_rows
    ], "顺序不守恒"
    covered = len(skeleton_part)
    if "时间桶" in skel_mode:
        assert covered == len(skeleton_part), "时间桶必须覆盖全部"

    return {
        "chat_id": chat_id,
        "full": lines_full,
        "skeleton": lines_skel,
        "skeleton_mode": skel_mode,
        "skeleton_source_count": len(skeleton_part),
        "days": lines_day,
        "total_messages": len(rows),
        "today_messages": len(today_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chat_id", nargs="?", default=DEFAULT_CHAT)
    parser.add_argument("--full", type=int, default=40, help="最近 N 条原文全量")
    parser.add_argument("--days", type=int, default=7, help="回溯天数")
    parser.add_argument("--budget", type=int, default=3200, help="窗口总字数预算")
    args = parser.parse_args()

    # 总预算制：先满足 ①场外(≤300) 与 ②最近 N 条全量，剩下的给 ③更早
    probe = load_rows(args.chat_id, args.days)
    today_key = datetime.now().strftime("%Y-%m-%d")
    today_rows = [
        r for r in probe if r["dt"] and r["dt"].strftime("%Y-%m-%d") == today_key
    ]
    full_len = sum(len(fmt_full(r)) + 3 for r in today_rows[-args.full:])
    skel_budget = max(200, args.budget - full_len - 300)
    win = build_window(args.chat_id, args.full, args.days, skeleton_budget=skel_budget)

    out: list[str] = []
    out.append(f"══════ 新窗口预览：{win['chat_id']} ══════")
    out.append(f"（近 {args.days} 天共 {win['total_messages']} 条，其中今天 {win['today_messages']} 条）")
    out.append("")
    out.append("【① 场外补充 · 占位】(人物备注 + 群基线——P1 生成，预计 ≤300 字)")
    out.append("  （此处留白）")
    out.append("")
    out.append(f"【② 最近 {args.full} 条 · 原文全量】({len(win['full'])} 条)")
    out.extend("  " + line for line in win["full"])
    out.append("")
    out.append(
        f"【③ 今天更早 · 压缩降级】({win['skeleton_source_count']} 条 → {len(win['skeleton'])} 行"
        f"；档位：{win['skeleton_mode']})"
    )
    out.extend("  " + line for line in win["skeleton"])
    out.append("")
    out.append(f"【④ 近 {args.days} 天 · 每天 1 行】({len(win['days'])} 天)")
    out.extend("  " + line for line in win["days"])

    body = "\n".join(out)
    win_chars = len(body)
    win_tokens = approx_tokens(body)

    # 现状近似：最近 30 条原文 + 6 条检索记忆（真实块长度以线上为准）
    old_text = "\n".join(win["full"][-30:]) + "\n" + ("x" * 300)
    old_chars = len(old_text)
    old_tokens = approx_tokens(old_text)

    print(body)
    print()
    print("──────── 统计 ────────")
    print(
        f"新窗口：{win_chars} 字 ≈ {win_tokens} token"
        f"（②{sum(len(x) for x in win['full'])} + ③{sum(len(x) for x in win['skeleton'])}"
        f" + ④{sum(len(x) for x in win['days'])} + 场外 ≤300）"
    )
    print(
        f"现状近似（最近 30 条原文 + 6 条检索块）：{old_chars} 字 ≈ {old_tokens} token"
    )
    print(
        f"差额：{win_tokens - old_tokens:+d} token"
        f"（现状输入中位数实测 2185 token）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
