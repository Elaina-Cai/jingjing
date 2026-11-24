"""Build bench/items_v3.json — a never-tuned hold-out item set (read-only).

Why: v2 (33 items) exposed that the retrieval layer cannot save place (0/3),
term (1/2) or arxiv (0/1) questions.  v2 has already been looked at, so any
repair must be validated on a fresh set.  v3 is that hold-out set: same fields
and discipline as v2 (question_kind, >=2 distractors, question must not leak
the answer token, unique + verifiable answer), deduplicated against the 33 v2
items (same normalised token or same source.row_index == duplicate).

Red lines: read-only (db.write_guard.read_only_engine via bench_replay),
no production writes, never modifies bench_replay.py / bench_build_items_v2.py.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CH657 = "wxid_seed01"
CH501 = "10000000003@chatroom"
CH579 = "10000000001@chatroom"
CHATS = (CH657, CH501, CH579)


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BR = _load("bench_replay", "scripts/bench_replay.py")
V2 = _load("bench_build_items_v2", "scripts/bench_build_items_v2.py")

V3_SPECS: list[dict[str, Any]] = [
    {
        "chat": CH579,
        "kind": "place",
        "qkind": "place",
        "token": "\u91cd\u5e86",
        "hint": "\u5bbf\u820d\u5168\u6eda\u86cb\u4e86",
        "distractor_kind": "place",
        "force_fallback": True,
        "fallback_question": "\u6211\u8bf4\u8fc7\u6211\u662f\u54ea\u513f\u4eba\u6765\u7740\uff1f",
    },
    {
        "chat": CH579,
        "kind": "place",
        "qkind": "place",
        "token": "\u5e7f\u5dde",
        "hint": "\u770b\u770b\u5e7f\u5dde\u4eca\u5929\u7684\u5929\u6c14",
        "distractor_kind": "place",
        "force_fallback": True,
        "fallback_question": "\u56fd\u5e86\u90a3\u5929\u6211\u8ba9\u94f6\u6708\u5e2e\u6211\u67e5\u4e86\u54ea\u5ea7\u57ce\u5e02\u7684\u5929\u6c14\uff1f",
    },
    {
        "chat": CH579,
        "kind": "place",
        "qkind": "place",
        "token": "\u5357\u5b81",
        "hint": "\u540e\u5929\u5357\u5b81\u7684\u5929\u6c14",
        "distractor_kind": "place",
        "force_fallback": True,
        "fallback_question": "\u7fa4\u53cb\u8ba9\u94f6\u6708\u770b\u540e\u5929\u54ea\u5ea7\u57ce\u5e02\u7684\u5929\u6c14\u6765\u7740\uff1f",
    },
    {
        "chat": CH579,
        "kind": "place",
        "qkind": "place",
        "token": "\u676d\u5dde",
        "hint": "\u5230\u676d\u5dde\u522b\u6025\u7740\u8d76\u8def",
        "distractor_kind": "place",
        "force_fallback": True,
        "fallback_question": "\u5c0f\u6986\u8981\u53bb\u7684\u90a3\u5ea7\u57ce\u5e02\uff0c\u6211\u8bf4\u5230\u4e86\u5148\u627e\u7897\u7247\u513f\u5ddd\uff0c\u662f\u54ea\u513f\uff1f",
    },
    {
        "chat": CH579,
        "kind": "term",
        "qkind": "term",
        "token": "2026-2027",
        "hint": "\u9009\u8bfe\u65f6\u95f4\u662f",
        "distractor_kind": "term",
        "expects": ["2026-2027", "2026-2027\u5b66\u5e74"],
        "force_fallback": True,
        "fallback_question": "\u90a3\u5219\u9009\u8bfe\u901a\u77e5\u8bf4\u7684\u662f\u54ea\u4e2a\u5b66\u5e74\uff1f",
    },
    {
        "chat": CH579,
        "kind": "term",
        "qkind": "term",
        "token": "2026\u5e74\u79cb\u5b63\u5b66\u671f",
        "hint": "\u5b66\u98ce\u5efa\u8bbe\u4f1a\u8bae",
        "distractor_kind": "term",
        "expects": ["2026\u5e74\u79cb\u5b63\u5b66\u671f"],
        "force_fallback": True,
        "fallback_question": "\u90a3\u4efd\u4f1a\u8bae\u901a\u77e5\u8bf4\u7684\u662f\u54ea\u4e2a\u5b66\u671f\u7684\u5b66\u98ce\u5efa\u8bbe\u4f1a\u8bae\uff1f",
    },
    {
        "chat": CH501,
        "kind": "arxiv",
        "qkind": "arxiv",
        "token": "BV1V9p16VEHR",
        "hint": "\u8299\u8299\u8fdb\u961f",
        "distractor_kind": "arxiv",
        "expects": ["BV1V9p16VEHR"],
        "force_fallback": True,
        "fallback_question": "\u90a3\u4e2a\u8bf4\u8299\u8299\u8fdb\u961f\u63d0\u5347\u7684\u89c6\u9891\uff0cBV \u53f7\u662f\u591a\u5c11\u6765\u7740\uff1f",
    },
    {
        "chat": CH579,
        "kind": "term",
        "qkind": "relation",
        "token": "\u8ba1\u7b97\u673a25-4\u73ed",
        "hint": "\u76fc\u76fc",
        "distractor_kind": "term",
        "expects": ["\u8ba1\u7b97\u673a25-4\u73ed"],
        "force_fallback": True,
        "fallback_question": "\u6211\u8bf4\u8fc7\u76fc\u76fc\u662f\u54ea\u4e2a\u73ed\u7684\u6765\u7740\uff1f",
    },
    {
        "chat": CH579,
        "kind": "topic",
        "qkind": "relation",
        "token": "P3",
        "hint": "\u73b0\u4efb\u662fP3",
        "distractor_kind": "topic",
        "expects": ["P3"],
        "force_fallback": True,
        "fallback_question": "\u7fa4\u91cc\u73b0\u5728\u7684\u5b66\u59d4\u662f\u8c01\u6765\u7740\uff1f",
    },
    {
        "chat": CH657,
        "kind": "topic",
        "qkind": "relation",
        "token": "\u4f5c\u4e1a\u8bda\u4fe1\u4e92\u5237\u7fa4",
        "hint": "\u597d\u53cb\u4eec\u51d1",
        "distractor_kind": "topic",
        "expects": ["\u4f5c\u4e1a\u8bda\u4fe1\u4e92\u5237\u7fa4"],
        "force_fallback": True,
        "fallback_question": "\u6211\u8ddf\u4f60\u8bf4\u8fc7\u7684\u90a3\u4e2a\u597d\u53cb\u4eec\u51d1\u7684\u7fa4\uff0c\u53eb\u4ec0\u4e48\u540d\u5b57\u6765\u7740\uff1f",
    },
    {
        "chat": CH657,
        "kind": "file",
        "qkind": "file",
        "token": "25034480429-\u8521\u5b50\u5065.pdf",
        "hint": "",
        "distractor_kind": "file",
        "force_fallback": True,
        "fallback_question": "\u6211\u53d1\u4f60\u7684\u90a3\u4efd PDF \u6587\u4ef6\u540d\u662f\u4ec0\u4e48\uff1f",
    },
    {
        "chat": CH579,
        "kind": "topic",
        "qkind": "topic",
        "token": "\u7269\u7406\u5b9e\u9a8c",
        "hint": "\u53c8\u8981\u8fc7\u597d\u51e0\u5929\u624d\u80fd\u518d\u4e0a",
        "distractor_kind": "topic",
        "force_fallback": True,
        "fallback_question": "\u7fa4\u91cc\u6709\u4eba\u8bf4\u4ec0\u4e48\u8bfe\u53c8\u8981\u7b49\u597d\u51e0\u5929\u624d\u80fd\u518d\u4e0a\uff1f",
    },
]


def build(specs: list[dict], items_v2: list[dict], rows_by_chat: dict[str, list[dict]], agent, report: dict) -> list[dict]:
    used_tok = {V2.norm(it["token"]) for it in items_v2}
    used_rows = set()
    for it in items_v2:
        r = it["source"].get("row_index")
        if r is not None:
            used_rows.add((it["chat_id"], r))
    items: list[dict] = []
    for spec in specs:
        chat = spec["chat"]
        rows = rows_by_chat[chat]
        token = spec["token"]
        if V2.norm(token) in used_tok:
            report["dedup_token"].append(token)
            continue
        idx, hits = V2.locate_row(rows, token, spec.get("hint") or "")
        if idx < 0:
            report["specs_row_missing"].append(token)
            continue
        if (chat, idx) in used_rows:
            report["dedup_row"].append(token + " @row" + str(idx))
            continue
        content = rows[idx].get("content") or ""
        expects = V2.expect_tokens(spec["kind"], token, content, spec.get("expects"))
        question, source = V2.generate_question(agent, spec, content, expects, report)
        if not question:
            report["specs_no_question"].append(token)
            continue
        wtext = V2.window_text(chat)
        dkind = spec.get("distractor_kind") or spec["kind"]
        if dkind not in dict(BR.TOKEN_PATTERNS):
            dkind = "topic"
        item = V2.make_item(
            rows,
            chat,
            idx,
            kind=spec["kind"],
            qkind=spec["qkind"],
            token=token,
            expects=expects,
            question=question,
            category="in_window" if V2.in_window_text(token, wtext) else "out_of_window",
            distractor_kind=dkind,
            item_id="%s:%s:v3" % (chat, rows[idx].get("event_id")),
            selected_by="curated_v3::" + source,
        )
        if len(item["distractors"]) < 2:
            report["specs_lt2_distractors"].append(token)
        report["hits"][token] = hits
        items.append(item)
    return items


def verify(items: list[dict], items_v2: list[dict]) -> list[str]:
    problems: list[str] = []
    v2_tok = {V2.norm(it["token"]) for it in items_v2}
    v2_rows = {(it["chat_id"], it["source"].get("row_index")) for it in items_v2}
    seen_ids: set[str] = set()
    for it in items:
        tag = it["item_id"]
        if tag in seen_ids:
            problems.append("dup item_id %s" % tag)
        seen_ids.add(tag)
        if V2.norm(it["token"]) in v2_tok:
            problems.append("token also in v2: %s" % it["token"])
        if (it["chat_id"], it["source"].get("row_index")) in v2_rows:
            problems.append("row also in v2: %s" % tag)
        if not V2.question_is_clean(it["question"], it["expect_tokens"]):
            problems.append("question leaks/none: %s" % tag)
        if len(it["distractors"]) < 2:
            problems.append("lt2 distractors: %s" % tag)
        for d in it["distractors"]:
            nd = V2.norm(d["content"])
            if any(V2.norm(e) and V2.norm(e) in nd for e in it["expect_tokens"]):
                problems.append("distractor leaks: %s" % tag)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build bench/items_v3.json (read-only hold-out set)")
    parser.add_argument("--v2", default="bench/items_v2.json")
    parser.add_argument("--out", default="bench/items_v3.json")
    parser.add_argument("--report", default=".test_tmp/items_v3/report.json")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit-specs", type=int, default=0)
    args = parser.parse_args(argv)

    rows_by_chat = {chat: BR._read_rows(chat) for chat in CHATS}
    items_v2 = json.loads((ROOT / args.v2).read_text(encoding="utf-8"))
    specs = V3_SPECS[: args.limit_specs] if args.limit_specs else V3_SPECS
    report: dict[str, Any] = {
        "specs": len(specs),
        "writer_calls": 0,
        "writer_ok": 0,
        "writer_rejected": 0,
        "writer_errors": [],
        "dedup_token": [],
        "dedup_row": [],
        "specs_row_missing": [],
        "specs_no_question": [],
        "specs_lt2_distractors": [],
        "hits": {},
        "funnel": {chat: V2.build_funnel(rows) for chat, rows in rows_by_chat.items()},
        "note": "v3 = hold-out set; repairs tuned on v2 must be validated on v3, never retuned on v2.",
    }
    agent = None if (args.no_llm or args.dry_run) else V2.make_agent()
    items = build(specs, items_v2, rows_by_chat, agent, report)
    report["total_items"] = len(items)
    report["window_text_len"] = {chat: len(V2.window_text(chat)) for chat in CHATS}
    for key in ("kind_distribution", "question_kind_distribution", "category_distribution"):
        report[key] = {}
    for it in items:
        for key, field in (
            ("kind_distribution", "kind"),
            ("question_kind_distribution", "question_kind"),
            ("category_distribution", "category"),
        ):
            report[key][it[field]] = report[key].get(it[field], 0) + 1
    problems = verify(items, items_v2)

    print("PZSUMMARY total=%s specs=%s window_len=%s" % (len(items), len(specs), report["window_text_len"]))
    print("PZKIND %s" % json.dumps(report["kind_distribution"], ensure_ascii=False))
    print("PZQKIND %s" % json.dumps(report["question_kind_distribution"], ensure_ascii=False))
    print("PZCAT %s" % json.dumps(report["category_distribution"], ensure_ascii=False))
    print("PZWRITER calls=%s ok=%s rejected=%s errors=%s" % (report["writer_calls"], report["writer_ok"], report["writer_rejected"], report["writer_errors"]))
    print("PZDEDUP token=%s row=%s" % (report["dedup_token"], report["dedup_row"]))
    print("PZMISS row=%s noq=%s lt2=%s" % (report["specs_row_missing"], report["specs_no_question"], report["specs_lt2_distractors"]))
    for it in items:
        src = it["source"]
        print("PZITEM %s | qk=%s | kind=%s | tok=%s | cat=%s | row=%s | hits=%s | distr=%s" % (
            it["item_id"], it["question_kind"], it["kind"], it["token"], it["category"], src.get("row_index"),
            report["hits"].get(it["token"]), len(it["distractors"])))
        print("PZQ    %s" % it["question"])
        print("PZE    %s" % json.dumps(it["expect_tokens"], ensure_ascii=False))
        print("PZS    %s | %s | %s" % (src.get("at"), src.get("name"), (src.get("content") or "").replace(chr(10), " ")[:80]))
        for d in it["distractors"]:
            print("PZD    row=%s | %s" % (d["row_index"], d["content"].replace(chr(10), " ")[:70]))
    for chat, fun in report["funnel"].items():
        print("PZFUN %s %s" % (chat, json.dumps(fun, ensure_ascii=False)))
    print("PZVERIFY items=%s problems=%s" % (len(items), len(problems)))
    for p in problems:
        print("PZPROB %s" % p)
    if not args.dry_run:
        (ROOT / args.out).write_text(json.dumps(items, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
        (ROOT / args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
        print("PZWRITE %s items=%s report=%s" % (args.out, len(items), args.report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
