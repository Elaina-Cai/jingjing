"""生成层因果替换实验（只读，不写任何库）。

问题：出稿层（agents/conversational.py）拿到的【摘要载体】与【原文】相比，
决策会不会不一样？差多少？

设计（v2，2026-10-16）：
  base    = 生产形态的 system prompt：真实 chat_window 文本进 _group_recent_context
  raw     = base + 一段「历史原文」（含目标事实的原话）
  summary = base + 一行「她记下来的手记」（同一事实的摘要形态）
  memory  = base + [按需记忆] 命中块（召回链真的带回这条事实时的形态）

判定：同一句提问，三种输入下回答里是否出现目标事实 token、是否承认不知道、
与 base 的草稿相似度（Jaccard 3-gram）。

用法（必须在隔离库 + 私有 TEMP 下跑，见 docs/decisions/让一个决策点读到原文.md §9）：
  META_DB_PATH/DATA_DIR/PLATFORMS_DIR/LOG_DIR 指向隔离根，TEMP 指向可写空目录，
  然后 python -X utf8 scripts/probe_layer_causality.py --owner --runs 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("YINYUE_FEATURE_MEMORY_VECTOR_RECALL", "0")

DEFAULT_QUESTION = "我课表是哪一学期的来着？"
DEFAULT_FACT_TOKENS = "2026-2027"
DEFAULT_SUMMARY = (
    "[她记的手记] 主人 2026-10-15 发过课表《我的课表_2026-2027-1_从今天起.xlsx》，"
    "当时他问我这文件是要整理还是先存着。"
)


def _read_rows(chat_id: str, limit: int = 4000) -> list[dict]:
    from sqlalchemy import text as sa_text

    from core.bootstrap import get_meta_db_path
    from db.write_guard import read_only_engine

    engine = read_only_engine(get_meta_db_path())
    with engine.connect() as conn:
        rows = conn.execute(
            sa_text(
                "select event_id, occurred_at, event_type, actor_person_id, content, chat_type "
                "from terminal_events where chat_id = :c "
                "and event_type in ('message','question','reply','request') "
                "and content is not null and content != '' "
                "order by occurred_at, sequence limit :n"
            ),
            {"c": chat_id, "n": limit},
        ).fetchall()
        names = {}
        try:
            for pid, ident, disp in conn.execute(
                sa_text(
                    "select i.person_id, i.identity_value, e.display_name "
                    "from person_identities i "
                    "left join person_entities e on e.id = i.person_id "
                    "where i.identity_type = 'wxid'"
                )
            ).fetchall():
                if pid:
                    names[str(pid)] = str(disp or ident or "")
        except Exception:
            names = {}
    out = []
    for event_id, occurred_at, event_type, actor, content, chat_type in rows:
        out.append(
            {
                "event_id": str(event_id),
                "at": str(occurred_at),
                "type": str(event_type),
                "actor": str(actor or ""),
                "name": names.get(str(actor or ""), "") or str(actor or "?"),
                "content": str(content or ""),
                "chat_type": str(chat_type or ""),
            }
        )
    return out


def _window_text(chat_id: str) -> str:
    from core.context.chat_window import build_window_parts

    return str(build_window_parts(chat_id).text_full or "")


def _ngrams(text: str, n: int = 3) -> set[str]:
    s = "".join(str(text or "").split())
    return {s[i : i + n] for i in range(max(0, len(s) - n + 1))}


def _jaccard(a: str, b: str) -> float:
    na, nb = _ngrams(a), _ngrams(b)
    if not na or not nb:
        return 1.0
    return len(na & nb) / len(na | nb)


def _append_block(prompt: str, block: str) -> str:
    from agents.conversational import _append_prompt_block

    return _append_prompt_block(prompt, block)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", default="wxid_seed01")
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    ap.add_argument("--fact-tokens", default=DEFAULT_FACT_TOKENS)
    ap.add_argument("--summary-line", default=DEFAULT_SUMMARY)
    ap.add_argument("--fact-span", type=int, default=6, help="原文块取目标行前后各几条")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--sender", default="")
    ap.add_argument("--owner", action="store_true", default=None)
    ap.add_argument("--out", default=str(ROOT / ".test_tmp" / "layer_causality_report.json"))
    args = ap.parse_args()

    chat_id = args.chat
    is_group = chat_id.endswith("@chatroom")
    tokens = [t for t in str(args.fact_tokens).split(",") if t]

    rows = _read_rows(chat_id)
    if not rows:
        print("没有可用历史，退出")
        return 2
    inbound = [r for r in rows if r["type"] in ("message", "question") and r["content"]]
    last = inbound[-1]

    window = _window_text(chat_id)
    fact_idx = None
    for i, r in enumerate(rows):
        if any(t in r["content"] for t in tokens) and r["type"] in ("message", "question"):
            fact_idx = i
    if fact_idx is None:
        print(f"历史里找不到含 {tokens} 的行，退出")
        return 2
    fact_row = rows[fact_idx]
    span = max(1, args.fact_span)
    excerpt = rows[max(0, fact_idx - span) : fact_idx + span + 1]
    raw_block = "[历史原文·仅作上下文]\n" + "\n".join(
        f"{r['at'][:16]} {r['name']}: {r['content']}" for r in excerpt
    )

    from core.bootstrap import build_agent
    from core.config.settings import config
    from core.speech.turn import snapshot_from_context

    agent = build_agent()
    sender_id = args.sender or last["actor"]
    sender_name = last["name"]
    extra = {
        "platform": "wechat",
        "account_id": config.wechat.yinyue_wxid,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "user_message": args.question,
        "is_group": is_group,
        "group_id": chat_id if is_group else "",
    }
    context = SimpleNamespace(
        extra=extra,
        account_context=None,
        get=lambda key, default=None: extra.get(key, default),
    )
    snapshot = snapshot_from_context(
        context,
        chat_type="group" if is_group else "private",
        is_owner=bool(args.owner),
        is_mentioned=True,
        group_recent_context=window,
    )
    agent._last_context = context
    agent._inbound_content = str(args.question)
    agent._group_recent_context = window
    agent._speech_snapshot = snapshot

    base = agent._build_system_prompt_from_snapshot(snapshot, is_group)
    headers = [b.strip().splitlines()[0] for b in base.split("\n\n") if b.strip().startswith("[")]
    fact_in_base = any(t in base for t in tokens)

    from agents.conversational import _append_prompt_block

    variants: dict[str, str] = {"none": base}
    variants["raw"] = _append_prompt_block(base, raw_block)
    variants["summary"] = _append_prompt_block(base, args.summary_line)
    # 2026-10-24：「按需记忆」注入块（原 `_needs_memory_block`）随读场整族退役，
    # memory 变体跟着消失——记忆入口改走工具（模型自己调 recall_memories）。

    out_dir = Path(args.out).parent / "layer_causality_prompts"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in variants.items():
        (out_dir / f"{name}.txt").write_text(text, encoding="utf-8")

    async def _run(prompt: str) -> str:
        return await agent.llm.chat(
            system=prompt,
            user=args.question,
            agent_name="conversational",
            temperature=0.4,
            max_tokens=1000,
        )

    results: dict[str, list[dict]] = {}
    for name, prompt in variants.items():
        runs = []
        for _ in range(max(1, args.runs)):
            try:
                draft = str(asyncio.run(_run(prompt)) or "")
            except Exception as exc:  # noqa: BLE001
                draft = f"[ERROR] {type(exc).__name__}: {exc}"
            runs.append(
                {
                    "draft": draft,
                    "fact_hit": any(t in draft for t in tokens),
                    "admission": any(
                        m in draft
                        for m in ("不知道", "不记得", "记不得", "没听说", "没记录", "不清楚", "没存", "没有", "查不到", "翻不出", "我哪知道", "我这儿没", "不在我手上")
                    ),
                    "chars": len(draft),
                }
            )
        results[name] = runs
        print(f"[{name}] " + " | ".join(r["draft"][:70].replace("\n", " ") for r in runs))

    base_drafts = [r["draft"] for r in results["none"]]
    summary: dict[str, dict] = {}
    for name, runs in results.items():
        dists = [_jaccard(r["draft"], b) for r in runs for b in base_drafts]
        summary[name] = {
            "fact_hit_rate": sum(1 for r in runs if r["fact_hit"]) / len(runs),
            "admission_rate": sum(1 for r in runs if r["admission"]) / len(runs),
            "avg_chars": sum(r["chars"] for r in runs) / len(runs),
            "jaccard_vs_none": sum(dists) / len(dists) if dists else 0.0,
        }
    noise = [
        _jaccard(a, b) for i, a in enumerate(base_drafts) for b in base_drafts[i + 1 :]
    ]
    report = {
        "chat": chat_id,
        "question": args.question,
        "fact_tokens": tokens,
        "fact_row": {k: fact_row[k] for k in ("at", "name", "content")},
        "fact_in_base_prompt": fact_in_base,
        "window_chars": len(window),
        "raw_excerpt_rows": len(excerpt),
        "runs": args.runs,
        "base_headers": headers,
        "noise_floor_jaccard_none_pairwise": noise,
        "summary": summary,
        "raw": results,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n事实在 base 里：{fact_in_base}；窗口 {len(window)} 字；原文块 {len(excerpt)} 行")
    print(f"噪声底：{[round(x, 3) for x in noise]}")
    for name, s in summary.items():
        print(
            f"{name:9s} 事实命中 {s['fact_hit_rate']:.0%}  承认不知道 {s['admission_rate']:.0%}  "
            f"均长 {s['avg_chars']:.0f}  与 base 相似度 {s['jaccard_vs_none']:.3f}"
        )
    print(f"report -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
