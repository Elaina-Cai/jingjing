# -*- coding: utf-8 -*-
"""盲评材料：把真实模型产出的两版回复随机成 A/B，事后再算偏好率。

流程
----
1. ``python evals/expression/harness.py run --split holdout --variant baseline --real --repeats 2``
   （``full`` 同样跑一遍；需 ``YINYUE_EXPRESSION_REAL=1``）
2. ``python evals/expression/review.py build --split holdout [--run-id <id>]``
   → ``.test_tmp/expression/blind/holdout.jsonl``（给评审看的，无版本名）
   → ``.test_tmp/expression/blind/holdout-key.json``（别给评审看）
3. 评审逐条填 ``prefer`` / 五项 1-5 分 / ``reason`` / ``critical``，存成 votes.jsonl
4. ``python evals/expression/review.py score --split holdout --votes <路径>``

配对与计分（2026-11-17 A1-4）
-----------------------------
- 只配**同一份冻结输入、同一模型/供应商、同一参数、同一 repeat** 的两版记录；
  模型回落、超时、报错、缺样的样本不进有效对照，并逐条给原因。
- 同一 (场景, 变体, repeat) 出现多条 → 判重复，不合并、不取平均。
- 投票按 (评审人, 案例) 去重；重复票单独计数，不进偏好率。
- 平票是有效结果但不算「候选赢」；格式坏/案例不存在的票记无效票。
- 关键事实、隐私、越权三类错误按案例逐条记；**维度均分不能抵消它们**，
  有一条就不过门槛。
- 多轮包按原顺序保留每一轮的说话与沉默（没开口写「（未开口）」），不吞轮次。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUNS = ROOT / ".test_tmp" / "expression" / "runs"
BLIND = ROOT / ".test_tmp" / "expression" / "blind"
DIMENSIONS = ("贴合情境", "口语自然", "人格连续", "不套句式", "信息完整")
CRITICAL = ("事实", "隐私", "权限")
PAIR_FIELDS = ("model", "provider", "temperature")


def _records_in(directory: Path) -> list[dict]:
    out = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith("-key.json"):
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏文件不该冒充样本
            continue
        if "scenario" in rec and "variant" in rec:
            rec["_path"] = str(path)
            out.append(rec)
    return out


def _run_dirs() -> list[Path]:
    if not RUNS.exists():
        return []
    return sorted((p for p in RUNS.iterdir() if p.is_dir()), key=lambda p: p.name)


def select_records(run_id: str = "") -> tuple[list[dict], str, list[str]]:
    """读一个 run 目录；不指定就取最新一个同时含 baseline/full 记录的。"""
    notes: list[str] = []
    if run_id:
        # 允许 "a,b"：baseline 与 full 常分两次跑、落在两个 run 目录里。
        ids = [x.strip() for x in str(run_id).split(",") if x.strip()]
        recs: list[dict] = []
        for rid in ids:
            d = RUNS / rid
            if not d.is_dir():
                notes.append("run 目录不存在：%s" % d)
                continue
            recs.extend(_records_in(d))
        return recs, ",".join(ids), notes
    for d in reversed(_run_dirs()):
        recs = _records_in(d)
        variants = {str(r.get("variant") or "") for r in recs}
        if {"baseline", "full"} <= variants:
            return recs, d.name, notes
    return [], "", ["没有同时含 baseline/full 的 run 目录；先跑 harness.py run --real"]


def _pair_identity(rec: dict) -> dict:
    model = rec.get("model") or {}
    return {
        "model": str(model.get("model") or ""),
        "provider": str(model.get("provider") or ""),
        "temperature": model.get("temperature"),
        "frozen": rec.get("frozen") or {},
    }


def _scene_context(scenario: dict) -> dict:
    if scenario.get("kind") == "multi":
        rows = [{"speaker": "对方", "text": str(t)} for t in (scenario.get("turns") or [])]
    else:
        rows = [{"speaker": "我" if x.get("role") == "assistant" else "对方",
                 "text": str(x.get("text") or "")} for x in (scenario.get("history") or [])]
        rows.append({"speaker": "对方", "text": str(scenario.get("current") or "")})
    if scenario.get("attachment_hint"):
        rows[-1]["text"] = "[图片: %s] %s" % (scenario["attachment_hint"], rows[-1]["text"])
    return {"chat": "群" if str(scenario.get("chat") or "").endswith("@chatroom") else "私聊",
            "addressed": (str(scenario.get("chat") or "") == "private"
                          or bool(scenario.get("addressed", False))),
            "rows": rows}


def _variant_text(rec: dict) -> str:
    """把一版的实际输出摊平。多轮保留轮次与「未开口」，不许悄悄丢。"""
    from evals.expression.harness import visible_messages

    turns = rec.get("turns") or []
    replies = rec.get("replies") or []
    texts = []
    for i in range(max(len(turns), len(replies))):
        turn = turns[i] if i < len(turns) else {}
        fallback = replies[i] if i < len(replies) else ""
        texts.append("\n".join(visible_messages(turn, fallback)) or "（未开口）")
    if rec.get("kind") != "multi":
        return texts[0] if texts else "（未开口）"
    return "\n".join("第%d轮：%s" % (i, text) for i, text in enumerate(texts, 1))


def cmd_build(args) -> int:
    from evals.expression.harness import load_scenarios

    records, run_id, notes = select_records(args.run_id)
    scenarios = {s["id"]: s for s in load_scenarios(args.split)}
    rng = random.Random(args.seed)
    BLIND.mkdir(parents=True, exist_ok=True)
    packet = BLIND / ("%s.jsonl" % args.split)
    key_path = BLIND / ("%s-key.json" % args.split)

    by_key: dict[tuple[str, str, int], list[dict]] = {}
    non_real = 0
    for rec in records:
        # 脚本模型/装配捕获不能进盲评包：review 只处理真实模型实际生成的文本。
        if str((rec.get("model") or {}).get("adapter") or "") != "RealLLM":
            non_real += 1
            continue
        if str(rec.get("split") or args.split) != args.split:
            continue
        if str(rec.get("scenario")) not in scenarios:
            continue
        k = (str(rec.get("scenario")), str(rec.get("variant")),
             int(rec.get("repeat") or 0))
        by_key.setdefault(k, []).append(rec)

    items: dict = {}
    rows: list[dict] = []
    skipped: list[dict] = []
    total = 0
    for sid, scenario in scenarios.items():
        repeats = sorted({k[2] for k in by_key if k[0] == sid})
        total += len(repeats) or 1
        if not repeats:
            skipped.append({"id": sid, "reason": "not_real_model_only" if non_real else "missing_both"})
            continue
        for rep in repeats:
            left = by_key.get((sid, "baseline", rep)) or []
            right = by_key.get((sid, "full", rep)) or []
            tag = "%s#r%d" % (sid, rep)
            if len(left) > 1 or len(right) > 1:
                skipped.append({"id": tag, "reason": "duplicate",
                                "detail": "baseline=%d full=%d" % (len(left), len(right))})
                continue
            if not left:
                skipped.append({"id": tag, "reason": "missing_baseline"})
                continue
            if not right:
                skipped.append({"id": tag, "reason": "missing_full"})
                continue
            a_rec, b_rec = left[0], right[0]
            if not a_rec.get("valid", True) or not b_rec.get("valid", True):
                skipped.append({"id": tag, "reason": "invalid_run",
                                "detail": "%s | %s" % (a_rec.get("invalid_reason") or "",
                                                       b_rec.get("invalid_reason") or "")})
                continue
            if _pair_identity(a_rec) != _pair_identity(b_rec):
                skipped.append({"id": tag, "reason": "not_paired",
                                "detail": "模型/供应商/参数/冻结输入不一致"})
                continue
            a, b = _variant_text(a_rec), _variant_text(b_rec)
            if not a and not b:
                skipped.append({"id": tag, "reason": "no_output"})
                continue
            flip = rng.random() < 0.5
            item_id = "%s-r%d" % (sid, rep)
            items[item_id] = {
                "A": "baseline" if flip else "full",
                "B": "full" if flip else "baseline",
                "scenario": sid, "repeat": rep,
                "tags": scenario.get("tags") or [],
                "focus": scenario.get("pair") or scenario.get("focus") or "",
            }
            rows.append({"id": item_id,
                         "context": _scene_context(scenario),
                         "A": a if flip else b,
                         "B": b if flip else a})
    meta = {"split": args.split, "run_id": run_id, "seed": args.seed,
            "paired": len(rows), "expected": total,
            "coverage": round(len(rows) / total, 3) if total else 0.0,
            "skipped": skipped, "notes": notes, "non_real_records": non_real,
            "pair_fields": list(PAIR_FIELDS)}
    if not rows:
        print("没有可打包的真实模型输出。")
        print("run=%s non_real=%d skipped=%s" % (run_id or "-", non_real, json.dumps(skipped[:8], ensure_ascii=False)))
        return 2
    packet.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                      encoding="utf-8")
    key_path.write_text(json.dumps({"_meta": meta, "items": items},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote %d items -> %s" % (len(rows), packet))
    print("coverage %d/%d (%.1f%%) run=%s" % (len(rows), total, 100.0 * len(rows) / total, run_id))
    print("key -> %s（不要给评审）" % key_path)
    if skipped:
        print("skipped: %s" % json.dumps(skipped[:12], ensure_ascii=False))
    return 0


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


def _critical_of(vote: dict, letter: str) -> dict:
    raw = (vote.get("critical") or {}).get(letter) or {}
    if isinstance(raw, list):
        return {k: 1 for k in raw if k in CRITICAL}
    return {k: int(raw.get(k) or 0) for k in CRITICAL if int(raw.get(k) or 0) > 0}


def cmd_score(args) -> int:
    key_raw = json.loads((BLIND / ("%s-key.json" % args.split)).read_text(encoding="utf-8"))
    if "items" in key_raw:
        key, meta = key_raw["items"], key_raw.get("_meta") or {}
    else:  # 旧格式（扁平 item_id -> A/B）
        key, meta = key_raw, {}
    votes = [json.loads(line) for line in Path(args.votes).read_text(encoding="utf-8").splitlines()
             if line.strip()]
    tally = {"candidate": 0, "baseline": 0, "tie": 0}
    invalid: list[dict] = []
    duplicates: list[str] = []
    seen: dict[tuple[str, str], dict] = {}
    scores = {v: {d: [] for d in DIMENSIONS} for v in ("baseline", "full")}
    critical = {v: {k: 0 for k in CRITICAL} for v in ("baseline", "full")}
    critical_cases: list[dict] = []
    reasons: list[dict] = []
    for vote in votes:
        item_id = str(vote.get("id") or "")
        reviewer = str(vote.get("reviewer") or "anon")
        if item_id not in key:
            invalid.append({"id": item_id, "reviewer": reviewer, "reason": "unknown_case"})
            continue
        if (reviewer, item_id) in seen:
            duplicates.append("%s@%s" % (item_id, reviewer))
            continue
        seen[(reviewer, item_id)] = vote
        side = str(vote.get("prefer") or "").strip().lower()
        if side in ("a", "b"):
            winner = key[item_id][side.upper()]
            tally["candidate" if winner == "full" else "baseline"] += 1
        elif side in ("tie", "平", "平票", ""):
            if side == "":
                invalid.append({"id": item_id, "reviewer": reviewer, "reason": "missing_prefer"})
                continue
            tally["tie"] += 1
        else:
            invalid.append({"id": item_id, "reviewer": reviewer, "reason": "bad_prefer:%s" % side})
            continue
        for letter in ("A", "B"):
            version = key[item_id][letter]
            got = (vote.get("scores") or {}).get(letter) or {}
            for dim in DIMENSIONS:
                try:
                    scores[version][dim].append(float(got.get(dim)))
                except (TypeError, ValueError):
                    pass
            for kind, n in _critical_of(vote, letter).items():
                critical[version][kind] += n
                if n:
                    critical_cases.append({"id": item_id, "side": letter,
                                           "version": version, "kind": kind, "count": n})
        if vote.get("reason"):
            reasons.append({"id": item_id, "reason": vote["reason"]})
    decided = tally["candidate"] + tally["baseline"]
    rate = round(tally["candidate"] / decided, 3) if decided else None
    critical_any = {v: sum(x.values()) for v, x in critical.items()}
    coverage = float(meta.get("coverage") or 0.0)
    out = {
        "votes": len(votes),
        "deduped_votes": len(seen),
        "duplicate_votes": duplicates,
        "invalid_votes": invalid,
        "tally": tally,
        "candidate_preference_rate_excluding_ties": rate,
        "scores": {v: {d: _mean(xs) for d, xs in dims.items()} for v, dims in scores.items()},
        "critical_errors": critical,
        "critical_cases": critical_cases,
        "coverage": coverage,
        "run_id": meta.get("run_id") or "",
        "reasons": reasons,
        "gate": {
            "preference_ge_65pct": (rate is not None and rate >= 0.65),
            "info_complete_not_worse": (
                _mean(scores["full"]["信息完整"]) is not None
                and _mean(scores["baseline"]["信息完整"]) is not None
                and _mean(scores["full"]["信息完整"]) >= _mean(scores["baseline"]["信息完整"])),
            "min_decided": decided >= args.min_decided,
            "coverage_complete": coverage >= float(args.min_coverage),
            "no_critical_errors": critical_any["full"] == 0,
            "no_duplicate_votes": not duplicates,
        },
        "note": "AI 代评只能算初筛；同一模型两轮打分不是两个独立真人。"
                "维度均分不抵消关键事实/隐私/权限错误；样本过少、缺样或平局过多时结论标证据不足。",
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="表达改造盲评")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build")
    p.add_argument("--split", default="holdout", choices=("dev", "holdout", "ref"))
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--run-id", default="")
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("score")
    p.add_argument("--split", default="holdout", choices=("dev", "holdout", "ref"))
    p.add_argument("--votes", required=True)
    p.add_argument("--min-decided", type=int, default=8)
    p.add_argument("--min-coverage", type=float, default=1.0)
    p.set_defaults(func=cmd_score)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
