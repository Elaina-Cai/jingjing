# -*- coding: utf-8 -*-
"""施工 Step 9 · 效果验证（唯一能回答"像不像人"的一步）。

一句话：拿 `data/loop/step9_cases.json` 里的固定用例，让她**真跑一遍**（dry_run，
一句话都不会真发出去），然后数四个指标：
  1. 该说话时说话率      expect=speak 的用例里，她开口的比例
  2. 不该说时闭嘴率      expect=silent 的用例里，她没开口的比例
  3. 格式违规率          列表 / markdown / 代码块 / 自称 AI / 泄系统词
  4. 同拍重复率          同一拍里两句几乎一样（difflib ≥ 0.85）

用法：
    python scripts/probe_step9.py                 # 全跑
    python scripts/probe_step9.py --limit 3       # 冒烟
    python scripts/probe_step9.py --ab            # 盲测：同用例跑 V1/V2 两版，打乱配对
报告写到 data/loop/step9-report-<时间>.md 和 .json。
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

try:  # 与引擎同口径：.env 里的 key / 开关
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass

from loop import brain as B, snapshot  # noqa: E402
from loop import ports  # noqa: E402
from core.infra.jsonio import atomic_write_json  # noqa: E402

CASES = os.path.join(ROOT, "data", "loop", "step9_cases.json")

#: 格式违规：列表 / markdown / 代码块（她不该像一个助手那样排版）
_BAD_FMT = [
    (re.compile(r"(?m)^\s*[-*+]\s+"), "列表"),
    (re.compile(r"\*\*|__|`{1,3}"), "markdown"),
    (re.compile(r"(?m)^\s*#{1,6}\s"), "标题"),
    (re.compile(r"(?m)^\s*\d+[.、]\s+"), "编号"),
]
#: 越界：自称 AI / 泄系统
_BAD_EGO = [
    (re.compile(r"(作为|我是|身为).{0,8}(AI|人工智能|语言模型|大模型|助手|程序|机器人)"), "自称AI"),
    (re.compile(r"系统(提示|指令)|提示词|prompt|system|大模型|token"), "泄系统"),
    (re.compile(r"我没有(情感|感情|意识)"), "自我否认"),
]


def _now_hm(offset_min: int) -> str:
    t = time.localtime(time.time() - offset_min * 60)
    return time.strftime("%Y-%m-%d %H:%M", t)


def build_snap(case: dict) -> dict:
    """把一条用例搭成 snapshot 的形状（只放一个会话）。"""
    hist = case.get("history") or []
    rows = []
    for i, h in enumerate(hist):
        at = _now_hm(int(h.get("min_ago") or 0))
        who = h.get("who") or "?"
        mine = who in ("我", "鲸鲸")
        rows.append({"at": at, "speaker": "我" if mine else who,
                     "text": str(h.get("text") or ""), "_i": i})
    last = rows[-1] if rows else {"at": _now_hm(0), "speaker": "?", "text": ""}
    cell = {
        "chat_id": case["chat_id"], "chat_type": case["chat_type"], "label": case["label"],
        "self_chat": False, "last_at": last["at"], "last_speaker": last["speaker"],
        "last_is_self": last["speaker"] == "我", "preview": last["text"],
        "after_mine": [], "toward": "", "my_last_at": "",
        "peer_last": {"at": last["at"], "speaker": last["speaker"], "text": last["text"]},
        "tier": "ask" if "@" in "".join(h.get("text") or "" for h in hist) or
                any(k in "".join(h.get("text") or "" for h in hist) for k in ("在吗", "在不在", "怎么不回")) else "",
        "hits": 1, "prev": rows[:-1],
    }
    return {"at": _now_hm(0), "week": "", "db": os.path.join(ROOT, "data", "world.db"),
            "cells": [cell], "total": 1}


def check_text(text: str) -> list[str]:
    regs = []
    for rx, name in _BAD_FMT + _BAD_EGO:
        if rx.search(text or ""):
            regs.append(name)
    return regs


def repeat_pairs(says: list[str]) -> int:
    n = 0
    for i in range(len(says)):
        for j in range(i + 1, len(says)):
            if difflib.SequenceMatcher(None, says[i], says[j]).ratio() >= 0.85:
                n += 1
    return n


class _Res:
    """假的 say 结果：形状照 ports.say_many 的返回（.ok / .error / .data）。"""

    def __init__(self, ok=True, error="", data=None):
        self.ok = ok
        self.error = error
        self.data = data or {}


async def run_one(case: dict, llm, *, stub_target: bool = True) -> dict:
    """跑一条用例：dry_run，绝不真发。"""
    got: list[dict] = []
    orig = B.ledger.append_receipt
    # 2026-10-31：用例里的会话是造的，真实 resolve_say_target 认不出 → say 会落 error，
    # 指标就把"她判断该说话"算成"没开口"。默认把本用例的 label/chat_id 解析成本用例的 chat_id，
    # 只替换"认名字"这一步，say 的其余闸（白名单、quiet_ms、same_target…）原样跑。
    _cid = case["chat_id"]
    _names = {case["label"], _cid}
    _o_rst, _o_rcid, _o_say = B.ports.resolve_say_target, B.ports.resolve_chat_id, B.ports.say_many

    def _rcid(t):
        return _cid if (t or "").strip() in _names else _o_rcid(t)

    def _rst(t):
        return (_cid, "") if (t or "").strip() in _names else _o_rst(t)

    async def _say(bubbles, **kw):
        return _Res(True, "", {"ack": "probe", "bubbles": len(bubbles)})

    if stub_target:
        B.ports.resolve_chat_id = _rcid
        B.ports.resolve_say_target = _rst
        B.ports.say_many = _say

    def spy(**kw):
        row = dict(kw)
        got.append(row)
        # 与真实 ledger.append_receipt 同形返回：brain 会读 receipt["result"]
        return {"act": row.get("act"), "result": row.get("result"),
                "target": row.get("target"), "text": row.get("text"),
                "why": row.get("why") or "", "at": ""}

    B.ledger.append_receipt = spy
    snap = build_snap(case)
    try:
        snap_text = snapshot.render_text(snap, only=case["label"], back=False)
        turns = snapshot.render_turns(snap, case["label"], limit=15)
        blk = "[原文]（这一窗，按时间顺序，不带注释）\n" + turns
        t0 = time.time()
        await B.run_loop(snap["db"], snap_text, snap_turns=blk, llm=llm, dry_run=True,
                         focus=case["label"], platform=case["platform"], chat_id=case["chat_id"],
                         others=[], tick_since="")
        cost = round(time.time() - t0, 1)
    finally:
        B.ledger.append_receipt = orig
        B.ports.resolve_say_target, B.ports.resolve_chat_id = _o_rst, _o_rcid
        B.ports.say_many = _o_say
    says = [str(r.get("text") or "") for r in got if r.get("act") == "say" and r.get("result") == "sent"]
    return {"id": case["id"], "expect": case["expect"], "said": bool(says), "says": says,
            "acts": [(r.get("act"), r.get("result")) for r in got],
            "viol": sorted({v for s in says for v in check_text(s)}),
            "dup": repeat_pairs(says), "sec": cost}


def summarise(rows: list[dict]) -> dict:
    def pct(a, b):
        return None if not b else round(100.0 * a / b, 1)
    sp = [r for r in rows if r["expect"] == "speak"]
    si = [r for r in rows if r["expect"] == "silent"]
    said_any = [r for r in rows if r["said"]]
    viol = [r for r in rows if r["viol"]]
    dup = [r for r in rows if r["dup"] > 0]
    return {
        "cases": len(rows),
        "该说话时说话率": "%d/%d = %s%%" % (sum(1 for r in sp if r["said"]), len(sp), pct(sum(1 for r in sp if r["said"]), len(sp))),
        "不该说时闭嘴率": "%d/%d = %s%%" % (sum(1 for r in si if not r["said"]), len(si), pct(sum(1 for r in si if not r["said"]), len(si))),
        "格式/越界违规率": "%d/%d = %s%%" % (len(viol), len(said_any), pct(len(viol), len(said_any))),
        "同拍重复率": "%d/%d = %s%%" % (len(dup), len(said_any), pct(len(dup), len(said_any))),
        "开口率(全部)": "%d/%d = %s%%" % (len(said_any), len(rows), pct(len(said_any), len(rows))),
        "平均耗时s": round(sum(r["sec"] for r in rows) / max(1, len(rows)), 1),
    }


def to_md(rows, sumry, tag) -> str:
    out = ["# Step 9 · 效果验证 %s" % tag, "",
           "固定用例：`data/loop/step9_cases.json`（%d 条）。dry_run，未真发。" % len(rows), "",
           "## 四个指标", ""]
    for k, v in sumry.items():
        out.append("- **%s**：%s" % (k, v))
    out += ["", "## 逐条", "", "| 用例 | 期望 | 她开口 | 说了什么 | 违规 | 重复 | 秒 |",
            "|---|---|---|---|---|---|---|"]
    for r in rows:
        says = " / ".join(s.replace("\n", "⏎") for s in r["says"]) or "（静着）"
        out.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            r["id"], r["expect"], "说了" if r["said"] else "没说",
            says[:70], ",".join(r["viol"]) or "-", r["dup"] or "-", r["sec"]))
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（冒烟用）")
    ap.add_argument("--out", default="", help="报告路径前缀")
    ap.add_argument("--ab", action="store_true", help="盲测模式：同一批用例跑 V1/V2 两版并打乱配对")
    ap.add_argument("--only", default="", help="只跑这些 id（逗号分隔）")
    ap.add_argument("--raw", action="store_true", help="不 stub 目标解析（旧口径：认不出名字就算没开口）")
    args = ap.parse_args()

    data = json.load(open(CASES, encoding="utf-8"))
    cases = data["cases"]
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in want]
    if args.limit:
        cases = cases[:args.limit]

    llm = ports.get_llm()
    tag = time.strftime("%Y%m%d-%H%M%S")
    rows = []
    for i, case in enumerate(cases, 1):
        r = asyncio.run(run_one(case, llm, stub_target=not args.raw))
        rows.append(r)
        print("[%02d/%02d] %-8s 期望=%-6s 开口=%-2s %s" % (
            i, len(cases), r["id"], r["expect"], "是" if r["said"] else "否",
            (" / ".join(r["says"])[:40] if r["says"] else "")))

    sumry = summarise(rows)
    prefix = args.out or os.path.join(ROOT, "data", "loop", "step9-report-%s" % tag)
    atomic_write_json(prefix + ".json", {"cases": len(rows), "summary": sumry, "rows": rows}, indent=1)
    open(prefix + ".md", "w", encoding="utf-8").write(to_md(rows, sumry, tag))
    print("\n" + json.dumps(sumry, ensure_ascii=False, indent=1))
    print("报告：%s.md / .json" % prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
