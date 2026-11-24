"""核对 `remember` 是否走通：事件流里写了没有、下一拍提示词里读回来没有。

只读。用法：

    python scripts\verify_remember_roundtrip.py

流程：
1. 在 `data/yinyue_meta.db::terminal_events` 找最近的 `memory_write` 事件；
2. 找该事件之后、同一会话的回合提示词（`data/prompt/<turn>/request.json`）；
3. 看提示词里有没有这条正文——有就说明「记住 → 下一拍读回」整条链通了。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def latest_writes(meta_db: Path, limit: int = 5) -> list[dict]:
    conn = sqlite3.connect(meta_db.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "select id, event_id, platform, bot_account_id, chat_id, content, occurred_at "
            "from terminal_events where event_type='memory_write' order by id desc limit ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _prompt_texts(prompt_dir: Path) -> list[tuple[str, str, float]]:
    out = []
    if not prompt_dir.is_dir():
        return out
    for d in prompt_dir.iterdir():
        req = d / "request.json"
        if not req.is_file():
            continue
        try:
            data = json.loads(req.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            continue
        texts = []
        for r in (data.get("rounds") or []):
            for m in (r.get("messages") or []):
                texts.append(str(m.get("content") or ""))
        out.append((d.name, "\n".join(texts), req.stat().st_mtime))
    return sorted(out, key=lambda x: x[2], reverse=True)


def roundtrip(meta_db: Path, prompt_dir: Path, *, limit: int = 5) -> list[dict]:
    writes = latest_writes(meta_db, limit=limit)
    prompts = _prompt_texts(prompt_dir)
    out = []
    for w in writes:
        content = str(w.get("content") or "")
        at = str(w.get("occurred_at") or "")
        seen_in = ""
        for name, text, _mtime in prompts:
            if content and content[:40] in text:
                seen_in = name
                break
        out.append({"event_id": w.get("event_id"), "chat_id": w.get("chat_id"),
                    "at": at, "content": content[:60], "seen_in_prompt": seen_in})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--meta-db", default=str(ROOT / "data" / "yinyue_meta.db"))
    ap.add_argument("--prompt-dir", default=str(ROOT / "data" / "prompt"))
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    rows = roundtrip(Path(args.meta_db), Path(args.prompt_dir), limit=args.limit)
    if not rows:
        print("没有 memory_write 事件——这段时间她没调用过 remember。")
        return 0
    for r in rows:
        mark = "READ-BACK" if r["seen_in_prompt"] else "no-prompt-yet"
        print("%s | %s | %s | %s | %s" % (r["at"], r["chat_id"], mark, r["seen_in_prompt"], r["content"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
