#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""角色质量探针：对 persona_probe 的结果 JSON 计算机械代理指标（只读）。

指标（全部可机械核对，不做语义判断；"像不像"的归因留给人工）：
  * 引用率      回复与"本轮输入上下文"的字符 bigram 重叠——她有没有接住对方话里的词；
  * 称呼        @对象是否属于本场景人物；是否 @ 到场景外的人；
  * 出戏泄漏    旁白/系统格式/元话术（（系统）、[SILENT]、窗口时间戳、AI 自曝等）；
  * 套话        canned（core.speech.guards 硬名单）+ servant（persona_probe 词表）；
  * 长度合规    句数是否落在 1~3（[基本表达] 契约）；
  * 跨 run 一致性   同场景两次采样的 bigram Jaccard（越高越模板化、越低越漂移）；
  * 跨场景重复  出现在 >=2 个场景的 3-gram（模板候选，防"一套话术走天下"）。

用法::
    python scripts/character_quality_probe.py --probe output/persona_probe/after_style_20260926_191434.json
    python scripts/character_quality_probe.py --probe A.json --probe B.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_META_PATTERNS = (
    re.compile(r"（系统）"),
    re.compile(r"\[SILENT\]", re.I),
    re.compile(r"\[\d{1,2}:\d{2}\]"),  # 窗口行格式泄漏
    re.compile(r"\[表情包[：:]"),  # 原始占位泄漏
    re.compile(r"\[引用"),
    re.compile(r"（旁白|（内心|（动作"),
    re.compile(
        r"作为(一个)?(AI|人工智能|语言模型)|我是(一个)?(AI|人工智能|语言模型)|大模型",
        re.I,
    ),
)
_SENT_SPLIT = re.compile(r"[。！？!?…]+")


def _load_persona_probe():
    spec = importlib.util.spec_from_file_location(
        "persona_probe_mod", ROOT / "scripts" / "persona_probe.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _bigrams(text) -> set[str]:
    s = re.sub(r"[^\w\u4e00-\u9fa5]", "", str(text or ""))
    return {s[i : i + 2] for i in range(max(0, len(s) - 1))}


def _ngrams(text, n: int = 3) -> list[str]:
    s = re.sub(r"[^\w\u4e00-\u9fa5]", "", str(text or ""))
    return [s[i : i + n] for i in range(max(0, len(s) - n + 1))]


def _context_text(case: dict) -> str:
    parts = [str(case.get("text") or "")]
    parts += [str(x) for x in (case.get("history") or [])]
    parts += [str(x) for x in (case.get("group_recent") or [])]
    return " ".join(parts)


def _scene_names(case: dict) -> set[str]:
    names: set[str] = set()
    sender = str(case.get("sender") or "").strip()
    if sender:
        names.add(sender)
    for row in case.get("group_recent") or []:
        m = re.match(r"^\s*([^:：]{1,12})[:：]", str(row))
        if m:
            names.add(m.group(1).strip())
    return names


def _mentions(text) -> list[str]:
    return [m.group(1) for m in re.finditer(r"@([\w\u4e00-\u9fa5]{1,16})", str(text or ""))]


def analyze_record(case: dict, rec: dict) -> dict:
    text = str(rec.get("final") or "")
    b_reply = _bigrams(text)
    b_ctx = _bigrams(_context_text(case))
    ref = (len(b_reply & b_ctx) / len(b_reply)) if b_reply else 0.0
    sentences = [s for s in _SENT_SPLIT.split(text) if s.strip()]
    n_sent = max(1, len(sentences))
    names = _scene_names(case)
    mentions = _mentions(text)
    return {
        "case": rec.get("case"),
        "run": rec.get("run"),
        "chars": len(text),
        "sentences": n_sent,
        "sentence_ok": 1 <= n_sent <= 3,
        "reference_rate": round(ref, 4),
        "mentions": mentions,
        "unknown_mentions": [m for m in mentions if m not in names],
        "meta_hits": [p.pattern for p in _META_PATTERNS if p.search(text)],
        "canned_hits": list(rec.get("canned_hits") or []),
        "servant_hits": list(rec.get("servant_hits") or []),
        "final": text,
    }


def analyze_payload(payload: dict, pp) -> list[dict]:
    by_id = {c["id"]: c for c in pp.CASES}
    out = []
    for rec in payload.get("results") or []:
        case = by_id.get(rec.get("case")) or {"id": rec.get("case")}
        out.append(analyze_record(case, rec))
    return out


def summarize(records: list[dict]) -> dict:
    n = len(records)

    def mean(vals):
        vals = list(vals)
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    by_case: dict[str, list[dict]] = {}
    for r in records:
        by_case.setdefault(str(r["case"]), []).append(r)
    sims: list[float] = []
    for rs in by_case.values():
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = _bigrams(rs[i]["final"]), _bigrams(rs[j]["final"])
                sims.append(len(a & b) / len(a | b) if (a | b) else 0.0)

    gram_cases: dict[str, set[str]] = {}
    for r in records:
        for g in set(_ngrams(r["final"], 3)):
            gram_cases.setdefault(g, set()).add(str(r["case"]))
    cross = {g: sorted(c) for g, c in gram_cases.items() if len(c) >= 2}
    top_cross = sorted(cross.items(), key=lambda kv: -len(kv[1]))[:10]

    return {
        "records": n,
        "reference_rate_mean": mean(r["reference_rate"] for r in records),
        "sentence_ok_rate": round(sum(1 for r in records if r["sentence_ok"]) / n, 4) if n else 0.0,
        "chars_mean": mean(r["chars"] for r in records),
        "unknown_mentions": sum(len(r["unknown_mentions"]) for r in records),
        "meta_leak_records": sum(1 for r in records if r["meta_hits"]),
        "canned_records": sum(1 for r in records if r["canned_hits"]),
        "servant_records": sum(1 for r in records if r["servant_hits"]),
        "cross_run_similarity_mean": mean(sims),
        "cross_case_ngrams": len(cross),
        "cross_case_top": [{"gram": g, "cases": c} for g, c in top_cross],
        "empty_final_records": sum(1 for r in records if not r["final"]),
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="角色质量探针（只读、机械指标）")
    parser.add_argument("--probe", action="append", default=[], help="persona_probe 结果 JSON（可多次）")
    parser.add_argument("--json", default="", help="合并报告输出路径")
    parser.add_argument("--out-dir", default="output/character_quality")
    args = parser.parse_args()

    if not args.probe:
        parser.error("至少给一个 --probe")
    pp = _load_persona_probe()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = {"at": datetime.now().isoformat(timespec="seconds"), "probes": []}
    print("角色质量探针（机械代理指标；只读）")
    print("=" * 96)
    header = (
        f"{'label':<14}{'n':>3}  {'引用率':>6}  {'句数OK':>6}  {'字数':>5}  "
        f"{'未知@':>4}  {'泄漏':>4}  {'canned':>6}  {'servant':>7}  {'跨run相似':>8}  {'跨场景3gram':>10}"
    )
    print(header)
    for path in args.probe:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        records = analyze_payload(payload, pp)
        summary = summarize(records)
        label = str(payload.get("label") or Path(path).stem)
        report["probes"].append({"path": str(path), "label": label, "summary": summary, "records": records})
        print(
            f"{label:<14}{summary['records']:>3}  {summary['reference_rate_mean']:>6}  "
            f"{summary['sentence_ok_rate']:>6}  {summary['chars_mean']:>5}  "
            f"{summary['unknown_mentions']:>4}  {summary['meta_leak_records']:>4}  "
            f"{summary['canned_records']:>6}  {summary['servant_records']:>7}  "
            f"{summary['cross_run_similarity_mean']:>8}  {summary['cross_case_ngrams']:>10}"
        )
        for item in summary["cross_case_top"][:3]:
            print(f"    · 跨场景 3-gram: {item['gram']!r}  出现在 {item['cases']}")
    print("=" * 96)
    out_path = Path(args.json) if args.json else out_dir / f"character_quality_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"report -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
