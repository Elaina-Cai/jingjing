#!/usr/bin/env python
"""账1 观察期日检：一屏看新链路的活口与异常（只读）。

汇总（默认今天，本地墙钟）：
  * 进程：Web 8972 是否在监听；日志里 yinyue_start 次数（停机/重启）
  * 合并：group_coalesce_fired 次数 + 合计 merged（按会话）
  * 保护：group_turn_skipped 次数 + reason 分布（2c）
  * 落块：真实会话回合里 [局况] / [在场人·备注] 是否出现 + chars 范围
  * 反馈：feedback_samples 的 correction / repeat（当天）
红项（退出码 1）：Web 端口不通；有群回合但没有 [局况]；[在场人·备注] 出现但
最大 chars < 60（人物卡疑似没渲染）。

用法：
  python scripts/observe_rollout.py
  python scripts/observe_rollout.py --date 2026-09-23 --json
"""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
from collections import Counter
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG = ROOT / "logs" / "yinyue.log"
TRACE = ROOT / "logs" / "decision_trace.jsonl"
META_DB = ROOT / "data" / "yinyue_meta.db"
LOCAL_OFFSET = datetime.now().astimezone().utcoffset() or timedelta(hours=8)
FIXTURE_CHATS = {
    "12345678@chatroom",
    "wxid_friend_test",
    "wxid_stranger_test",
    "10000000005@chatroom",
    "owner_raw",
    "friend_raw",
    "g1",
    "owner_private",
    "owner",
}


def parse_utc(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def iter_jsonl(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def feedback_counts(lo_local: datetime, hi_local: datetime) -> Counter:
    out: Counter = Counter()
    if not META_DB.exists():
        return out
    con = sqlite3.connect(str(META_DB))
    try:
        for source, n in con.execute(
            "select source, count(*) from feedback_samples "
            "where created_at >= ? and created_at < ? group by source",
            (lo_local.strftime("%Y-%m-%d %H:%M:%S"), hi_local.strftime("%Y-%m-%d %H:%M:%S")),
        ):
            out[str(source)] = int(n)
    finally:
        con.close()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="账1 观察期日检")
    parser.add_argument("--date", default="", help="本地日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--port", type=int, default=8972)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date_cls.today()
    lo_local = datetime.combine(day, time.min)
    hi_local = lo_local + timedelta(days=1)
    lo_utc, hi_utc = lo_local - LOCAL_OFFSET, hi_local - LOCAL_OFFSET

    errors: list[str] = []
    warns: list[str] = []
    summary: dict = {"date": str(day)}

    # 1) 进程
    web_ok = port_open("127.0.0.1", args.port)
    if not web_ok:
        errors.append(f"Web {args.port} 端口不通（鲸鲸可能停机）")
    starts = []
    starts_utc: list[datetime] = []
    queue_err_ts: list[datetime] = []
    lock_blocked = 0
    coalesce = Counter()
    coalesce_merged = 0
    skipped = Counter()
    for o in iter_jsonl(LOG):
        ts = parse_utc(str(o.get("timestamp") or ""))
        if ts is None or not (lo_utc <= ts < hi_utc):
            continue
        ev = str(o.get("event") or "")
        if ev == "yinyue_start":
            starts.append(ts + LOCAL_OFFSET)
            starts_utc.append(ts)
        if "bound to a different event loop" in json.dumps(o, ensure_ascii=False):
            queue_err_ts.append(ts)
        elif ev == "scheduler_already_running":
            lock_blocked += 1
        elif ev == "group_coalesce_fired":
            coalesce[str(o.get("chat_id"))] += 1
            coalesce_merged += int(o.get("merged") or 0)
        elif ev == "group_turn_skipped":
            skipped[str(o.get("reason") or "?")] += 1
    if lock_blocked:
        warns.append(f"当天有 {lock_blocked} 次启动被单实例锁挡回（防护生效；重启要先停旧实例）")
    last_start = max(starts_utc) if starts_utc else None
    recent_queue_errors = [t for t in queue_err_ts if last_start is None or t >= last_start]
    if recent_queue_errors:
        errors.append(f"当前实例仍有 {len(recent_queue_errors)} 条「bound to a different event loop」")
    summary["queue_loop_errors"] = {"all_day": len(queue_err_ts), "since_last_start": len(recent_queue_errors)}
    metrics_path = ROOT / "logs" / "background_queue_metrics.json"
    queue_metrics: dict = {}
    try:
        queue_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except Exception:
        queue_metrics = {}
    if queue_metrics:
        summary["background_queue"] = queue_metrics
        try:
            recorded = datetime.fromisoformat(str(queue_metrics.get("recorded_at") or ""))
            age = max(0.0, (datetime.now() - recorded).total_seconds())
        except Exception:
            age = None
        if age is not None and age < 900:
            workers_n = int(queue_metrics.get("workers") or 0)
            size_n = int(queue_metrics.get("size") or 0)
            if workers_n == 0 and size_n > 0:
                errors.append("后台队列有积压但 workers=0（可能又绑错 loop）")
            if int(queue_metrics.get("dropped") or 0) > 0:
                warns.append(f"后台队列 dropped={queue_metrics['dropped']}（有任务被丢弃）")
    summary["web_ok"] = web_ok
    summary["yinyue_start"] = [dt.strftime("%H:%M:%S") for dt in starts]
    summary["coalesce_fires"] = sum(coalesce.values())
    summary["coalesce_merged"] = coalesce_merged
    summary["skipped"] = dict(skipped)

    # 2) prompt 块落 trace（真实会话）
    turns = 0
    room_hits: list[int] = []
    notes_hits: list[int] = []
    for o in iter_jsonl(TRACE):
        if o.get("kind") != "context_detail":
            continue
        at = str(o.get("at") or "")
        if o.get("section") != "system" or not (lo_local.isoformat() <= at < hi_local.isoformat()):
            continue
        chat = str(o.get("chat_id") or "")
        if chat in FIXTURE_CHATS:
            continue
        turns += 1
        blocks = {str(b.get("name")): b for b in (o.get("blocks") or [])}
        if "[局况]" in blocks:
            room_hits.append(int(blocks["[局况]"].get("chars") or 0))
        if "[在场人·备注]" in blocks:
            notes_hits.append(int(blocks["[在场人·备注]"].get("chars") or 0))
    if turns and not room_hits:
        errors.append(f"{turns} 个真实回合里没有 [局况] 块（2b 可能没生效）")
    if notes_hits and max(notes_hits) < 60:
        warns.append(
            f"[在场人·备注] 最大仅 {max(notes_hits)} 字（人物卡疑似没渲染，检查 "
            "build_person_notes_block 的 people 解析）"
        )
    summary["turns"] = turns
    summary["room_block"] = {"count": len(room_hits), "min": min(room_hits) if room_hits else None, "max": max(room_hits) if room_hits else None}
    summary["notes_block"] = {"count": len(notes_hits), "min": min(notes_hits) if notes_hits else None, "max": max(notes_hits) if notes_hits else None}

    # 3) 反馈
    fb = feedback_counts(lo_local, hi_local)
    summary["feedback"] = dict(fb)
    if fb.get("correction", 0) >= 3:
        warns.append(f"当天 correction={fb['correction']} 偏高（≥3），对照 §10.1 观察口径")

    if args.json:
        print(json.dumps({"summary": summary, "errors": errors, "warns": warns}, ensure_ascii=False, indent=2))
    else:
        print(f"[observe] 账1 观察期日检 · {day}")
        print(f"  Web {args.port}: {'✓ 在监听' if web_ok else 'x 不通'}    "
              f"yinyue_start: {len(starts)} 次"
              + (f"（最近 {starts[-1].strftime('%H:%M:%S')}）" if starts else ""))
        print(f"  合并触发: {sum(coalesce.values())} 次 / 合计 merged {coalesce_merged}"
              + (f"（{dict(coalesce)}）" if coalesce else ""))
        print(f"  2c 跳过 : {sum(skipped.values())} 次" + (f"（{dict(skipped)}）" if skipped else ""))
        print(f"  真实回合: {turns} 个；[局况] {len(room_hits)} 个"
              + (f"（{min(room_hits)}~{max(room_hits)} 字）" if room_hits else ""))
        print(f"  [在场人·备注]: {len(notes_hits)} 个"
              + (f"（{min(notes_hits)}~{max(notes_hits)} 字）" if notes_hits else ""))
        print(f"  反馈    : correction={fb.get('correction', 0)} repeat={fb.get('repeat', 0)}")
        if queue_metrics:
            print(
                f"  后台队列: workers={queue_metrics.get('workers')} size={queue_metrics.get('size')} "
                f"loops={queue_metrics.get('loops')} submitted={queue_metrics.get('submitted')} "
                f"completed={queue_metrics.get('completed')} dropped={queue_metrics.get('dropped')}"
            )
        for w in warns:
            print(f"  ! {w}")
        for e in errors:
            print(f"  x {e}")
        print(f"  => {'有红项' if errors else '正常'}")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
