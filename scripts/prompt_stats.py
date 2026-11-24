"""看提示词的账：形状对不对、每段有没有出现、她说了多少、重复多少。

用法：
  python scripts/prompt_stats.py            # 最近 1 天
  python scripts/prompt_stats.py --days 3
  python scripts/prompt_stats.py --json
读 data/prompt/<turn_id>/request.json（真发出去的请求）与 data/loop/trace-*.jsonl（结果与行为）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROMPT_DIR = ROOT / "data" / "prompt"
TRACE_DIR = ROOT / "data" / "loop"

E1_FRAG = "以下是这个世界里发生的事"
DAILY = ("[上一拍 · 最近", "这三天的账", "我的小本本",
         "还没销账的承诺", "我最近开口")
# 「未完的事」的**标题**在设计上不进 prompt（`_reshape` 只取值，见方案文档 E-16），
# 能匹配到的只有内容自带的提示行，所以这里放渲染文本，不放账本名。
EXTRA = ("群里", "我还记得", "我手上还没了结的事", E1_FRAG)
SKIPPED = {"turns": 0, "trace": 0}


def _shape(sysmsg: str, user1: str) -> str:
    if any(t in sysmsg for t in DAILY):
        return "v2"
    if any(t in user1 for t in DAILY):
        return "v1"
    return "unknown"


def load_turns(path: Path, *, days: int = 1) -> list[dict]:
    if not path.exists():
        return []
    cut = datetime.now() - timedelta(days=days)
    out: list[dict] = []
    for d in path.iterdir():
        if not d.is_dir() or not (d / "request.json").exists():
            continue
        if datetime.fromtimestamp(d.stat().st_mtime) < cut:
            continue
        try:
            j = json.loads((d / "request.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            SKIPPED["turns"] += 1
            continue
        rounds = j.get("rounds") or []
        ms = (rounds[0].get("messages") or []) if rounds else []
        sysmsg = ms[0]["content"] if ms and ms[0].get("role") == "system" else ""
        user1 = ms[1]["content"] if len(ms) > 1 else ""
        out.append({
            "turn": d.name,
            "at": datetime.fromtimestamp(d.stat().st_mtime).strftime("%m-%d %H:%M"),
            "rounds": len(rounds),
            "sys": len(sysmsg),
            "user": len(user1),
            "tools": sum(len(r.get("tools") or []) for r in rounds),
            "shape": _shape(sysmsg, user1),
            "daily": [t for t in DAILY if t in sysmsg or t in user1],
            "extra": [t for t in EXTRA if t in sysmsg or t in user1],
            "e1": E1_FRAG in (sysmsg + user1),
        })
    return sorted(out, key=lambda r: r["at"])


def load_trace(path: Path, *, days: int = 1) -> list[dict]:
    rows: list[dict] = []
    cut = datetime.now() - timedelta(days=days)
    if not path.exists():
        return rows
    for f in sorted(path.glob("trace-*.jsonl")):
        try:
            day = datetime.strptime(f.stem.replace("trace-", ""), "%Y-%m-%d")
        except ValueError:
            continue
        if day < cut:
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    SKIPPED["trace"] += 1
                    continue
    return rows


def summarize(turns: list[dict], trace: list[dict]) -> dict:
    said, silent, blocked, empty = [], 0, 0, 0
    texts: list[str] = []
    for r in trace:
        o = r.get("outcome") or {}
        got = [str(x) for x in (o.get("said") or [])]
        if o.get("silent") is None:
            empty += 1   # 这一拍没轮到任何窗口：不是“她选择不说”
        elif o.get("silent"):
            silent += 1
        blocked += len(o.get("blocked") or [])
        if got:
            said.append(len(got))
            texts.extend(got)
    why_empty, why_hit = 0, {}
    for r in trace:
        o = r.get("outcome") or {}
        if not o.get("silent"):
            continue
        w = str(o.get("silent_why") or "").strip()
        if not w:
            why_empty += 1
            continue
        key = re.sub(r"\s+", "", w)[:24]
        why_hit[key] = why_hit.get(key, 0) + 1
    dup = 0
    seen: dict[str, int] = {}
    for i, t in enumerate(texts):
        key = re.sub(r"\s+", "", t)[:40]
        if key and key in seen and i - seen[key] <= 5:
            dup += 1
        seen[key] = i
    shapes: dict[str, int] = {}
    for t in turns:
        shapes[t["shape"]] = shapes.get(t["shape"], 0) + 1
    n = max(len(turns), 1)
    return {
        "turns": len(turns),
        "shape": shapes,
        "sys_avg": round(sum(t["sys"] for t in turns) / n),
        "user_avg": round(sum(t["user"] for t in turns) / n),
        "chars_avg": round(sum(t["sys"] + t["user"] for t in turns) / n),
        "rounds_avg": round(sum(t["rounds"] for t in turns) / n, 1),
        "daily_rate": round(sum(1 for t in turns if t["daily"]) / n, 2),
        "extra_rate": round(sum(1 for t in turns if t["extra"]) / n, 2),
        "e1_rate": round(sum(1 for t in turns if t["e1"]) / n, 2),
        "trace_turns": len(trace),
        "said_turns": len(said),
        "silent_turns": silent,
        "empty_turns": empty,
        "blocked": blocked,
        "say_avg": round(sum(said) / len(said), 1) if said else 0,
        "dup": dup,
        "silent_why_empty": why_empty,
        "silent_why_top": sorted(why_hit.items(), key=lambda kv: -kv[1])[:3],
        "skipped": dict(SKIPPED),
    }


def render(res: dict) -> str:
    return "\n".join([
        "turns=%s shape=%s" % (res["turns"], res["shape"]),
        "chars avg=%s (sys %s / user %s) rounds avg=%s"
        % (res["chars_avg"], res["sys_avg"], res["user_avg"], res["rounds_avg"]),
        "sections: daily %s / extra %s / e1 %s" % (res["daily_rate"], res["extra_rate"], res["e1_rate"]),
        "behavior: trace %s / said %s / silent %s / blocked %s / say_avg %s / dup %s"
        % (res["trace_turns"], res["said_turns"], res["silent_turns"], res["blocked"], res["say_avg"], res["dup"]),
        "silent_why: 空 %s / 前三条 %s" % (res["silent_why_empty"], res["silent_why_top"]),
        "empty: %s（这一拍没轮到任何窗口，不是她选择不说）" % res["empty_turns"],
        "skipped: %s" % (res["skipped"],),
    ])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="提示词与行为的账")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--dir", default=str(PROMPT_DIR))
    ap.add_argument("--trace", default=str(TRACE_DIR))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = summarize(load_turns(Path(a.dir), days=a.days), load_trace(Path(a.trace), days=a.days))
    print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else render(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
