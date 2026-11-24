# -*- coding: utf-8 -*-
r"""层2：逐字稿切段 -> data/web_memes/chunks/<bvid>.jsonl（300-500 字/段，带时间戳）。

用法：.venv_it\Scripts\python.exe scripts\chunk_transcripts.py
"""
from __future__ import annotations

import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TR = ROOT / "data/web_memes/transcripts"
OUT = ROOT / "data/web_memes/chunks"
_LINE = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*(.*)$")
TARGET = 300


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    n_files = n_chunks = 0
    for fp in sorted(TR.glob("*.txt")):
        rows = []
        for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _LINE.match(line.strip())
            if m:
                mm, ss, hh, text = m.group(1), m.group(2), m.group(3) or "", m.group(4).strip()
                rows.append((mm + ":" + ss + (":" + hh if hh else ""), text))
        chunks = []
        cur, t0, size = [], "", 0
        for t, text in rows:
            if not text:
                continue
            if not t0:
                t0 = t
            cur.append(text)
            size += len(text)
            if size >= TARGET:
                chunks.append({"bvid": fp.stem, "idx": len(chunks), "t_start": t0, "t_end": t,
                               "text": "".join(cur)})
                cur, t0, size = [], "", 0
        if cur:
            chunks.append({"bvid": fp.stem, "idx": len(chunks), "t_start": t0, "t_end": "",
                           "text": "".join(cur)})
        if chunks:
            (OUT / (fp.stem + ".jsonl")).write_text(
                "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks), encoding="utf-8")
            n_files += 1
            n_chunks += len(chunks)
    print(f"chunks built: files={n_files} chunks={n_chunks} -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
