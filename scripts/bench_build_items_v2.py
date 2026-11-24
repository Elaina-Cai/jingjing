"""Build bench/items_v2.json: keep the 11 valid v1 items and add curated new items.

Read-only: reuses scripts/bench_replay.py helpers by import, never modifies them.
The v2 item set adds `question_kind` and `distractors` on top of the v1 fields.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _load_bench_replay():
    spec = importlib.util.spec_from_file_location("bench_replay", ROOT / "scripts" / "bench_replay.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bench_replay"] = mod
    spec.loader.exec_module(mod)
    return mod


BR = _load_bench_replay()

CHATS = ("wxid_seed01", "10000000003@chatroom", "10000000001@chatroom")
DROP_TOKENS = {"247991621", "282000708"}
QUESTION_KINDS = ("file", "num", "date", "term", "arxiv", "topic", "place", "relation")
KEEP_QKIND = {"topic": "topic", "date": "date", "file": "file", "num": "num", "place": "place"}
BOT_NAMES = ("鲸鲸",)

JUNK_RES = (
    re.compile(r"<\?xml|<sysmsg|revoketime|<content>"),
    re.compile(r"@\S{1,30}\d{6,}"),
    re.compile(r"\[表情包|\[图片|\[引用|\[语音|\[视频|\[文件|拍了拍"),
)
URL_RE = re.compile(r"https?://\S+")


def is_junk(content: str) -> bool:
    if not content:
        return True
    return any(pat.search(content) for pat in JUNK_RES)


def url_only(content: str) -> bool:
    stripped = URL_RE.sub("", content or "").strip()
    return bool(content) and len(stripped) < 10


def token_in_url(content: str, token: str) -> bool:
    if not token:
        return False
    for match in URL_RE.finditer(content or ""):
        if token in match.group(0):
            return True
    return False


_ITEM_ID_USED: dict[str, int] = {}


def unique_item_id(chat: str, event_id: str) -> str:
    base = "%s:%s:v2" % (chat, event_id)
    seen = _ITEM_ID_USED.get(base, 0) + 1
    _ITEM_ID_USED[base] = seen
    return base if seen == 1 else "%s-%s" % (base, seen)


def norm(text: str) -> str:
    return BR._norm(text or "")


def row_hits(rows: list[dict], token: str) -> list[int]:
    key = token or ""
    nkey = norm(key)
    hits = []
    for i, row in enumerate(rows):
        content = row.get("content") or ""
        if key and key in content:
            hits.append(i)
            continue
        if nkey and nkey in norm(content):
            hits.append(i)
    return hits


def locate_row(rows: list[dict], token: str, hint: str = "") -> tuple[int, int]:
    hits = row_hits(rows, token)
    if not hits:
        return -1, 0
    if hint:
        narrowed = [i for i in hits if hint in (rows[i].get("content") or "")]
        if narrowed:
            hits = narrowed
    human = [i for i in hits if rows[i].get("name") not in BOT_NAMES]
    if human:
        hits = human
    return hits[0], len(row_hits(rows, token))


def expect_tokens(kind: str, token: str, content: str, explicit: list[str] | None) -> list[str]:
    if explicit:
        return list(explicit)
    try:
        return list(BR._expect_tokens(kind, token, content))
    except Exception:
        return [token]


def distractors_for(rows: list[dict], src: int, *, want_kind: str, expects: list[str], limit: int = 2) -> list[dict]:
    keys = [norm(e) for e in expects if e]
    chains = {
        "num": ("num", "date", "term", "file", "topic"),
        "date": ("date", "term", "num", "topic", "file"),
        "term": ("term", "date", "topic", "file", "num"),
        "file": ("file", "topic", "term", "num", "date"),
        "topic": ("topic", "file", "term", "date", "num"),
        "place": ("place", "topic", "file", "num"),
        "arxiv": ("arxiv", "file", "topic", "num"),
        "phone": ("phone", "num", "date", "file"),
    }
    order = chains.get(want_kind) or (want_kind,)
    by_kind: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        if i == src:
            continue
        content = row.get("content") or ""
        if len(content) < 4 or len(content) > 200 or is_junk(content) or url_only(content):
            continue
        picked = BR._pick_token(content)
        if not picked or picked[0] not in order:
            continue
        nc = norm(content)
        if any(k and k in nc for k in keys):
            continue
        if token_in_url(content, picked[1]):
            continue
        by_kind.setdefault(picked[0], []).append(i)
    chosen: list[tuple[str, int]] = []
    for kind in order:
        for i in sorted(by_kind.get(kind, []), key=lambda j: (abs(j - src), j)):
            chosen.append((kind, i))
            if len(chosen) >= limit:
                break
        if len(chosen) >= limit:
            break
    return [{"row_index": i, "content": (rows[i].get("content") or "")[:80], "kind": kind} for kind, i in chosen]


def make_item(rows: list[dict], chat: str, idx: int, *, kind: str, qkind: str, token: str, expects: list[str], question: str, category: str, distractor_kind: str, item_id: str, selected_by: str) -> dict:
    row = rows[idx] if idx >= 0 else {}
    return {
        "item_id": item_id,
        "chat_id": chat,
        "kind": kind,
        "question_kind": qkind,
        "token": token,
        "expect_tokens": expects,
        "question": question,
        "category": category,
        "distractors": distractors_for(rows, idx, want_kind=distractor_kind, expects=expects),
        "source": {
            "at": row.get("at"),
            "name": row.get("name"),
            "type": row.get("type"),
            "content": row.get("content"),
            "row_index": idx,
        },
        "token_rows": len(row_hits(rows, token)),
        "rows_total": len(rows),
        "selected_by": selected_by,
    }


def in_window_text(token: str, wtext: str) -> bool:
    if not token or not wtext:
        return False
    if token in wtext:
        return True
    for size in (16, 12, 8, 5):
        head = token[:size]
        if len(head) >= 5 and head in wtext:
            return True
    return False


def window_text(chat: str) -> str:
    try:
        return BR._window_text(chat) or ""
    except Exception:
        return ""


def build_funnel(rows: list[dict]) -> dict:
    cjk_re = re.compile(r"[\u4e00-\u9fff]")
    out: dict[str, Any] = {"rows_total": len(rows), "picked_rows": 0, "junk_picked_rows": 0, "short_or_cjk_filtered_rows": 0, "kinds": {}, "raw_by_kind": {}}
    pool: dict[str, set[str]] = {}
    for r in rows:
        content = r.get("content") or ""
        picked = BR._pick_token(content)
        if not picked:
            continue
        out["picked_rows"] += 1
        kind, token = picked
        if is_junk(content):
            out["junk_picked_rows"] += 1
            continue
        if len(cjk_re.findall(content)) < 4:
            out["short_or_cjk_filtered_rows"] += 1
        pool.setdefault(kind, set()).add(token)
    for kind, tokens in sorted(pool.items()):
        le6 = sum(1 for t in tokens if len(row_hits(rows, t)) <= 6)
        le30 = sum(1 for t in tokens if len(row_hits(rows, t)) <= 30)
        out["kinds"][kind] = {"tokens": len(tokens), "uniq_le6": le6, "uniq_le30": le30}
    for name, pat in BR.TOKEN_PATTERNS:
        raw = sum(1 for r in rows if pat.search(r.get("content") or ""))
        cjk = sum(1 for r in rows if pat.search(r.get("content") or "") and len(cjk_re.findall(r.get("content") or "")) >= 4)
        out["raw_by_kind"][name] = {"raw_rows": raw, "rows_ge4cjk": cjk}
    return out


def build_kept(v1_items: list[dict], rows_by_chat: dict[str, list[dict]]) -> tuple[list[dict], list[str]]:
    kept: list[dict] = []
    notes: list[str] = []
    for it in v1_items:
        chat = it.get("chat_id") or ""
        token = it.get("token") or ""
        if token in DROP_TOKENS:
            notes.append("dropped degenerate v1 item: " + token)
            continue
        rows = rows_by_chat.get(chat)
        if rows is None:
            notes.append("skip v1 item, unknown chat: " + chat)
            continue
        idx = int((it.get("source") or {}).get("row_index") or -1)
        verified = 0 <= idx < len(rows) and token in (rows[idx].get("content") or "")
        if not verified:
            found, _hits = locate_row(rows, token)
            notes.append("v1 row_index re-resolved token=%s old=%s new=%s" % (token, idx, found))
            idx = found
        if idx < 0:
            notes.append("skip v1 item, row not found: " + token)
            continue
        kind = it.get("kind") or "topic"
        qkind = KEEP_QKIND.get(kind, "topic")
        wtext = window_text(chat)
        category = "in_window" if in_window_text(token, wtext) else (it.get("category") or "out_of_window")
        want_kind = kind if kind in dict(BR.TOKEN_PATTERNS) else "topic"
        item = make_item(rows, chat, idx, kind=kind, qkind=qkind, token=token, expects=list(it.get("expect_tokens") or []), question=it.get("question") or "", category=category, distractor_kind=want_kind, item_id=it.get("item_id") or "", selected_by="v1_kept")
        kept.append(item)
    return kept, notes


NEW_SPECS: list[dict] = [
    {"chat": "wxid_seed01", "kind": "file", "qkind": "file", "token": "我的课表_2026-2027-1_从今天起.xlsx", "hint": "课表_2026-2027-1", "fallback_question": "我发你的那个文件叫什么名字来着？"},
    {"chat": "wxid_seed01", "kind": "term", "qkind": "term", "token": "2026-2027-1", "hint": "我的课表_2026-2027-1", "fallback_question": "我课表是哪一学年的来着？"},
    {"chat": "wxid_seed01", "kind": "topic", "qkind": "topic", "token": "概率论", "hint": "概率论", "fallback_question": "我说过我哪门课的老师也教过咱们？"},
    {"chat": "wxid_seed01", "kind": "date", "qkind": "date", "token": "2022年", "hint": "2022年", "expects": ["2022年", "2022"], "fallback_question": "我说过哪一年的作文题来着？"},
    {"chat": "wxid_seed01", "kind": "arxiv", "qkind": "arxiv", "token": "BV171WGzCEaf", "hint": "鲸鲸合集", "expects": ["BV171WGzCEaf"], "distractor_kind": "file", "fallback_question": "我发给你的那个合集视频，编号（BV 号）是多少？"},
    {"chat": "wxid_seed01", "kind": "topic", "qkind": "topic", "token": "课表", "hint": "课表", "expects": ["课表"], "distractor_kind": "file", "fallback_question": "我发你的那个文件是干什么用的？"},
    {"chat": "10000000003@chatroom", "kind": "num", "qkind": "num", "token": "9500", "hint": "9500", "force_fallback": True, "fallback_question": "群里那位显摆零花钱的，说的是多少万来着？"},
    {"chat": "10000000003@chatroom", "kind": "num", "qkind": "num", "token": "1260", "hint": "1260", "fallback_question": "我说过那位机长一局能收掉多少吗？"},
    {"chat": "10000000003@chatroom", "kind": "date", "qkind": "date", "token": "2026年", "hint": "2026年10月15日", "force_fallback": True, "fallback_question": "我说过那场斗貫貫是哪一年的事？"},
    {"chat": "10000000003@chatroom", "kind": "relation", "qkind": "relation", "token": "神秘大物理", "hint": "万叶那时候经常打", "expects": ["万叶"], "distractor_kind": "topic", "fallback_question": "我常打神秘纯风伤和神秘大物理的那个角色是谁来着？"},
    {"chat": "10000000001@chatroom", "kind": "arxiv", "qkind": "arxiv", "token": "2604.01985v2", "hint": "2604.01985", "force_fallback": True, "no_row": True, "speaker_note": "P5", "expects": ["2604.01985v2", "2604.01985"], "fallback_question": "昨天群里贴的那篇论文，编号是多少来着？"},
    {"chat": "10000000001@chatroom", "kind": "place", "qkind": "place", "token": "湛江", "hint": "湛江", "expects": ["湛江"], "fallback_question": "我说过我要去哪个城市来着？"},
    {"chat": "10000000001@chatroom", "kind": "place", "qkind": "place", "token": "长沙", "hint": "长沙", "expects": ["长沙"], "force_fallback": True, "fallback_question": "那个湖南的城市是哪儿来着？"},
    {"chat": "10000000001@chatroom", "kind": "topic", "qkind": "topic", "token": "体测", "hint": "体测", "force_fallback": True, "fallback_question": "我说过要准备哪项测试吗？"},
    {"chat": "10000000001@chatroom", "kind": "topic", "qkind": "topic", "token": "概率论作业", "hint": "概率论作业", "force_fallback": True, "fallback_question": "我说过明天要交哪门课的作业吗？"},
    {"chat": "10000000001@chatroom", "kind": "date", "qkind": "date", "token": "10月4日", "hint": "10月4日", "fallback_question": "我说过那件事是几月几号来着？"},
    {"chat": "10000000001@chatroom", "kind": "term", "qkind": "term", "token": "25级", "hint": "25级", "expects": ["25级"], "fallback_question": "我说过那种班号里，专业后面跟的是什么？"},
    {"chat": "10000000001@chatroom", "kind": "num", "qkind": "num", "token": "1000", "hint": "这1000", "fallback_question": "我说过那笔钱只能给到哪个账号？"},
    {"chat": "10000000001@chatroom", "kind": "num", "qkind": "num", "token": "2000", "hint": "2000豆", "fallback_question": "我说过我手里有多少豆吗？"},
    {"chat": "10000000001@chatroom", "kind": "num", "qkind": "num", "token": "99999", "hint": "sleep 99999", "fallback_question": "我说过要给自己的代码写多大的那个数吗？"},
    {"chat": "10000000001@chatroom", "kind": "relation", "qkind": "relation", "token": "高数一", "hint": "概率论老师是我们高数一", "expects": ["高数一"], "distractor_kind": "topic", "fallback_question": "我说过那个教我概率论的老师，是我们哪门课的老师来着？"},
    {"chat": "10000000001@chatroom", "kind": "relation", "qkind": "relation", "token": "学委", "hint": "24、25、26级学委", "expects": ["24级", "25级", "26级"], "force_fallback": True, "distractor_kind": "topic", "fallback_question": "这则选课通知要求哪些年级的学委转发？"},
]


def make_agent():
    try:
        return BR._make_agent(CHATS[-1], "", sender="", owner=False)[0]
    except Exception as exc:
        print("PZWARN agent failed: %s" % str(exc)[:120])
        return None


def question_is_clean(question: str, expects: list[str]) -> bool:
    if not question or len(question) > 40:
        return False
    if not any("\u4e00" <= ch <= "\u9fff" for ch in question):
        return False
    nq = norm(question)
    return not any(norm(e) and norm(e) in nq for e in expects)


def _ask_writer(agent, content: str, expects: list[str]) -> str:
    import asyncio as _asyncio

    system = BR.QW_SYSTEM.format(expects=" / ".join(expects))
    try:
        raw = _asyncio.run(
            agent.llm.chat(
                system=system,
                user=content[:300],
                agent_name="conversational",
                temperature=0.3,
                max_tokens=80,
            )
        )
    except Exception as exc:
        print("PZLLMERR %s" % str(exc)[:160])
        return ""
    print("PZLLMRAW %r" % (str(raw)[:120],))
    q = str(raw or "").strip().strip('"').strip("「」")
    q = q.splitlines()[0].strip() if q else ""
    if not q or len(q) > 40:
        return ""
    if any(norm(t) in norm(q) for t in expects):
        return ""
    return q


def generate_question(agent, spec: dict, content: str, expects: list[str], report: dict) -> tuple[str, str]:
    if spec.get("force_fallback"):
        fb = spec.get("fallback_question") or ""
        if question_is_clean(fb, expects):
            report["curated_forced"] = report.get("curated_forced", 0) + 1
            return fb, "curated"
    if agent is not None:
        report["writer_calls"] += 1
        try:
            q = _ask_writer(agent, content, expects)
        except Exception as exc:
            q = ""
            report["writer_errors"].append(str(exc)[:120])
        print("PZDBG kindraw=%r len=%d clean=%s expects=%s" % (q[:70], len(q or ""), question_is_clean(q, expects), expects))
        if question_is_clean(q, expects):
            report["writer_ok"] += 1
            return q, "llm"
        report["writer_rejected"] += 1
    try:
        q = BR._question_for(spec["kind"], content)
    except Exception:
        q = ""
    if question_is_clean(q, expects):
        return q, "template"
    fb = spec.get("fallback_question") or ""
    if question_is_clean(fb, expects):
        return fb, "curated"
    return "", "none"


def build_new(specs: list[dict], rows_by_chat: dict[str, list[dict]], agent, report: dict) -> list[dict]:
    items: list[dict] = []
    for spec in specs:
        chat = spec["chat"]
        rows = rows_by_chat[chat]
        token = spec["token"]
        if spec.get("no_row"):
            idx = -1
        else:
            idx, _hits = locate_row(rows, token, spec.get("hint") or "")
            if idx < 0:
                report["specs_row_missing"].append(token)
                continue
        content = (rows[idx].get("content") or "") if idx >= 0 else token
        expects = expect_tokens(spec["kind"], token, content, spec.get("expects"))
        question, source = generate_question(agent, spec, content, expects, report)
        if not question:
            report["specs_no_question"].append(token)
            continue
        wtext = window_text(chat)
        category = "in_window" if in_window_text(token, wtext) else "out_of_window"
        dkind = spec.get("distractor_kind") or spec["kind"]
        if dkind not in dict(BR.TOKEN_PATTERNS):
            dkind = "topic"
        item = make_item(rows, chat, idx, kind=spec["kind"], qkind=spec["qkind"], token=token, expects=expects, question=question, category=category, distractor_kind=dkind, item_id=unique_item_id(chat, rows[idx].get("event_id")), selected_by="curated_v2::" + source)
        if not item["distractors"]:
            report["specs_no_distractor"].append(token)
        items.append(item)
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build bench/items_v2.json (read-only)")
    parser.add_argument("--v1", default="bench/items.json")
    parser.add_argument("--out", default="bench/items_v2.json")
    parser.add_argument("--report", default=".test_tmp/items_v2/report.json")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit-specs", type=int, default=0)
    args = parser.parse_args(argv)

    rows_by_chat = {chat: BR._read_rows(chat) for chat in CHATS}
    v1_items = json.loads((ROOT / args.v1).read_text(encoding="utf-8"))
    kept, notes = build_kept(v1_items, rows_by_chat)
    specs = NEW_SPECS[: args.limit_specs] if args.limit_specs else NEW_SPECS
    report: dict[str, Any] = {
        "kept_v1": len(kept),
        "new_specs": len(specs),
        "writer_calls": 0,
        "writer_ok": 0,
        "writer_rejected": 0,
        "writer_errors": [],
        "specs_row_missing": [],
        "specs_no_question": [],
        "specs_no_distractor": [],
        "notes": notes,
        "funnel": {chat: build_funnel(rows) for chat, rows in rows_by_chat.items()},
    }
    agent = None if (args.no_llm or args.dry_run) else make_agent()
    new_items = build_new(specs, rows_by_chat, agent, report)
    items = kept + new_items
    report["total_items"] = len(items)
    report["window_text_len"] = {chat: len(window_text(chat)) for chat in CHATS}
    report["kind_distribution"] = {}
    report["question_kind_distribution"] = {}
    report["category_distribution"] = {}
    for it in items:
        report["kind_distribution"][it["kind"]] = report["kind_distribution"].get(it["kind"], 0) + 1
        report["question_kind_distribution"][it["question_kind"]] = report["question_kind_distribution"].get(it["question_kind"], 0) + 1
        report["category_distribution"][it["category"]] = report["category_distribution"].get(it["category"], 0) + 1

    print("PZSUMMARY total=%s kept=%s new=%s window_len=%s" % (len(items), len(kept), len(new_items), report["window_text_len"]))
    print("PZKIND %s" % json.dumps(report["kind_distribution"], ensure_ascii=False))
    print("PZQKIND %s" % json.dumps(report["question_kind_distribution"], ensure_ascii=False))
    print("PZCAT %s" % json.dumps(report["category_distribution"], ensure_ascii=False))
    for it in items:
        src = it["source"]
        print("PZITEM %s | qk=%s | kind=%s | tok=%s | cat=%s | row=%s | distr=%s | %s" % (it["item_id"], it["question_kind"], it["kind"], it["token"], it["category"], src.get("row_index"), len(it["distractors"]), source_of(it)))
        print("PZQ    %s" % it["question"])
        print("PZS    %s | %s | %s" % (src.get("at"), src.get("name"), (src.get("content") or "").replace(chr(10), " ")[:80]))
        for d in it["distractors"]:
            print("PZD    row=%s | %s" % (d["row_index"], d["content"].replace(chr(10), " ")[:70]))
    for chat, fun in report["funnel"].items():
        print("PZFUN %s %s" % (chat, json.dumps(fun, ensure_ascii=False)))
    if not args.dry_run:
        (ROOT / args.out).write_text(json.dumps(items, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
        (ROOT / args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
        print("PZWRITE %s items=%s report=%s" % (args.out, len(items), args.report))
    return 0


def source_of(item: dict) -> str:
    return item.get("selected_by") or ""


if __name__ == "__main__":
    raise SystemExit(main())
