# -*- coding: utf-8 -*-
r"""层3：构建 web 梗词条索引（去重合并 + 标题优先 + 热度）。

用法：.venv_it\Scripts\python.exe scripts\build_meme_index.py
读 data/web_memes/extracted/*.json（批量管线产物）+ video_list.tsv（补 title/created），
写 data/web_memes/memes_index.json。status 默认 pending；已存在索引里的 status 保留。
"""
from __future__ import annotations

import json
import pathlib
import re
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXTRACTED = ROOT / "data/web_memes/extracted"
INDEX = ROOT / "data/web_memes/memes_index.json"
VIDEO_LIST = ROOT / "data/web_memes/video_list.tsv"

_PUNCT = " \t\r\n-_·・，。！？!?、；;：:（）()【】[]「」\u201c\u201d\"'"


def norm(s) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch not in _PUNCT)


def title_meme(title) -> str:
    m = re.search(r"([^:：\[\]【】]{1,24}?)\s*是什么梗", str(title or ""))
    return m.group(1).strip() if m else ""


def _merge(existing: dict, item: dict) -> None:
    for k in ("meaning", "origin", "example"):
        if len(str(item.get(k) or "")) > len(str(existing.get(k) or "")):
            existing[k] = str(item.get(k) or "")


def main() -> int:
    old_status = {}
    if INDEX.exists():
        try:
            for e in json.loads(INDEX.read_text(encoding="utf-8")).get("entries", []):
                old_status[norm(e.get("key"))] = e.get("status", "pending")
        except Exception:
            pass
    meta = {}
    if VIDEO_LIST.exists():
        for line in VIDEO_LIST.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].strip():
                meta[parts[0].strip()] = (parts[1], int(parts[2] or 0))
    buckets = {}
    files = sorted(EXTRACTED.glob("*.json"))
    for fp in files:
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        bvid = str(d.get("bvid") or fp.stem)
        title = str(d.get("title") or "") or (meta.get(bvid, ("", 0))[0])
        created = int(d.get("created") or 0) or meta.get(bvid, ("", 0))[1]
        video = {"bvid": bvid, "title": title, "created": created}
        tkey = title_meme(title)
        for m in (d.get("memes") or []):
            if not isinstance(m, dict):
                continue
            key = str(m.get("key") or "").strip()
            if not key:
                continue
            aliases = [str(a).strip() for a in (m.get("aliases") or []) if str(a).strip()]
            canonical = key
            if tkey and any(norm(k) == norm(tkey) for k in [key, *aliases]):
                canonical = tkey
            bkey = norm(canonical)
            extra = [key] if norm(key) != bkey else []
            b = buckets.get(bkey)
            if b is None:
                b = {
                    "key": canonical,
                    "aliases": sorted({a for a in [*aliases, *extra] if norm(a) != bkey}, key=len, reverse=True),
                    "meaning": str(m.get("meaning") or ""),
                    "origin": str(m.get("origin") or ""),
                    "example": str(m.get("example") or ""),
                    "videos": [],
                    "mentions": 0,
                    "first_seen": created or 0,
                    "last_seen": created or 0,
                    "status": old_status.get(bkey, "pending"),
                }
                buckets[bkey] = b
            else:
                _merge(b, m)
                for a in [*aliases, *extra]:
                    if norm(a) != bkey and a not in b["aliases"]:
                        b["aliases"].append(a)
            b["videos"] = [v for v in b["videos"] if v["bvid"] != bvid] + [video]
            b["mentions"] += 1
            if created:
                b["first_seen"] = min(b["first_seen"] or created, created)
                b["last_seen"] = max(b["last_seen"], created)
    entries = sorted(buckets.values(), key=lambda e: (-e["mentions"], -e["last_seen"]))
    for e in entries:
        e["heat"] = e["mentions"]
    out = {"generated_at": int(time.time()), "videos": len(files), "count": len(entries), "entries": entries}
    INDEX.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    pending = sum(1 for e in entries if e["status"] != "approved")
    print(f"index built: videos={len(files)} entries={len(entries)} pending={pending} -> {INDEX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
