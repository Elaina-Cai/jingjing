# -*- coding: utf-8 -*-
"""A 路线离线探针：同一条消息 + ``worth_remembering`` 字段，量一次保真率。

纪律（task-12）：这是**探针**，不进产品调用路径、不改任何 prompt 常量、不写库。
它只回答一个问题：若让模型自己挑「值得记的字面片段」，能挑中多少 token（对照组：
B 路线确定性观察器 = 保真率 11/15 token、9/13 exact；旧写手基线 3/13）。

用法::

    python scripts/probe_writer_llm_field.py --limit 3          # 先探通道
    python scripts/probe_writer_llm_field.py --json <out.json>  # 全量 13 条
    python scripts/probe_writer_llm_field.py --dry-run          # 只打印 prompt，不调模型
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from report_writer_fidelity import _norm  # noqa: E402

ITEMS = ROOT / "bench" / "items.json"
SYSTEM = (
    "你是写侧观察器。只从「当前消息」里挑 0-3 条值得记的字面片段（必须是原话的连续子串，"
    "不要改写、不要解释）。最多 3 条，没有就返回空数组。"
)
PROMPT = (
    "当前消息：\n{content}\n\n"
    '只输出 JSON：{{"worth_remembering": ["原话片段", ...]}}（0-3 条）'
)


def load_items(limit: int = 0) -> list[dict]:
    items = json.loads(ITEMS.read_text(encoding="utf-8"))
    items = [it for it in items if (it.get("expect_tokens") or [])]
    if limit > 0:
        items = items[:limit]
    return items


def parse_items(text: str) -> list[str]:
    import re

    match = re.search(r"\{.*\}", str(text or ""), re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except Exception:
        return []
    raw = data.get("worth_remembering")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(one).strip() for one in raw if str(one).strip()][:3]


def grade(expect: list[str], picked: list[str]) -> dict:
    blob = _norm(" ".join(picked))
    hits = [token for token in expect if _norm(token) in blob]
    return {
        "token_hit": len(hits),
        "token_total": len(expect),
        "exact": len(hits) == len(expect),
        "missed": [token for token in expect if token not in hits],
    }


async def run(limit: int, dry: bool) -> dict:
    items = load_items(limit)
    if dry:
        for it in items[:2]:
            print(PROMPT.format(content=(it.get("source") or {}).get("content", "")[:200]))
        return {"dry_run": True, "items": len(items)}
    from llm.client import LLMClient

    client = LLMClient()
    rows = []
    for it in items:
        content = str((it.get("source") or {}).get("content", ""))
        expect = [str(t) for t in (it.get("expect_tokens") or [])]
        answer = ""
        error = ""
        try:
            answer = await client.chat(
                system=SYSTEM,
                user=PROMPT.format(content=content),
                agent_name="probe_writer",
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        picked = parse_items(answer)
        rows.append(
            {
                "item_id": it.get("item_id", ""),
                "kind": it.get("kind", ""),
                "expect": expect,
                "picked": picked,
                "error": error,
                **grade(expect, picked),
            }
        )
        print(
            f"[probe] {it.get('item_id', '')[:20]} kind={it.get('kind')} "
            f"hit={rows[-1]['token_hit']}/{rows[-1]['token_total']} picked={picked}"
            + (f" error={error}" if error else "")
        )
    exact = sum(1 for row in rows if row["exact"])
    token_hit = sum(row["token_hit"] for row in rows)
    token_total = sum(row["token_total"] for row in rows)
    summary = {
        "items": len(rows),
        "exact": exact,
        "token_hit": token_hit,
        "token_total": token_total,
        "errors": sum(1 for row in rows if row["error"]),
        "baseline_old_writer": {"exact": 3, "items": 13},
        "b_route_deterministic": {"exact": 9, "token_hit": 11, "token_total": 15},
    }
    print(f"[probe] A 路线 exact={exact}/{len(rows)} token={token_hit}/{token_total}")
    return {"summary": summary, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    report = asyncio.run(run(args.limit, args.dry_run))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[probe] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
