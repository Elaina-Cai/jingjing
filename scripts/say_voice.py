# -*- coding: utf-8 -*-
"""把一句话变成一条语音条（wav + silk），存下来、想听就听。

用法：
    python scripts/say_voice.py --text "今天也慢慢来" --slug jingjing --play
    python scripts/say_voice.py --canon --slug jingjing
    python scripts/say_voice.py --recent 5

2026-10-26 定：发送只发文字；语音先在本地做成、能听、能翻旧账（C 方案）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech import voice_archive, voice_pack                     # noqa: E402
from core.speech.voice_message import build_voice_message            # noqa: E402
from core.speech.voice_send import voice_capability, send_voice      # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--text', default='')
    ap.add_argument('--slug', default='jingjing')
    ap.add_argument('--canon', action='store_true', help='用音色包里的 canon.txt 当台词')
    ap.add_argument('--play', action='store_true', help='生成后直接播一遍')
    ap.add_argument('--no-archive', action='store_true', help='只在缓存里，不复制进 voice_out')
    ap.add_argument('--recent', type=int, default=0, help='只列出最近 N 条存档')
    ap.add_argument('--platform', default='wechat')
    ap.add_argument('--send', action='store_true', help='尝试发送（微信会诚实返回不支持）')
    ap.add_argument('--target', default='')
    ap.add_argument('--out-dir', default='')
    args = ap.parse_args()

    if args.recent:
        print(json.dumps({'recent': voice_archive.recent(args.recent)}, ensure_ascii=False, indent=2))
        return 0

    text = args.text
    if args.canon or not text:
        text = voice_pack.canon_text(args.slug) or text
    built = build_voice_message(text, slug=args.slug, out_dir=args.out_dir)
    out = {'built': built}
    if built.get('ok'):
        if not args.no_archive:
            out['archived'] = voice_archive.save(built, label=Path(str(built['wav'])).stem[:12])
        if args.play:
            out['played'] = voice_archive.play(built['wav'])
        out['capability'] = voice_capability(args.platform)
        if args.send:
            out['send'] = send_voice(platform=args.platform, target=args.target,
                                     silk_path=built['silk'], text=built['spoken'])
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if built.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
