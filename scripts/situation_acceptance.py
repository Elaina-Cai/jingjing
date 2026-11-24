#!/usr/bin/env python
"""处境验收读数（只读）：把 [当前局面] 的事实值和她的原话排在一张表里。

用法::

    .\\.venv\\Scripts\\python.exe scripts\\situation_acceptance.py --days 3

数据源（只读，不写生产日志/库）::

    logs/yinyue.log           structlog JSONL，event=state_assembly / state_assembly_shadow
    logs/decision_trace.jsonl 每回合 [当前局面] 块的 hash（与上面 block_hash 对齐）
    logs/render_pairs.jsonl   真实发送的她的话（rendered）

机器只负责"把该读的回合找出来 + 关键词预筛"；通过/失败仍由人读判定
（归她 / 归系统；偶尔提 / 每次提 / 报表腔）。usage_pct 字段是本次生产改动
新增的，重启前的事件没有该字段，会显示为"旧进程/未注入"。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import temporal  # noqa: E402

DEFAULT_LOG = ROOT / "logs" / "yinyue.log"
DEFAULT_TRACE = ROOT / "logs" / "decision_trace.jsonl"
DEFAULT_PAIRS = ROOT / "logs" / "render_pairs.jsonl"

PATTERNS = {
    "time": re.compile(r"这么晚|一点|两点|凌晨|几点|该睡|熬夜|这个点|半夜|快睡"),
    "consumption": re.compile(r"消耗|烧|省|费|参考量|额度|预算|token|话费|一半"),
    "self_behavior": re.compile(r"主动|我找|找你|发给你|等你|没回|不回|我答应|说好|记着"),
    "memory": re.compile(r"还没|上次|之前说|答应|说好|约好|未了结|讲书|提过|那件事"),
    "report_tone": re.compile(r"(消耗|参考量|预算|额度).{0,8}\d+%|请注意|提醒你"),
}

def _read_jsonl(path):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
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

def _parse_dt(raw):
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return temporal.to_naive_local(dt).replace(microsecond=0)

def load_state_events(path, cutoff):
    out = []
    for d in _read_jsonl(path):
        if d.get("event") not in ("state_assembly", "state_assembly_shadow"):
            continue
        at = _parse_dt(d.get("timestamp"))
        if at is None or at < cutoff:
            continue
        out.append({
            "at": at,
            "event": str(d.get("event")),
            "chat_id": str(d.get("chat_id") or ""),
            "usage_pct": d.get("usage_pct", "MISSING"),
            "block_hash": str(d.get("block_hash") or ""),
            "block_len": d.get("block_len"),
            "is_group": bool(d.get("is_group")),
        })
    return out

def load_trace_index(path):
    idx = {}
    for d in _read_jsonl(path):
        at = _parse_dt(d.get("at"))
        chat = str(d.get("chat_id") or "")
        for b in d.get("blocks") or []:
            if b.get("name") == "[当前局面]" and b.get("hash"):
                idx[str(b["hash"])] = {"at": at, "chat_id": chat}
    return idx

def load_pairs(path):
    out = []
    for d in _read_jsonl(path):
        at = _parse_dt(d.get("ts"))
        if at is None:
            continue
        out.append({
            "at": at,
            "scene": str(d.get("scene") or ""),
            "inbound": str(d.get("inbound") or ""),
            "rendered": str(d.get("rendered") or ""),
        })
    return out

def _hits(text):
    t = str(text or "")
    return {k: bool(rx.search(t)) for k, rx in PATTERNS.items()}

def _nearest_pair(pairs, at, scene, tol=300):
    best = None
    for p in pairs:
        if scene and p["scene"] != scene:
            continue
        delta = abs((p["at"] - at).total_seconds())
        if delta <= tol and (best is None or delta < best[0]):
            best = (delta, p)
    return best[1] if best else None

def _usage_label(value):
    if value == "MISSING":
        return "旧进程/未注入"
    if value is None:
        return "无(<1%或读不到)"
    try:
        return "%.0f%%" % float(value)
    except (TypeError, ValueError):
        return str(value)

def main() -> int:
    parser = argparse.ArgumentParser(description="处境验收读数（只读）")
    parser.add_argument("--days", type=float, default=3.0)
    parser.add_argument("--chat", default="")
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--trace", default=str(DEFAULT_TRACE))
    parser.add_argument("--pairs", default=str(DEFAULT_PAIRS))
    parser.add_argument("--out", default="")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    cutoff = datetime.now() - timedelta(days=args.days)
    events = load_state_events(args.log, cutoff)
    if args.chat:
        events = [e for e in events if e["chat_id"] == args.chat]
    trace = load_trace_index(args.trace)
    pairs = load_pairs(args.pairs)

    rows = []
    for ev in sorted(events, key=lambda e: e["at"]):
        info = trace.get(ev["block_hash"]) if ev["block_hash"] else None
        at = (info or {}).get("at") or ev["at"]
        chat = (info or {}).get("chat_id") or ev["chat_id"]
        scene = "group" if ev["is_group"] else "private"
        pair = _nearest_pair(pairs, at, scene)
        reply = pair["rendered"] if pair else ""
        inbound = pair["inbound"] if pair else ""
        rows.append({
            "at": at, "chat": chat, "event": ev["event"],
            "usage": _usage_label(ev["usage_pct"]),
            "usage_num": ev["usage_pct"] if ev["usage_pct"] != "MISSING" else None,
            "block_len": ev["block_len"],
            "reply": reply, "inbound": inbound, "hits": _hits(reply),
        })

    injected = [r for r in rows if isinstance(r["usage_num"], (int, float))]
    matched = [r for r in rows if r["reply"]]
    lines = ["# 处境验收读数 " + datetime.now().strftime("%Y-%m-%d %H:%M"), ""]
    lines.append("- 窗口：最近 %s 天" % args.days)
    lines.append("- state_assembly 事件：%d（usage_pct 数值行 %d）" % (len(rows), len(injected)))
    lines.append("- 匹配到她原话的回合：%d" % len(matched))
    for key in PATTERNS:
        lines.append("- 原话命中 %s：%d" % (key, sum(1 for r in rows if r["hits"][key])))
    lines.append("- 说明：usage_pct 数值=事实在场；旧进程行没有该字段；通过/失败仍由人读判定。")
    lines.append("")
    lines.append("| 本地时间 | 会话 | 事件 | usage_pct | 她的话 | 用户/触发 | 预筛 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows[: max(0, args.limit)]:
        reply = r["reply"].replace("\n", " / ")[:100] or "（未匹配到发送记录）"
        inbound = r["inbound"].replace("\n", " / ")[:60] or "-"
        marks = ",".join(k for k, v in r["hits"].items() if v) or "-"
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            r["at"].strftime("%m-%d %H:%M"), r["chat"], r["event"],
            r["usage"], reply, inbound, marks))
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        print("written:", args.out)
    print(text)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
