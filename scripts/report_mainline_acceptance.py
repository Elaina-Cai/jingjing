# -*- coding: utf-8 -*-
"""主线验收读数：当前 on 的件（房间片段 / 情绪分层 / 自我模型 / 连续性 / 归属 / @她上下文）。

用法：
  .venv_it/Scripts/python.exe scripts/report_mainline_acceptance.py
  .venv_it/Scripts/python.exe scripts/report_mainline_acceptance.py --since 2026-10-14T13:59:14
不给 --since 时用日志里最后一个 yinyue_start。
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import statistics
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]



def _iter(log_path):
    for line in pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
        if '"event"' not in line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            pass


def _since_from_log(events, since):
    if since:
        return since
    starts = [str(e.get("timestamp", "")) for e in events if e.get("event") == "yinyue_start"]
    return starts[-1] if starts else ""


def _local(since):
    try:
        dt = datetime.fromisoformat(since.rstrip("Z"))
        return (dt + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return ""


def _is_true(v):
    return v is True or str(v).lower() == "true"


def collect(log_path, since):
    events = list(_iter(log_path))
    since = _since_from_log(events, since)
    local_since = _local(since)
    room = collections.Counter()
    room_samples = []
    emo = collections.Counter()
    cont = collections.Counter()
    cont_chars = []
    dupes = []
    addr = collections.Counter()
    repaired = []
    judge_diff = []
    sends = 0
    writes = 0
    for e in events:
        ts = str(e.get("timestamp", ""))
        if since and ts < since:
            continue
        ev = str(e.get("event"))
        if ev == "room_episode_used":
            room[str(e.get("action") or "?")] += 1
            if len(room_samples) < 20:
                room_samples.append({"ts": ts[:19], "action": e.get("action"), "speakers": e.get("speakers"), "messages": e.get("messages"), "span": e.get("span_seconds")})
        elif ev in ("emotion_layers_outbound", "emotion_layers_shadow"):
            emo[ev] += 1
        elif ev == "self_continuity_used":
            cont["used"] += 1
            try:
                cont_chars.append(int(e.get("chars") or 0))
            except Exception:
                pass
        elif ev == "self_duplicate_draft":
            dupes.append({"ts": ts[:19], "dup_chars": e.get("dup_chars"), "reply_chars": e.get("reply_chars")})
        elif ev == "router_sent":
            sends += 1
        elif ev == "self_model_write":
            writes += 1
        elif ev == "addressee_repaired":
            repaired.append({"ts": ts[:19], "from": e.get("from"), "to": e.get("to"), "applied": e.get("applied")})
        elif ev == "addressee_judge_diff":
            judge_diff.append({"ts": ts[:19], "guard": [e.get("guard_from"), e.get("guard_to")], "render": [e.get("render_from"), e.get("render_to")]})
    recent = []
    for line in pathlib.Path(str(ROOT / "logs/decision_trace.jsonl")).read_text(encoding="utf-8", errors="replace").splitlines():
        if '"section": "system"' not in line or '"blocks"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        at = str(o.get("at") or "")
        if local_since and at and at < local_since:
            continue
        triggers = (o.get("world") or {}).get("trigger_reasons") or []
        if "mentioned_in_group" not in triggers:
            continue
        for b in o.get("blocks") or ():
            if b.get("source") == "session.group_recent":
                try:
                    recent.append(int(b.get("chars") or 0))
                except Exception:
                    pass
    return {
        "since": since,
        "room": {"by_action": dict(room), "samples": room_samples[:5]},
        "emotion": dict(emo),
        "continuity": {"used": cont["used"], "chars_min": min(cont_chars) if cont_chars else 0, "chars_max": max(cont_chars) if cont_chars else 0, "dupes": dupes},
        "self_model": {"writes": writes, "sends": sends, "coverage_gap": max(0, sends - writes)},
        "addressee": {"from_intent": dict(addr), "repaired": repaired[:8], "judge_diff": judge_diff[:8]},
        "context": {"turns": len(recent), "min_chars": min(recent) if recent else 0, "median": int(statistics.median(recent)) if recent else 0, "below_500": sum(1 for c in recent if c < 500)},
    }


def gates(r):
    out = []

    def add(name, metric, threshold, ready, note=""):
        out.append({"name": name, "metric": metric, "threshold": threshold, "ready": ready, "note": note})

    used = sum(r["room"]["by_action"].values())
    add("room_episode", "used=%s" % used, ">=10 片段 + 人工抽检合片正确", used >= 10, "需人工抽检")
    add("emotion_layers", "outbound=%s" % r["emotion"].get("emotion_layers_outbound", 0), ">=20 条抽检无不合适语气", r["emotion"].get("emotion_layers_outbound", 0) >= 20)
    sm = r["self_model"]
    add("self_model", "writes=%s sends=%s" % (sm["writes"], sm["sends"]), "coverage_gap<=1", sm["coverage_gap"] <= 1)
    c = r["continuity"]
    add("continuity", "used=%s dupes=%s" % (c["used"], len(c["dupes"])), "used>=5 且 dupes=0（复读改写闸已退役，2026-10-24）", c["used"] >= 5 and len(c["dupes"]) == 0, "需人工抽检群里是否可见复读")
    a = r["addressee"]
    add("addressee", "from_intent=%s repaired=%s judge_diff=%s" % (a["from_intent"], len(a["repaired"]), len(a["judge_diff"])), "@错人抽检=0", False, "需人工抽检 30 条")
    ctx = r["context"]
    add("admission_context", "turns=%s min=%s median=%s below500=%s" % (ctx["turns"], ctx["min_chars"], ctx["median"], ctx["below_500"]), "修复后 @她 回合全部 >=500 字", ctx["below_500"] == 0 and ctx["turns"] > 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(ROOT / "logs/yinyue.log"))
    ap.add_argument("--since", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    report = collect(args.log, args.since)
    report["gates"] = gates(report)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        pathlib.Path(args.json).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
