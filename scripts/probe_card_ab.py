"""人设卡 A/B：同一批 Step 9 用例，分别喂 鲸鲸_v2 / 鲸鲸_v3，比较输出差异。

只替换 selfcard.card_text()（brain 每拍读它建 system），其余一模一样。
"""
from __future__ import annotations

import argparse, asyncio, json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

import probe_step9 as P  # noqa: E402
import loop.brain as B  # noqa: E402
from loop import ports  # noqa: E402
from core.infra.jsonio import atomic_write_json  # noqa: E402

CARDS = os.path.join(ROOT, "data", "persona", "cards")


def card_body(name: str) -> str:
    raw = open(os.path.join(CARDS, name), encoding="utf-8").read()
    return (raw.split("---", 1)[1] if "---" in raw else raw).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cases = json.load(open(P.CASES, encoding="utf-8"))["cases"]
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in want]
    if args.limit:
        cases = cases[: args.limit]

    llm = ports.get_llm()
    orig = B.selfcard.card_text
    out = []
    for card in ("鲸鲸_v2.md", "鲸鲸_v3.md"):
        body = card_body(card)
        B.selfcard.card_text = lambda _b=body: _b
        rows = []
        for i, case in enumerate(cases, 1):
            r = asyncio.run(P.run_one(case, llm, stub_target=True))
            rows.append(r)
            print("[%s %02d/%02d] %-8s 期望=%-6s 开口=%-2s %s" % (
                card.replace("鲸鲸_", "").replace(".md", ""), i, len(cases), r["id"],
                r["expect"], "是" if r["said"] else "否",
                (" / ".join(r["says"])[:50] if r["says"] else "")), flush=True)
        out.append({"card": card, "summary": P.summarise(rows), "rows": rows})
        print("[%s] %s" % (card, json.dumps(out[-1]["summary"], ensure_ascii=False)), flush=True)
    B.selfcard.card_text = orig

    tag = time.strftime("%Y%m%d-%H%M%S")
    prefix = args.out or os.path.join(ROOT, "data", "loop", "card-ab-%s" % tag)
    atomic_write_json(prefix + ".json", {"cases": len(cases), "runs": out}, indent=1)

    # 逐条对照
    v2, v3 = out[0]["rows"], out[1]["rows"]
    lines = ["# 人设卡 A/B：鲸鲸_v2 vs 鲸鲸_v3", "",
             "- v2 摘要：`%s`" % json.dumps(out[0]["summary"], ensure_ascii=False),
             "- v3 摘要：`%s`" % json.dumps(out[1]["summary"], ensure_ascii=False), "",
             "| 用例 | 期望 | v2 | v3 | v2 说了什么 | v3 说了什么 |", "|---|---|---|---|---|---|"]
    diff = 0
    for a, b in zip(v2, v3):
        if a["said"] != b["said"] or [s for s in a["says"]] != [s for s in b["says"]]:
            diff += 1
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            a["id"], a["expect"], "说了" if a["said"] else "没说", "说了" if b["said"] else "没说",
            (" / ".join(a["says"]) or "（静）")[:60], (" / ".join(b["says"]) or "（静）")[:60]))
    lines.insert(3, "- 有差异的用例：%d / %d" % (diff, len(v2)))
    open(prefix + ".md", "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("报告：%s.md / .json" % prefix, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
