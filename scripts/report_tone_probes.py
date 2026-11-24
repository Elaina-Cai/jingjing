# -*- coding: utf-8 -*-
"""P2 语气探针基线合格率报表（只读）。

语料 = 生产库 terminal_events 里 event_type=reply 的真实回复（口径同
core/speech/guards.py:479-486 的基线法：唯一源是生产库，不用
outbound_audit.content_preview）。分母、窗口、样本不足一律如实打印。

对 core/speech/tone_target.py 的 19 个探针逐条输出：
  触发率 / 语境合格率（代理判据，见下）/ 样本数 / 红绿判定
代理判据：命中的字面若落在引号内（引用别人的话）、被否定前缀包裹、或处在
那种话/别用/不说这类元语言上下文里，就不算她本人这么说；其余算 direct。
任务门槛：合格率 < 0.80 判红，命中样本 < 5 条判 n/a（不编数）。

再统计提示词内嵌写死台词（ENTRIES 的 text 引号片段 + 324-363 语素表）
在真实回复里的出现次数与占比。

用法：
  .\\.venv_it\\Scripts\\python.exe scripts\report_tone_probes.py
  .\\.venv_it\\Scripts\\python.exe scripts\report_tone_probes.py --json output\tone_probes.json

只读：sqlite 以 mode=ro 打开，不写库、不调 LLM、不改 tone_target / guards。
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import pathlib
import re
import sqlite3
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech import tone_target as tt  # noqa: E402

RED_BELOW = 0.80
MIN_SAMPLE = 5
_QUOTE_PAIRS = (
    ("\u300c", "\u300d"), ("\u201c", "\u201d"), ("\u300a", "\u300b"),
    ("\"", "\""), (chr(39), chr(39)),
)
_NEG_PREFIX = ("不", "没", "别", "无", "未", "非", "未必", "否则")
_META_MARKERS = ("这种话", "这类", "这种", "别用", "不说", "不许说", "禁", "所谓", "引用", "原话")
_QUOTED_FRAG_RE = re.compile(r"[\u300c\u201c\u300a]([^\u300d\u201d\u300a\n]{1,40})[\u300d\u201d\u300b]")


def _text_like(text: str) -> bool:
    return any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in text)


def load_corpus(meta_db: str, *, since: str, chat_type: str):
    info = {"meta_db": meta_db, "since": since, "chat_type": chat_type}
    uri = "file:%s?mode=ro" % pathlib.Path(meta_db).as_posix()
    con = sqlite3.connect(uri, uri=True)
    try:
        sql = "select id, occurred_at, chat_type, content from terminal_events where event_type = ?"
        args: list[str] = ["reply"]
        if since:
            sql += " and occurred_at >= ?"
            args.append(since)
        if chat_type:
            sql += " and chat_type = ?"
            args.append(chat_type)
        sql += " order by id"
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    excluded = collections.Counter()
    corpus = []
    for row_id, occurred_at, chat_type_value, content in rows:
        text = str(content or "")
        if text.lstrip().startswith("{"):
            excluded["json_tool_call"] += 1
            continue
        if "\"arguments\"" in text:
            excluded["tool_arguments"] += 1
            continue
        if not _text_like(text):
            excluded["non_text"] += 1
            continue
        corpus.append({
            "id": int(row_id),
            "at": str(occurred_at or ""),
            "chat_type": str(chat_type_value or ""),
            "raw": text,
            "body": tt._body(text),
        })
    info.update({
        "rows_total": len(rows),
        "corpus": len(corpus),
        "excluded": dict(excluded),
        "window": [corpus[0]["at"] if corpus else "", corpus[-1]["at"] if corpus else ""],
    })
    return corpus, info


def probe_literals(probe) -> dict:
    """从探针函数的 co_names 里取出它引用的模块级字面表 / 正则。"""
    words: list[str] = []
    regexes: list[str] = []
    for name in probe.__code__.co_names:
        value = getattr(tt, name, None)
        if isinstance(value, tuple) and value and all(isinstance(item, str) for item in value):
            words.extend(value)
        elif hasattr(value, "search") and hasattr(value, "pattern"):
            regexes.append(str(value.pattern))
    seen = set()
    uniq_words = [w for w in words if not (w in seen or seen.add(w))]
    return {"words": uniq_words, "regexes": regexes}


def match_span(body: str, literals: dict):
    best = None
    for word in literals["words"]:
        pos = body.find(word)
        if pos >= 0 and (best is None or pos < best[0]):
            best = (pos, pos + len(word), word)
    for pattern in literals["regexes"]:
        found = re.search(pattern, body)
        if found and (best is None or found.start() < best[0]):
            best = (found.start(), found.end(), found.group(0))
    return best


def _inside_quotes(body: str, pos: int) -> bool:
    for open_c, close_c in _QUOTE_PAIRS:
        start = body.rfind(open_c, 0, pos)
        if start < 0:
            continue
        if body.rfind(close_c, 0, pos) > start:
            continue
        if body.find(close_c, pos) > pos:
            return True
    return False


def context_of(body: str, span) -> str:
    if span is None:
        return "n/a"
    start, end, _matched = span
    if _inside_quotes(body, start):
        return "quoted"
    if any(part in body[max(0, start - 6):start] for part in _NEG_PREFIX):
        return "negated"
    wide = body[max(0, start - 12):start] + body[end:end + 12]
    if any(marker in wide for marker in _META_MARKERS):
        return "metalinguistic"
    return "direct"


def entry_class_map() -> dict:
    out: dict[str, list] = {}
    for entry in tt.ENTRIES:
        out.setdefault(str(entry.probe), []).append({
            "key": str(entry.key),
            "axis": str(entry.axis),
            "verdict": str(entry.verdict),
            "text": str(entry.text),
        })
    return out


def classify(probe_name: str, entries: dict) -> str:
    if probe_name.startswith("banned_"):
        return "banned"
    rows = entries.get(probe_name) or []
    verdicts = {row["verdict"] for row in rows}
    if tt.VERDICT_HAVE in verdicts:
        return "have"
    if tt.VERDICT_NOT in verdicts:
        return "banned"
    return "observer"


def scan_probes(corpus, *, samples: int) -> list:
    entries = entry_class_map()
    results = []
    for name, probe in tt.PROBES.items():
        literals = probe_literals(probe)
        fires = 0
        contexts = collections.Counter()
        word_hits = collections.Counter()
        samples_out = []
        for row in corpus:
            body = row["body"]
            try:
                fired = bool(probe(row["raw"]))
            except Exception:  # noqa: BLE001 - 读数不该炸
                fired = False
            if not fired:
                continue
            fires += 1
            for word in literals["words"]:
                if word in body:
                    word_hits[word] += 1
            span = match_span(body, literals)
            ctx = context_of(body, span)
            contexts[ctx] += 1
            if len(samples_out) < samples:
                snippet = re.sub(r"\s+", " ", body)[:70]
                samples_out.append({"id": row["id"], "ctx": ctx, "text": snippet})
        direct = contexts.get("direct", 0)
        rate = round(fires / len(corpus), 4) if corpus else "n/a"
        located = fires - contexts.get("n/a", 0)
        qualification = round(direct / located, 4) if located else "n/a"
        kind = classify(name, entries)
        if fires < MIN_SAMPLE or not located:
            verdict = "n/a"
        elif qualification < RED_BELOW:
            verdict = "red"
        else:
            verdict = "green"
        results.append({
            "probe": name,
            "class": kind,
            "fires": fires,
            "rate": rate,
            "contexts": dict(contexts),
            "qualification": qualification,
            "located": located,
            "verdict": verdict,
            "words": literals["words"],
            "regexes": literals["regexes"],
            "word_hits": dict(word_hits.most_common()),
            "entries": [row["key"] + "/" + row["verdict"] for row in (entries.get(name) or [])],
            "samples": samples_out,
        })
    results.sort(key=lambda item: (-item["fires"], item["probe"]))
    return results


def scan_canned(corpus) -> dict:
    """写死台词：ENTRIES 的 text 引号片段 + 324-363 语素表，在真实回复里的占比。"""
    fragments = collections.Counter()
    for entry in tt.ENTRIES:
        for frag in _QUOTED_FRAG_RE.findall(str(entry.text)):
            fragments[frag.strip()] += 1
    frag_hits = collections.Counter()
    frag_replies = 0
    for row in corpus:
        hit_any = False
        for frag in fragments:
            if frag and frag in row["raw"]:
                frag_hits[frag] += 1
                hit_any = True
        if hit_any:
            frag_replies += 1
    tables = {}
    table_hits = collections.Counter()
    for name in dir(tt):
        if not (name.startswith("_") and name.endswith("_WORDS")):
            continue
        value = getattr(tt, name)
        if not (isinstance(value, tuple) and value and all(isinstance(x, str) for x in value)):
            continue
        per_word = collections.Counter()
        for row in corpus:
            for word in value:
                if word in row["raw"]:
                    per_word[word] += 1
        tables[name] = {
            "words": len(value),
            "words_seen": len(per_word),
            "hits": sum(per_word.values()),
            "top": dict(per_word.most_common(5)),
        }
        table_hits.update(per_word)
    table_replies = 0
    for row in corpus:
        if any(word in row["raw"] for word in table_hits):
            table_replies += 1
    n = len(corpus)
    return {
        "entry_fragments": len(fragments),
        "entry_fragment_hits": dict(frag_hits.most_common()),
        "entry_fragment_replies": frag_replies,
        "entry_fragment_share": round(frag_replies / n, 4) if n else "n/a",
        "word_tables": tables,
        "word_table_words": sum(row["words"] for row in tables.values()),
        "word_table_words_seen": len(table_hits),
        "word_table_replies": table_replies,
        "word_table_share": round(table_replies / n, 4) if n else "n/a",
        "word_table_top": dict(table_hits.most_common(12)),
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="P2 语气探针基线（只读）")
    parser.add_argument("--meta-db", default=str(ROOT / "data" / "yinyue_meta.db"))
    parser.add_argument("--since", default="", help="occurred_at 字符串前缀，例如 2026-09-20")
    parser.add_argument("--chat-type", default="", help="group / private / 空=全部")
    parser.add_argument("--samples", type=int, default=6, help="每条探针附带的命中样例数")
    parser.add_argument("--json", default="", help="可选：把明细写到该路径")
    args = parser.parse_args()

    corpus, info = load_corpus(args.meta_db, since=args.since, chat_type=args.chat_type)
    probes = scan_probes(corpus, samples=args.samples)
    canned = scan_canned(corpus)
    red = [row["probe"] for row in probes if row["verdict"] == "red"]
    green = [row["probe"] for row in probes if row["verdict"] == "green"]
    unknown = [row["probe"] for row in probes if row["verdict"] == "n/a"]
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "corpus": info,
        "threshold": {"red_below": RED_BELOW, "min_sample": MIN_SAMPLE},
        "probes": probes,
        "canned": canned,
        "summary": {"red": red, "green": green, "n/a": unknown},
        "probe_count": len(probes),
    }
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "corpus: %d 条真实回复（reply 总 %d，剔除 %s）；窗口 %s → %s"
        % (info["corpus"], info["rows_total"], info["excluded"], info["window"][0], info["window"][1]),
        "probe_count=%d red=%d green=%d n/a=%d" % (len(probes), len(red), len(green), len(unknown)),
        "%-18s %-9s %6s %7s %7s %8s %7s %s" % ("probe", "class", "fires", "rate", "located", "qual", "verdict", "contexts"),
    ]
    for row in probes:
        lines.append("%-18s %-9s %6d %7s %7s %8s %7s %s"
                     % (row["probe"], row["class"], row["fires"], row["rate"], row["located"], row["qualification"],
                        row["verdict"], row["contexts"]))
    lines.append("red   = %s" % ", ".join(red))
    lines.append("green = %s" % ", ".join(green))
    lines.append("n/a   = %s" % ", ".join(unknown))
    lines.append("canned: ENTRIES 引号片段 %d 个命中 %d 条回复（占比 %s）；语素表 %d 词见过 %d 个，覆盖 %d 条（占比 %s）"
                 % (canned["entry_fragments"], canned["entry_fragment_replies"], canned["entry_fragment_share"],
                    canned["word_table_words"], canned["word_table_words_seen"],
                    canned["word_table_replies"], canned["word_table_share"]))
    lines.append("entry fragments top: %s" % json.dumps(canned["entry_fragment_hits"], ensure_ascii=False))
    lines.append("word tables top: %s" % json.dumps(canned["word_table_top"], ensure_ascii=False))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

