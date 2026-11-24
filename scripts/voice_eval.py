# -*- coding: utf-8 -*-
# 语体体检：把"像不像人"拆成能算的数。只观测，不改任何 prompt。
#
#   python scripts\voice_eval.py corpus             # 她真实发出去的话的基线
#   python scripts\voice_eval.py compare            # 她 vs 真人（同一批会话里的入站消息）
#   python scripts\voice_eval.py prompt FILE        # 一份卡/提示词的 示范:分析 比
#
# 为什么要有 compare：绝对值没有意义，只有和"同一批人在同一批群里怎么说话"对照才有意义。
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "world.db").replace("\\", "/")
OUT_DIR = os.path.join(ROOT, ".test_tmp")

SENT = re.compile(r"[。！？!?…\n]+")
PUNCT = re.compile(r"[\s，。！？!?…、；;：:\"'“”‘’（）()\[\]{}~～—\-]+")


def sentences(text):
    return [s.strip() for s in SENT.split(text or "") if s.strip()]


def metrics(texts):
    n_msg = len(texts)
    chars = sum(len(t) for t in texts)
    sents = [s for t in texts for s in sentences(t)]
    n_sent = max(1, len(sents))
    joined = "".join(texts)
    grams = [joined[i:i + 2] for i in range(max(0, len(joined) - 1))]
    open2 = [PUNCT.sub("", t)[:2] for t in texts if PUNCT.sub("", t)]
    top_open = max((open2.count(k) for k in set(open2)), default=0)
    return {
        "msgs": n_msg,
        "chars": chars,
        "sentences": len(sents),
        "avg_msg_len": round(chars / n_msg, 1) if n_msg else 0,
        "avg_sent_len": round(sum(len(s) for s in sents) / n_sent, 1),
        "sent_per_msg": round(len(sents) / max(1, n_msg), 2),
        "short_ratio": round(sum(1 for s in sents if len(s) <= 6) / n_sent, 3),
        "q_ratio": round(sum(1 for t in texts if "?" in t or "？" in t) / max(1, n_msg), 3),
        "exclaim_ratio": round(sum(1 for t in texts if "!" in t or "！" in t) / max(1, n_msg), 3),
        "ellipsis_ratio": round(sum(1 for t in texts if "…" in t or "。。" in t or "..." in t) / max(1, n_msg), 3),
        "comma_per_sent": round(len(re.findall(r"[，,]", joined)) / n_sent, 2),
        "bigram_ttr": round(len(set(grams)) / max(1, len(grams)), 3),
        "top_open_share": round(top_open / max(1, len(open2)), 3),
    }


def _conn():
    return sqlite3.connect("file:%s?mode=ro" % DB, uri=True)


def her(limit=3000):
    con = _conn()
    rows = con.execute("select text from outbound_receipt where length(text)>0 "
                       "order by id desc limit ?", (limit,)).fetchall()
    con.close()
    return [r[0].strip() for r in rows if r[0] and r[0].strip()]


def human(limit=20000):
    """入站消息里"人打的字"：去掉 [图片]/[表情] 这类占位、去掉她自己的号。"""
    con = _conn()
    rows = con.execute("select text from inbound_event where length(text)>0 "
                       "order by id desc limit ?", (limit,)).fetchall()
    con.close()
    out = []
    for (t,) in rows:
        t = (t or "").strip()
        if not t or t.startswith("["):
            continue
        out.append(t)
    return out


def prompt_report(fp):
    src = open(fp, encoding="utf-8").read()
    pts = re.findall(r"\*\*要点\*\*：(.+)", src)
    demo = re.findall(r"[「“]([^」”]{2,80})[」”]", src)
    a, d = sum(len(p) for p in pts), sum(len(q) for q in demo)
    return {"file": os.path.relpath(fp, ROOT).replace("\\", "/"), "chars": len(src),
            "n_points": len(pts), "analysis_chars": a, "n_quotes": len(demo), "demo_chars": d,
            "demo_per_analysis": round(d / a, 3) if a else None}


def compare():
    h, me = her(), human()
    mh, mm = metrics(h), metrics(me)
    keys = [k for k in mh if isinstance(mh[k], (int, float)) and k not in ("msgs", "chars", "sentences")]
    delta = {k: (round(mm[k] - mh[k], 3) if isinstance(mh[k], float) else None) for k in keys}
    os.makedirs(OUT_DIR, exist_ok=True)
    open(os.path.join(OUT_DIR, "voice_compare.json"), "w", encoding="utf-8", newline="\n").write(
        json.dumps({"her": mh, "human": mm, "delta_human_minus_her": delta}, ensure_ascii=False, indent=1))
    return {"her": mh, "human": mm, "delta": delta}


def main(argv):
    mode = argv[0] if argv else "corpus"
    if mode == "prompt":
        print(json.dumps(prompt_report(argv[1]), ensure_ascii=False))
        return 0
    if mode == "compare":
        print(json.dumps(compare(), ensure_ascii=False))
        return 0
    texts = her()
    m = metrics(texts)
    os.makedirs(OUT_DIR, exist_ok=True)
    open(os.path.join(OUT_DIR, "voice_baseline.json"), "w", encoding="utf-8", newline="\n").write(
        json.dumps({"metrics": m, "texts": texts[-80:]}, ensure_ascii=False, indent=1))
    print(json.dumps(m, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
