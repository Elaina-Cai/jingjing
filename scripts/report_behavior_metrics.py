# -*- coding: utf-8 -*-
"""行为指标 v1（只读日志）：自相矛盾/改口、任务链存活、工具误开。

用法：
  .venv_it\\Scripts\\python.exe scripts\\report_behavior_metrics.py --since 2026-10-15T00:00 --top 8

口径见 docs/decisions/行为指标_量具单.md。只读日志、不改运行时行为、不调 LLM。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
from datetime import datetime, timedelta

DEFAULT_LOG = "logs/yinyue.log"
DEFAULT_PAIRS = "logs/render_pairs.jsonl"

# 与 agents/conversational._TOOL_ASK_RE 同源（tests/test_behavior_metrics.py 有同步锁）
TOOL_ASK_PATTERN = (
    "帮我|帮忙|替我|给我查|查一下|查查|搜一下|搜索|搜搜|找一下|找找|"
    "写一份|写个|写一下|整理|总结一下|算一下|翻译|提醒我|记一下|记下|"
    "日程|安排一下|计划一下|订一下"
)
NEG_RE = re.compile(r"不|没|别|未|无|否|非|莫")
ACCUSE_RE = re.compile(
    r"你骗|骗我|撒谎|不是说|不是刚|你自己说的|前后矛盾|又改口|不是说好|刚才还说|别装|耍我"
)
AT_RE = re.compile(r"@[^\s,，。；;:：!！?？、）)】\]]+")
PUNCT_RE = re.compile(r"[\s，。！？；：、（）【】《》…—～·,.!?;:'\"()\[\]<>@]+")
TASK_SIGNALS = {"delegated", "promised", "time_pressure", "revoke_risk"}
TASK_INTENTS = {"执行动作", "提醒"}
CASUAL_INTENTS = {"闲聊", "玩梗", "情绪宣泄", "吐槽", "安慰"}


def _parse_ts(value):
    text = str(value or "").strip()
    if not text:
        return None
    text = text.rstrip("Z")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _norm(text):
    out = AT_RE.sub("", str(text or ""))
    out = PUNCT_RE.sub("", out)
    return out.lower()


def _bigrams(text):
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _jac(a, b):
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def load_pairs(path=DEFAULT_PAIRS, since=None):
    rows = []
    p = pathlib.Path(path)
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        ts = _parse_ts(o.get("ts"))
        if ts is not None:
            ts = ts - (datetime.now().astimezone().utcoffset() or timedelta(0))
        text = str(o.get("rendered") or o.get("draft") or "").strip()
        if ts is None or not text:
            continue
        if since is not None and ts < since:
            continue
        rows.append({"ts": ts, "text": text, "inbound": str(o.get("inbound") or "")})
    rows.sort(key=lambda r: r["ts"])
    return rows


def load_events(path=DEFAULT_LOG, since=None):
    events = []
    p = pathlib.Path(path)
    if not p.exists():
        return events
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"event"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        ts = _parse_ts(o.get("timestamp"))
        if ts is None:
            continue
        if since is not None and ts < since:
            continue
        events.append({"ts": ts, "event": str(o.get("event")), "raw": o})
    return events


def similar_pairs(pairs, *, window_seconds=900, top=8):
    """A1 近重复对（她两次说得很像）+ A2 否定翻转对；样本一律人工判读。"""
    near, flip = [], []
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            a, b = pairs[i], pairs[j]
            if (b["ts"] - a["ts"]).total_seconds() > window_seconds:
                break
            na, nb = _norm(a["text"]), _norm(b["text"])
            sa, sb = _bigrams(na), _bigrams(nb)
            shared = len(sa & sb)
            if shared < 2:
                continue
            item = {
                "prev_ts": a["ts"].strftime("%H:%M:%S"), "prev": a["text"][:80],
                "cur_ts": b["ts"].strftime("%H:%M:%S"), "cur": b["text"][:80],
                "shared": shared, "sim": round(_jac(sa, sb), 2),
            }
            if shared >= 8 or item["sim"] >= 0.60:
                near.append(item)
            elif item["sim"] >= 0.30 and (bool(NEG_RE.search(na)) != bool(NEG_RE.search(nb))):
                flip.append(item)
    return {"near": near[:top], "near_total": len(near), "flip": flip[:top], "flip_total": len(flip)}


def accusation_hits(pairs, *, top=8):
    hits = []
    for idx, row in enumerate(pairs):
        if not ACCUSE_RE.search(row["inbound"]):
            continue
        prev = [p["text"][:60] for p in pairs[max(0, idx - 2):idx]]
        hits.append({
            "ts": row["ts"].strftime("%H:%M:%S"), "inbound": row["inbound"][:80],
            "prev_replies": prev, "reply": row["text"][:80],
        })
    return hits[:top], len(hits)


def task_chain(events, *, window_seconds=900, top=8):
    triggers = [e for e in events if e["event"] == "group_trigger_fired"]
    skips = [e for e in events if e["event"] == "tool_loop_skipped"]
    starts = []
    for e in events:
        raw = e["raw"]
        if e["event"] == "serious_signals" and (set(raw.get("signals") or []) & TASK_SIGNALS):
            starts.append({"ts": e["ts"], "chat_id": str(raw.get("chat_id") or ""),
                           "kind": "serious", "content": str(raw.get("content") or "")[:80]})
        elif e["event"] == "reply_intent_resolved" and str(raw.get("intent")) in TASK_INTENTS:
            starts.append({"ts": e["ts"], "chat_id": str(raw.get("chat_id") or ""),
                           "kind": "intent", "content": ""})
    alive = broken = unknown = 0
    samples = []
    for start in starts:
        nxt = next((t for t in triggers if str(t["raw"].get("chat_id") or "") == start["chat_id"]
                    and t["ts"] > start["ts"]
                    and (t["ts"] - start["ts"]).total_seconds() <= window_seconds), None)
        if nxt is None:
            unknown += 1
            continue
        near = next((s for s in skips if str(s["raw"].get("chat_id") or "") == start["chat_id"]
                     and abs((s["ts"] - nxt["ts"]).total_seconds()) <= 90), None)
        if near is not None:
            broken += 1
            if len(samples) < top:
                samples.append({
                    "start_ts": start["ts"].strftime("%H:%M:%S"), "start": start["content"],
                    "next_ts": nxt["ts"].strftime("%H:%M:%S"), "next_msg": str(nxt["raw"].get("question") or "")[:80],
                    "skip_reason": str(near["raw"].get("reason") or ""),
                })
        else:
            alive += 1
    return {"starts": len(starts), "alive": alive, "broken": broken, "unknown": unknown, "samples": samples}


def tool_false_open(events, *, window_seconds=120, top=8):
    ask_re = re.compile(TOOL_ASK_PATTERN)
    calls = [e for e in events if e["event"] == "agent_tool_call"]
    rounds = []
    seen = set()
    for c in calls:
        key = c["ts"].strftime("%Y%m%d%H%M")
        if key in seen:
            continue
        seen.add(key)
        rounds.append(c)
    suspected = []
    unknown = 0
    for c in rounds:
        trg = next((t for t in reversed(events) if t["event"] == "group_trigger_fired"
                    and t["ts"] <= c["ts"] and (c["ts"] - t["ts"]).total_seconds() <= window_seconds), None)
        if trg is None:
            unknown += 1
            continue
        sig = next((s for s in reversed(events) if s["event"] == "serious_signals"
                    and s["ts"] <= c["ts"] and (c["ts"] - s["ts"]).total_seconds() <= window_seconds), None)
        intent = next((r for r in reversed(events) if r["event"] == "reply_intent_resolved"
                       and r["ts"] <= c["ts"] and (c["ts"] - r["ts"]).total_seconds() <= window_seconds), None)
        msg = str(trg["raw"].get("question") if trg else "")
        has_ask = bool(ask_re.search(msg))
        has_task_sig = bool(sig and (set(sig["raw"].get("signals") or []) & TASK_SIGNALS))
        if has_ask or has_task_sig:
            continue
        suspected.append({
            "ts": c["ts"].strftime("%H:%M:%S"), "tool": str(c["raw"].get("tool") or ""),
            "msg": msg[:80],
            "intent": str(intent["raw"].get("intent") or "") if intent else "",
        })
    return {"opened": len(rounds), "suspected": len(suspected), "unknown": unknown, "samples": suspected[:top]}


def daily_rows(*, log_path, pairs_path, top=0):
    """按本地日分桶：给过去几天的读数（用于定门槛，不用等）。"""
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    pairs = load_pairs(pairs_path)
    events = load_events(log_path)
    days = sorted({(p["ts"] + offset).date() for p in pairs} | {(e["ts"] + offset).date() for e in events})
    rows = []
    for day in days:
        day_pairs = [p for p in pairs if (p["ts"] + offset).date() == day]
        day_events = [e for e in events if (e["ts"] + offset).date() == day]
        sim = similar_pairs(day_pairs, top=top)
        _, acc_total = accusation_hits(day_pairs, top=top)
        chain = task_chain(day_events, top=top)
        tools = tool_false_open(day_events, top=top)
        skipped = sum(1 for e in day_events if e["event"] == "tool_loop_skipped")
        rows.append({
            "day": day.isoformat(), "replies": len(day_pairs),
            "near": sim["near_total"], "flip": sim["flip_total"], "accuse": acc_total,
            "chain_starts": chain["starts"], "chain_alive": chain["alive"], "chain_broken": chain["broken"],
            "tool_opened": tools["opened"], "tool_suspected": tools["suspected"], "tool_skipped": skipped,
        })
    return rows


def _print_daily(rows):
    print("=== 行为指标 · 按天（本地日） ===")
    print("day         replies  近重复  翻转  指认  任务存活/断链  工具轮/疑似  skipped")
    for r in rows:
        print(f"{r['day']}  {r['replies']:>6}  {r['near']:>5}  {r['flip']:>4}  {r['accuse']:>4}  "
              f"{r['chain_alive']}/{r['chain_broken']:<3}  {r['tool_opened']:>3}/{r['tool_suspected']:<3}  {r['tool_skipped']:>3}")


def build_report(*, log_path, pairs_path, since_text, top):
    since = _parse_ts(since_text) if since_text else None
    pairs = load_pairs(pairs_path, since=since)
    events = load_events(log_path, since=since)
    similarity = similar_pairs(pairs, top=top)
    acc_samples, acc_total = accusation_hits(pairs, top=top)
    chain = task_chain(events, top=top)
    tools = tool_false_open(events, top=top)
    skipped = sum(1 for e in events if e["event"] == "tool_loop_skipped")
    return {
        "since": since_text or "",
        "replies": len(pairs),
        "inbounds": sum(1 for r in pairs if r["inbound"]),
        "similarity": similarity,
        "accusation": {"total": acc_total, "samples": acc_samples},
        "task_chain": chain,
        "tool_false_open": {**tools, "tool_loop_skipped": skipped},
    }


def _print(report):
    print("=== 行为指标 v1 ===")
    print(f"窗口：since={report['since'] or '(全部)'}；她回复 {report['replies']} 条（带入站 {report['inbounds']} 条）")
    s1 = report["similarity"]
    rate = (s1["near_total"] / report["replies"] * 100) if report["replies"] else 0.0
    print()
    print(f"[A1 近重复自我重复] {s1['near_total']} 对 / 100 条回复 = {rate:.1f}%")
    for s in s1["near"]:
        print(f"   {s['prev_ts']} -> {s['cur_ts']}  shared={s['shared']} sim={s['sim']}")
        print(f"     前：{s['prev']}")
        print(f"     后：{s['cur']}")
    print(f"[A2 否定翻转对] {s1['flip_total']} 对（低精度，人工判读）")
    for s in s1["flip"]:
        print(f"   {s['prev_ts']} -> {s['cur_ts']}  shared={s['shared']} sim={s['sim']}")
        print(f"     前：{s['prev']}")
        print(f"     后：{s['cur']}")
    a = report["accusation"]
    print(f"[A3 用户指认] {a['total']} 条")
    for s in a["samples"]:
        print(f"   {s['ts']} 入站：{s['inbound']}")
        print(f"     她前两条：{s['prev_replies']}")
        print(f"     她当条：{s['reply']}")
    t = report["task_chain"]
    base = t["alive"] + t["broken"]
    rate2 = (t["alive"] / base * 100) if base else 0.0
    print()
    print(f"[B 任务链] 启动 {t['starts']}；存活 {t['alive']} / 断链 {t['broken']} / 无后续 {t['unknown']}"
          f" -> 存活率 {rate2:.0f}%")
    for s in t["samples"]:
        print(f"   {s['start_ts']} 任务：{s['start']}")
        print(f"     {s['next_ts']} 下一句：{s['next_msg']}  skip={s['skip_reason']}")
    g = report["tool_false_open"]
    rate3 = (g["suspected"] / g["opened"] * 100) if g["opened"] else 0.0
    print()
    print(f"[C 工具误开] 开启 {g['opened']} 轮；疑似误开 {g['suspected']} = {rate3:.0f}%；"
          f"无对应触发 {g.get('unknown', 0)}；tool_loop_skipped={g['tool_loop_skipped']}")
    for s in g["samples"]:
        print(f"   {s['ts']} tool={s['tool']} intent={s['intent']} msg={s['msg']}")
    # D1/D2（近重复闸 / 断言台账闸）随 core.speech.repeat_guard 与 assertion_guard
    # 整模块退役（2026-10-24）：那两项都在替模型判"这句话和她刚说的像不像"。


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--pairs", default=DEFAULT_PAIRS)
    ap.add_argument("--since", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--by-day", action="store_true")
    args = ap.parse_args()
    if args.by_day:
        rows = daily_rows(log_path=args.log, pairs_path=args.pairs)
        if args.json:
            pathlib.Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        _print_daily(rows)
        return 0
    report = build_report(log_path=args.log, pairs_path=args.pairs, since_text=args.since, top=args.top)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
