"""窗口预算扫描（task-9，只读）：13 道真题不变，只变窗口预算与原文尾条数。

复用 scripts/bench_replay.py 的 run 路径（_read_rows/_make_agent/_prompt_for/_chat/_grade），
只把 agent._group_recent_context 换成按 (budget_chars, full) 显式构造的窗口文本 ——
因为 core/context/chat_window.py:83-98 把 env 钳在 budget<=8000、full<=80，
本扫描要回答"多给原文能换多少"，所以对 build_window_parts 显式传参绕过钳制（报告里注明）。

实验进程内的一个补丁（不改仓库代码，报告 patches 字段留痕）：
--lift-cap：core/context/budget.py:37 DEFAULT_BUDGETS[HISTORY]=1200 + CUT_TAIL
   会把 session.group_recent 块从 3586/25797 字截到 1191 字（两档窗口逐字节相同）。
   默认开（lifted 臂）；asis 臂用 --no-lift-cap 保真现状。

必须走隔离库（run_isolated.ps1 会准备隔离副本与 TEMP）：
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_isolated.ps1
  scripts/bench_window_budget.py --runs 2 --tag lifted --report .test_tmp/window_budget/report.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("YINYUE_FEATURE_MEMORY_VECTOR_RECALL", "0")


def _load_bench_replay():
    path = ROOT / "scripts" / "bench_replay.py"
    spec = importlib.util.spec_from_file_location("bench_replay", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


BR = _load_bench_replay()


def set_window_cap(lift: bool) -> str:
    """lift=True: session.group_recent 块不受 DEFAULT_BUDGETS[HISTORY]=1200 截断。"""
    import core.context.budget as budget

    if not hasattr(budget, "_orig_budget_for"):
        budget._orig_budget_for = budget._budget_for
    orig = budget._orig_budget_for
    if lift:
        def patched(block):
            if getattr(block, "source", "") == "session.group_recent":
                return None
            return orig(block)

        budget._budget_for = patched
        return "lifted: session.group_recent 不受 HISTORY=1200 截断"
    budget._budget_for = orig
    return "asis: 保留 HISTORY=1200 CUT_TAIL 截断"


def parse_combo(text: str) -> tuple:
    left, _, right = str(text).strip().lower().partition("x")
    return int(left), int(right)


def window_text(chat_id: str, *, budget: int, full: int) -> str:
    from core.context.chat_window import build_window_parts

    try:
        return str(build_window_parts(chat_id, full=full, budget_chars=budget).text_full or "")
    except Exception as exc:
        return "[ERROR] %s: %s" % (type(exc).__name__, exc)


def env_clamps() -> dict:
    from core.context import chat_window

    return {
        "env_full": chat_window.window_full(),
        "env_budget": chat_window.window_budget(),
        "clamp_full_high": 80,
        "clamp_budget_high": 8000,
        "note": "core/context/chat_window.py:83-98 对 env 取值钳制；本扫描显式传参绕过",
    }


def window_block_chars(agent, snapshot, is_group: bool, wtext: str) -> dict:
    """用 assembler 的真实预算决策量窗口块落到 prompt 里多少字。"""
    import core.context.assembler as asm

    try:
        bundle, decisions = asm._build(
            snapshot, is_group=is_group, group_recent_context=wtext,
            context=getattr(agent, "_last_context", None)
        )
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}
    rows = [d for d in decisions if getattr(d, "source", "") == "session.group_recent"]
    return {
        "bundle_total": getattr(bundle, "total_chars", 0),
        "blocks": len(getattr(bundle, "blocks", [])),
        "decisions": [
            {
                "before": d.chars_before,
                "after": d.chars_after,
                "action": d.action,
                "omitted": d.omitted_chars,
            }
            for d in rows
        ],
        "sizes": {b.source: len(b.content) for b in getattr(bundle, "blocks", [])},
    }


def prompt_sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def aggregate(per_item: list) -> dict:
    runs = [r for it in per_item for r in it.get("runs", [])]
    n = len(runs)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "exact_count": sum(1 for r in runs if r["exact"]),
        "exact_rate": round(sum(1 for r in runs if r["exact"]) / n, 4),
        "recall_mean": round(sum(r["token_recall"] for r in runs) / n, 4),
        "admission_rate": round(sum(1 for r in runs if r["admission"]) / n, 4),
        "declined_rate": round(sum(1 for r in runs if r.get("declined")) / n, 4),
        "halluc_mean": round(sum(len(r["halluc_tokens"]) for r in runs) / n, 4),
        "chars_mean": round(sum(r["chars"] for r in runs) / n, 2),
        "prompt_chars_mean": round(sum(it.get("prompt_chars", 0) for it in per_item) / len(per_item), 1),
        "prompt_chars_total": sum(it.get("prompt_chars", 0) for it in per_item),
        "fact_in_prompt_rate": round(sum(1 for it in per_item if it.get("fact_in_prompt")) / len(per_item), 4),
    }


def run(args) -> int:
    patches = {"window_cap": set_window_cap(not args.no_lift_cap)}
    items = json.loads(Path(args.items).read_text(encoding="utf-8"))
    if args.item_ids:
        wanted = {s.strip() for s in str(args.item_ids).split(",") if s.strip()}
        items = [it for it in items if it["item_id"] in wanted]
    if args.limit_items:
        items = items[: int(args.limit_items)]
    combos = [parse_combo(c) for c in str(args.combos).split(",") if c.strip()]
    out_path = Path(args.report)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows_cache: dict = {}
    agent_cache: dict = {}
    window_cache: dict = {}
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tag": args.tag,
        "patches": patches,
        "items_file": str(args.items),
        "item_ids": [it["item_id"] for it in items],
        "items": len(items),
        "runs_per_combo": int(args.runs),
        "span": int(args.span),
        "env_clamps": env_clamps(),
        "env": {
            "YINYUE_CHAT_WINDOW_BUDGET": os.environ.get("YINYUE_CHAT_WINDOW_BUDGET", ""),
            "YINYUE_CHAT_WINDOW_FULL": os.environ.get("YINYUE_CHAT_WINDOW_FULL", ""),
            "META_DB_PATH": os.environ.get("META_DB_PATH", ""),
        },
        "combos": [],
    }
    for budget, full in combos:
        combo = {"budget": budget, "full": full, "per_item": [], "window_chars": {}, "window_blocks": {}}
        started = time.time()
        for item in items:
            chat = item["chat_id"]
            if chat not in rows_cache:
                rows_cache[chat] = BR._read_rows(chat)
            rows = rows_cache[chat]
            key = (chat, budget, full)
            if key not in window_cache:
                window_cache[key] = window_text(chat, budget=budget, full=full)
            wtext = window_cache[key]
            combo["window_chars"][chat] = len(wtext)
            if chat not in agent_cache:
                agent_cache[chat] = BR._make_agent(
                    chat, item["question"], sender=args.sender, owner=bool(args.owner)
                )
            agent, snapshot = agent_cache[chat][0], agent_cache[chat][1]
            if agent is None:
                combo["per_item"].append({"item_id": item["item_id"], "error": "no_inbound_rows"})
                continue
            is_group = chat.endswith("@chatroom")
            if chat not in combo["window_blocks"]:
                combo["window_blocks"][chat] = window_block_chars(agent, snapshot, is_group, wtext)
            agent._inbound_content = item["question"]
            agent._group_recent_context = wtext
            base = BR._prompt_for(agent, snapshot, is_group)
            runs = []
            for _ in range(max(1, int(args.runs))):
                try:
                    ans = asyncio.run(BR._chat(agent, base, item["question"]))
                except Exception as exc:
                    ans = "[ERROR] %s: %s" % (type(exc).__name__, exc)
                runs.append(BR._grade(ans, item, rows))
            entry = {
                "item_id": item["item_id"],
                "chat_id": chat,
                "kind": item["kind"],
                "category": item["category"],
                "question": item["question"],
                "expect_tokens": item["expect_tokens"],
                "prompt_chars": len(base),
                "prompt_sha": prompt_sha(base),
                "window_chars": len(wtext),
                "window_block": combo["window_blocks"][chat].get("decisions"),
                "fact_in_prompt": all(BR._norm(t) in BR._norm(base) for t in item["expect_tokens"]),
                "runs": runs,
            }
            combo["per_item"].append(entry)
            detail = " | ".join(
                "exact=%s recall=%.2f h=%d chars=%d"
                % (r["exact"], r["token_recall"], len(r["halluc_tokens"]), r["chars"])
                for r in runs
            )
            print(
                "[%dx%d] %s prompt=%d sha=%s fact_in_prompt=%s %s"
                % (
                    budget,
                    full,
                    item["item_id"][-22:],
                    len(base),
                    entry["prompt_sha"],
                    entry["fact_in_prompt"],
                    detail,
                ),
                flush=True,
            )
        combo["agg"] = aggregate(combo["per_item"])
        combo["seconds"] = round(time.time() - started, 1)
        report["combos"].append(combo)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            "[%dx%d] agg=%s (%ss)"
            % (budget, full, json.dumps(combo["agg"], ensure_ascii=False), combo["seconds"]),
            flush=True,
        )

    if report["combos"]:
        base_agg = report["combos"][0]["agg"]
        for combo in report["combos"]:
            agg = combo["agg"]
            chars_delta = agg["prompt_chars_total"] - base_agg["prompt_chars_total"]
            exact_delta = agg["exact_count"] - base_agg["exact_count"]
            combo["gain_vs_first"] = {
                "exact_delta": exact_delta,
                "chars_delta": chars_delta,
                "exact_per_1000_chars": (
                    round(exact_delta / (chars_delta / 1000.0), 4) if chars_delta else "n/a"
                ),
            }
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("report -> %s" % out_path, flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="窗口预算扫描（只读，需隔离库）")
    parser.add_argument("--items", default=str(ROOT / "bench" / "items.json"))
    parser.add_argument("--item-ids", default="", help="逗号分隔的 item_id 过滤")
    parser.add_argument("--combos", default="3600x40,3600x120,10800x40,10800x120,36000x40,36000x120")
    parser.add_argument("--runs", type=int, default=2, help="每个组合几遍（温度 0.4）")
    parser.add_argument("--limit-items", type=int, default=0)
    parser.add_argument("--span", type=int, default=6)
    parser.add_argument("--sender", default="")
    parser.add_argument("--owner", action="store_true")
    parser.add_argument("--tag", default="", help="本臂标签，写进报告")
    parser.add_argument("--no-lift-cap", action="store_true", help="asis 臂：保留 HISTORY=1200 截断")
    parser.add_argument("--report", default=str(ROOT / ".test_tmp" / "window_budget" / "report.json"))
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
