# -*- coding: utf-8 -*-
"""抓取 B 站 up 主「梗指南」（mid=94510621）全部视频 BVID 列表（yt-dlp + Firefox cookie，慢速翻页）。"""
from __future__ import annotations

import pathlib
import time

from yt_dlp import YoutubeDL

OUT = pathlib.Path("data/web_memes/video_list.tsv")


def main() -> int:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "playlistend": 2000,
        "cookiesfrombrowser": ("firefox",),
        "socket_timeout": 30,
        "sleep_interval_requests": 2,
        "sleep_interval": 1,
        "max_sleep_interval": 4,
        "retries": 2,
    }
    started = time.time()
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://space.bilibili.com/94510621/video", download=False)
    entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{e[chr(39) + chr(105) + chr(100) + chr(39)] if False else e["id"]}\t{(e.get("title") or "").strip()}" for e in entries]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"TOTAL {len(entries)} elapsed {time.time() - started:.0f}s -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
