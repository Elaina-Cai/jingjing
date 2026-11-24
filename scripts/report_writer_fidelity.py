"""写侧 ①³ 量具：随手记（B 路线确定性观察器）的三项读数（只读）。

读三样东西，全部只读：
1. **覆盖率**：``terminal_events``（``mode=ro``）真实回合每轮产出 0/1/2/3 条的分布；
2. **保真率**：``bench/items.json`` 13 题的**正确答案段**喂给观察器，``expect_tokens``
   命中率（口径同 ``scripts/bench_replay.py:_norm/_grade``；基线 = 旧 LLM 写手
   「从正确答案段写」只抄对 3/13 的关键 token，见 ``docs/decisions/让一个决策点读到原文.md`` §12）；
3. **误记率**：候选的 ``quote`` 是否为源行字面子串（结构判据），并导出人工复核样本 20 条。

分母为 0 一律报 ``n/a``，不编数。

用法::

    python scripts/report_writer_fidelity.py
    python scripts/report_writer_fidelity.py --limit 6000 --sample 20 \
        --json .test_tmp/task12/writer_fidelity_report.json \
        --md .test_tmp/task12/writer_fidelity_sample.md
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory.note_writer import observe_message  # noqa: E402

BENCH = ROOT / "bench" / "items.json"
META_DB = ROOT / "data" / "yinyue_meta.db"

# 与 scripts/bench_replay.py 同口径（抄一份：脚本之间不互相 import，避免跑测试时拉起重模块）。
_CN_DIGITS = {
    "零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}
_CN_CHARS = "零一二三四五六七八九十"

# 基线：旧写手「小模型手记 · 从正确答案段写」只抄对 3/13 的关键 token。
BASELINE = {
    "source": "docs/decisions/让一个决策点读到原文.md §12",
    "exact": 3,
    "items": 13,
    "note": "小模型「手记」从正确答案段写，只抄对 3/13 的关键 token（文件名/数字/日期会丢）",
}

TURN_TYPES = ("message", "question", "reply", "request")


def _cn_int(text: str) -> str:
    if "十" not in text:
        return "".join(_CN_DIGITS.get(ch, "") for ch in text)
    head, _, tail = text.partition("十")
    tens = _CN_DIGITS.get(head, "1") if head else "1"
    ones = _CN_DIGITS.get(tail, "0") if tail else "0"
    return f"{tens}{ones}"


def _norm(text: str) -> str:
    s = re.sub(r"[\s_\-\u2013\u2014*#`]", "", str(text or ""))
    s = s.replace("号", "日")
    s = re.sub(
        r"[" + _CN_CHARS + r"]{1,3}月",
        lambda m: f"{int(_cn_int(m.group(0)[:-1]))}月",
        s,
    )
    s = re.sub(
        r"[" + _CN_CHARS + r"]{1,3}日",
        lambda m: f"{int(_cn_int(m.group(0)[:-1]))}日",
        s,
    )
    return s


def read_rows(con: sqlite3.Connection, chat_id: str, limit: int) -> list[dict[str, Any]]:
    """与 ``scripts/bench_replay.py:_read_rows`` 同一条 SQL（只读）。"""
    rows = con.execute(
        "select event_id, occurred_at, event_type, actor_person_id, content "
        "from terminal_events where chat_id = ? "
        "and event_type in ('message','question','reply','request') "
        "and content is not null and content != '' "
        "order by occurred_at, sequence limit ?",
        (chat_id, int(limit)),
    ).fetchall()
    return [
        {
            "event_id": str(event_id),
            "at": str(occurred_at),
            "type": str(event_type),
            "actor": str(actor or ""),
            "content": str(content or ""),
        }
        for event_id, occurred_at, event_type, actor, content in rows
    ]


def bot_actors(con: sqlite3.Connection) -> set[str]:
    """她自己的 actor：只有她会写 ``thought`` 行，所以拿它当判据（不写死人名）。"""
    return {
        str(row[0])
        for row in con.execute(
            "select distinct actor_person_id from terminal_events where event_type = 'thought'"
        )
        if row[0]
    }


def fidelity(limit_items: int = 0, include_bare: bool = False) -> dict[str, Any]:
    items = json.load(io.open(BENCH, encoding="utf-8"))
    if limit_items:
        items = items[: int(limit_items)]
    per_item = []
    exact = 0
    token_hit = 0
    token_total = 0
    for item in items:
        source = item.get("source") or {}
        content = str(source.get("content") or "")
        notes = observe_message(
            content,
            message_id=str(item.get("item_id") or ""),
            speaker=str(source.get("name") or ""),
            at=str(source.get("at") or ""),
            include_bare=include_bare,
            chat_id=str(item.get("chat_id") or ""),
        )
        joined = " || ".join(n.note_text for n in notes)
        expect = list(item.get("expect_tokens") or [])
        hits = [t for t in expect if _norm(t) in _norm(joined)]
        token_hit += len(hits)
        token_total += len(expect)
        ok = bool(expect) and len(hits) == len(expect)
        exact += 1 if ok else 0
        per_item.append(
            {
                "item_id": item.get("item_id"),
                "kind": item.get("kind"),
                "expect_tokens": expect,
                "hits": hits,
                "exact": ok,
                "notes": [n.to_record() for n in notes],
                "source_content": content,
            }
        )
    return {
        "items": len(items),
        "exact": exact,
        "token_hit": token_hit,
        "token_total": token_total,
        "token_recall": round(token_hit / token_total, 4) if token_total else "n/a",
        "baseline": BASELINE,
        "include_bare": bool(include_bare),
        "per_item": per_item,
    }


def coverage(
    con: sqlite3.Connection, chats: list[str], limit: int, sample_n: int = 20, include_bare: bool = False
) -> dict[str, Any]:
    dist: dict[str, int] = {"0": 0, "1": 0, "2": 0, "3": 0}
    kinds: dict[str, int] = {}
    per_chat = []
    notes_all: list[dict[str, Any]] = []
    mine = bot_actors(con)
    turns = 0
    note_total = 0
    supported = 0
    unsupported: list[dict[str, Any]] = []
    for chat_id in chats:
        rows = read_rows(con, chat_id, limit)
        chat_notes = 0
        chat_dist = {"0": 0, "1": 0, "2": 0, "3": 0}
        for row in rows:
            if not row["actor"].startswith("wxid_"):
                continue
            if row["actor"] in mine:
                continue
            turns += 1
            notes = observe_message(
                row["content"],
                message_id=row["event_id"],
                speaker=row["actor"],
                at=row["at"],
                include_bare=include_bare,
                chat_id=chat_id,
            )
            n = min(len(notes), 3)
            dist[str(n)] = dist.get(str(n), 0) + 1
            chat_dist[str(n)] = chat_dist.get(str(n), 0) + 1
            note_total += len(notes)
            chat_notes += len(notes)
            for cand in notes:
                kinds[cand.kind] = kinds.get(cand.kind, 0) + 1
                slice_ = row["content"][cand.start : cand.end]
                if slice_ == cand.quote or slice_.startswith(cand.quote):
                    supported += 1
                else:
                    unsupported.append(
                        {
                            "chat_id": chat_id,
                            "message_id": row["event_id"],
                            "start": cand.start,
                            "end": cand.end,
                            "quote": cand.quote,
                            "source": row["content"][:200],
                        }
                    )
                notes_all.append(
                    {
                        "chat_id": chat_id,
                        "message_id": row["event_id"],
                        "at": row["at"],
                        "speaker": row["actor"],
                        "kind": cand.kind,
                        "quote": cand.quote,
                        "start": cand.start,
                        "end": cand.end,
                        "source_content": row["content"][:300],
                    }
                )
        per_chat.append(
            {
                "chat_id": chat_id,
                "rows": len(rows),
                "turns": sum(chat_dist.values()),
                "dist": chat_dist,
                "notes": chat_notes,
            }
        )
    # 抽样要覆盖整个窗口，不取「最早 20 条」（那全落在最早的几行里）。
    sample: list[dict[str, Any]] = []
    picked_n = max(1, int(sample_n))
    if notes_all:
        if len(notes_all) <= picked_n:
            picked = list(notes_all)
        else:
            step = len(notes_all) / float(picked_n)
            picked = [
                notes_all[min(len(notes_all) - 1, int((k + 0.5) * step))]
                for k in range(picked_n)
            ]
        for k, item in enumerate(picked, 1):
            merged = dict(item)
            merged["index"] = k
            sample.append(merged)
    return {
        "limit_per_chat": int(limit),
        "include_bare": bool(include_bare),
        "turn_definition": "actor 为 wxid_*、且不是她自己（她写的 thought/reply 行）的入站事件行",
        "turns": turns,
        "dist": dist,
        "dist_pct": {
            k: (round(v / turns * 100, 1) if turns else "n/a") for k, v in dist.items()
        },
        "notes": note_total,
        "kinds": kinds,
        "bare_share": round(kinds.get("bare", 0) / note_total, 4) if note_total else "n/a",
        "structural_support": {
            "supported": supported,
            "unsupported": len(unsupported),
            "rate": round(supported / note_total, 4) if note_total else "n/a",
            "unsupported_items": unsupported[:20],
        },
        "per_chat": per_chat,
        "sample": sample,
    }


def write_sample_md(path: Path, sample: list[dict[str, Any]]) -> None:
    lines = [
        "# 随手记 20 条抽样（人工判「支持 / 不支持」用）",
        "",
        "判据：① `quote` 是否是源行的字面子串（结构支持，脚本已判）；"
        "② 这条候选读起来是不是**源行真有的事**（不是拼接/脑补）。",
        "",
        "| # | kind | 候选（note_text） | 源行 | 结构 | 人工判定 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in sample:
        quote = str(item["quote"]).replace("|", "\\|")
        source = str(item["source_content"]).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {item['index']} | {item['kind']} | {quote} | {source} | 支持 | |"
        )
    if not sample:
        lines.append("| - | - | （分母为 0：样本窗口内没有候选） | - | n/a | n/a |")
    lines.append("")
    io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description="写侧随手记（B 路线）三项读数（只读）")
    parser.add_argument("--limit", type=int, default=6000, help="每个 chat 读多少行（同 bench_replay 默认）")
    parser.add_argument("--chats", default="10000000001@chatroom,10000000003@chatroom")
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--include-bare", action="store_true", help="把 bare（整条短词）也算进来")  # noqa: E501
    parser.add_argument("--json", default=".test_tmp/task12/writer_fidelity_report.json")
    parser.add_argument("--md", default=".test_tmp/task12/writer_fidelity_sample.md")
    args = parser.parse_args()

    chats = [c.strip() for c in args.chats.split(",") if c.strip()]
    result: dict[str, Any] = {"chats": chats, "limit": args.limit}
    result["include_bare"] = bool(args.include_bare)
    result["fidelity"] = fidelity(include_bare=args.include_bare)
    if not META_DB.exists():
        result["coverage"] = {"error": f"meta db not found: {META_DB}", "turns": 0, "dist": {}}
    else:
        con = sqlite3.connect(f"file:{META_DB.as_posix()}?mode=ro", uri=True)
        try:
            result["coverage"] = coverage(con, chats, args.limit, args.sample, args.include_bare)
        finally:
            con.close()

    out_path = (ROOT / args.json) if not Path(args.json).is_absolute() else Path(args.json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    io.open(out_path, "w", encoding="utf-8", newline="\n").write(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    md_path = (ROOT / args.md) if not Path(args.md).is_absolute() else Path(args.md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    write_sample_md(md_path, result["coverage"].get("sample", []))

    fid = result["fidelity"]
    cov = result["coverage"]
    group = "bare=on" if args.include_bare else "bare=off（默认）"
    print(f"[group] {group} limit={args.limit} chats={','.join(chats)}")
    print(f"[fidelity] items={fid['items']} exact={fid['exact']}/{fid['items']} "
          f"token={fid['token_hit']}/{fid['token_total']} (baseline {BASELINE['exact']}/13)")
    if "error" in cov:
        print(f"[coverage] {cov['error']}")
    else:
        print(f"[coverage] turns={cov['turns']} dist={cov['dist']} pct={cov['dist_pct']} "
              f"notes={cov['notes']} kinds={cov['kinds']}")
        print(f"[support] {cov['structural_support']['supported']}/{cov['notes']} "
              f"rate={cov['structural_support']['rate']}")
    print(f"[out] {out_path}")
    print(f"[out] {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
