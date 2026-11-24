#!/usr/bin/env python
"""真实回合回放（logs/trace 级，不调 LLM）。

把 context_trace 里的真实回合当作验收集：
  1) 用同源数据重建 prompt 块（[局况]）；
  2) 与 trace 记录的块 chars/hash（sha1[:12]）比对——一致 = 逐字节复现；
  3) 检查行为预期（yinyue.log 的 coalesce/route 事件 + terminal_events 的动作/回复）。

同步直回路径（@ / 拍 / 引用她）的 [局况] 可逐字节复现；异步触发（合并 / 延迟）
含内存 pending，历史不可完全回溯，只做内容检查（见 configs/log_cases.json 注释）。

用法：
  python scripts/replay_log_cases.py
  python scripts/replay_log_cases.py --json
  python scripts/replay_log_cases.py --case R4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASES = ROOT / "configs" / "log_cases.json"
TRACE = ROOT / "logs" / "decision_trace.jsonl"
LOG = ROOT / "logs" / "yinyue.log"
META_DB = ROOT / "data" / "yinyue_meta.db"
LOCAL_OFFSET = datetime.now().astimezone().utcoffset() or timedelta(hours=8)


def sha12(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def load_cases() -> list[dict]:
    return list(json.loads(CASES.read_text(encoding="utf-8")).get("cases") or [])


def find_trace_blocks(chat: str, at_str: str):
    """返回 (实际 at, {块名: (chars, hash)})；找不到返回 (None, {})。"""
    try:
        text = TRACE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("kind") != "context_detail":
            continue
        if o.get("section") != "system" or str(o.get("chat_id")) != chat:
            continue
        if str(o.get("at")) == at_str:
            return at_str, {
                str(b.get("name")): (b.get("chars"), str(b.get("hash") or ""))
                for b in (o.get("blocks") or [])
            }
    return None, {}


def rebuild_room(chat: str, at: datetime) -> str | None:
    from core.context.chat_window import load_messages
    from core.context.room_state import _as_room_row, build_room_situation

    rows = [m for m in load_messages(chat, days=7) if m.occurred_at <= at]
    if not rows:
        return None
    normalized = [_as_room_row(m, now=at) for m in rows][-48:]
    situation = build_room_situation(normalized, now=at)
    return situation.render() if situation else None


def rebuild(name: str, chat: str, at: datetime):
    if name == "[局况]":
        return rebuild_room(chat, at)
    return None


def load_log_events() -> list[dict]:
    events: list[dict] = []
    try:
        text = LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("timestamp"):
            events.append(o)
    return events


def parse_utc(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def in_utc_window(ts: str, center: datetime, window: int) -> bool:
    dt = parse_utc(ts)
    if dt is None:
        return False
    return center - timedelta(seconds=window) <= dt <= center + timedelta(seconds=window)


def terminal_rows(chat: str, lo: datetime, hi: datetime) -> list[tuple]:
    if not META_DB.exists():
        return []
    con = sqlite3.connect(str(META_DB))
    try:
        return con.execute(
            "select occurred_at, event_type, content from terminal_events "
            "where chat_id = ? and occurred_at >= ? and occurred_at <= ?",
            (chat, lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
    finally:
        con.close()


def evaluate(case: dict) -> dict:
    problems: list[str] = []
    notes: list[str] = []
    chat = str(case.get("chat") or "")
    at = datetime.fromisoformat(case["trace_at"])
    utc_at = at - LOCAL_OFFSET
    window = int((case.get("log_expect") or {}).get("window_seconds") or 120)

    actual_at, blocks = find_trace_blocks(chat, case["trace_at"])
    if actual_at is None:
        problems.append("trace 里找不到该回合（at 不匹配）")

    fidelity = case.get("fidelity") or {}
    for name in fidelity.get("hash_blocks") or []:
        if name not in blocks:
            problems.append(f"trace 无 {name} 块")
            continue
        rebuilt = rebuild(name, chat, at)
        want_chars, want_hash = blocks[name]
        if sha12(rebuilt) == want_hash:
            notes.append(f"{name} hash 复现 ✓（{want_chars} 字）")
        else:
            problems.append(
                f"{name} hash 不符：重建 {len(rebuilt or '')} 字/{sha12(rebuilt)} "
                f"vs trace {want_chars} 字/{want_hash}"
            )
    for name, needles in (fidelity.get("content_blocks") or {}).items():
        rebuilt = rebuild(name, chat, at) or ""
        miss = [n for n in needles if n not in rebuilt]
        if miss:
            problems.append(f"{name} 内容缺：{miss}")
        else:
            notes.append(f"{name} 内容检查 ✓")

    events = load_log_events()
    le = case.get("log_expect") or {}
    spec = le.get("coalesce")
    if spec:
        hits = [
            o
            for o in events
            if o.get("event") == "group_coalesce_fired"
            and str(o.get("chat_id")) == chat
            and in_utc_window(o.get("timestamp", ""), utc_at, int(spec.get("window_seconds") or window))
        ]
        merged = sum(int(o.get("merged") or 0) for o in hits)
        need_fires = int(spec.get("fires_min") or 1)
        need_merged = int(spec.get("merged_min") or 1)
        if len(hits) >= need_fires and merged >= need_merged:
            notes.append(f"coalesce {len(hits)} 次 / 合计 merged {merged} ✓")
        else:
            problems.append(
                f"coalesce 期望 ≥{need_fires} 次 / 合计 ≥{need_merged}，实测 {len(hits)} 次 / {merged}"
            )

    want_reason = le.get("route_reason")
    if want_reason:
        hits = [
            o
            for o in events
            if o.get("event") == "route_will_reply"
            and str(o.get("chat_id")) == chat
            and in_utc_window(o.get("timestamp", ""), utc_at, window)
        ]
        if any(str(o.get("reason")) == want_reason for o in hits):
            notes.append(f"route_will_reply={want_reason} ✓")
        else:
            got = [str(o.get("reason")) for o in hits]
            problems.append(f"route reason 期望 {want_reason}，实测 {got or '无'}")

    absent = le.get("reply_absent_seconds")
    if absent:
        hits = [
            o
            for o in events
            if o.get("event") in ("router_sent", "route_reply_done")
            and str(o.get("chat_id")) == chat
            and in_utc_window(o.get("timestamp", ""), utc_at, int(absent))
        ]
        if hits:
            problems.append(f"{absent}s 内不应有回复，却出现 {len(hits)} 条发送事件")
        else:
            notes.append(f"{absent}s 内无回复 ✓")

    actions = le.get("actions_contain")
    if actions:
        rows = terminal_rows(chat, at - timedelta(seconds=60), at + timedelta(seconds=window))
        hit = any(
            t == "action" and any(k in (c or "") for k in actions)
            for _, t, c in rows
        )
        if hit:
            notes.append(f"动作包含 {actions} ✓")
        else:
            problems.append(f"窗口内没有动作 {actions}")

    replies = le.get("reply_contains_any")
    if replies:
        rows = terminal_rows(chat, at, at + timedelta(seconds=window))
        hit = any(
            t in ("reply", "question") and any(k in (c or "") for k in replies)
            for _, t, c in rows
        )
        if hit:
            notes.append(f"回复含 {replies} 之一 ✓")
        else:
            problems.append(f"窗口内回复未含 {replies} 之一")

    return {
        "id": case.get("id"),
        "title": case.get("title"),
        "status": "PASS" if not problems else "FAIL",
        "problems": problems,
        "notes": notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="真实回合回放（logs/trace 级）")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--case", default="", help="只跑 id 含该子串的 case")
    parser.add_argument("--with-cues", action="store_true", help="复现时保留社交线索（默认强制关，匹配历史 trace）")
    args = parser.parse_args()

    cases = load_cases()
    if args.case:
        cases = [c for c in cases if args.case in str(c.get("id"))]
    results: list[dict] = []
    for case in cases:
        # case 级 env 覆盖：让「开态」内容检查（如 P2 姿态行）也能回放；
        # 未声明的键沿用全局默认（历史复现时 P1/P2 开关已被强制关）。
        overrides = (case.get("fidelity") or {}).get("env") or {}
        saved = {key: os.environ.get(key) for key in overrides}
        for key, value in overrides.items():
            os.environ[key] = str(value)
        try:
            results.append(evaluate(case))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    failed = [r for r in results if r["status"] == "FAIL"]
    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        print("[replay_log] 真实回合回放（hash/内容 + 日志行为）")
        for r in results:
            print(f"  {r['id']:<28} {r['status']}")
            for note in r["notes"]:
                print(f"      · {note}")
            for problem in r["problems"]:
                print(f"      x {problem}")
        print(f"\n  summary: {len(results) - len(failed)} PASS / {len(failed)} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
