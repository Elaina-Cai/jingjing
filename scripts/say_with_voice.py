"""把一句话合成成音频（试音色 / 验证 TTS 链路）。

    python scripts/say_with_voice.py --text "今天也慢慢来。"
    python scripts/say_with_voice.py --slug jingjing --canon

只合成，不发送（发送要走 OutboundGateway，见 core/speech/tts_engine.py 的说明）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="")
    ap.add_argument("--slug", default="")
    ap.add_argument("--canon", action="store_true", help="念音色包的 canon.txt（参考文本）")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    from core.speech import voice_pack
    from core.speech import tts_engine
    from core.speech.tts_text import normalize_for_speech

    pack = voice_pack.load_pack(args.slug)
    text = args.text or (voice_pack.canon_text(args.slug) if args.canon else "")
    spoken = normalize_for_speech(text)
    if not spoken:
        print(json.dumps({"ok": False, "why": "清洗后没有可念的内容（或这句是静默标记）"},
                         ensure_ascii=False))
        return 2
    path = tts_engine.synth(spoken, engine=pack.get("engine", ""), voice=pack["voice"],
                            rate=pack["rate"], pitch=pack["pitch"],
                            sapi_voice=pack.get("sapi_voice", ""), out_path=args.out)
    used = ""
    if path:
        used = "edge" if str(path).lower().endswith(".mp3") else "sapi"
    print(json.dumps({"ok": bool(path), "spoken": spoken, "path": str(path or ""),
                      "want_engine": pack.get("engine"), "used_engine": used,
                      "voice": pack.get("voice"), "sapi_voice": pack.get("sapi_voice"),
                      "available": tts_engine.available()}, ensure_ascii=False))
    return 0 if path else 3


if __name__ == "__main__":
    raise SystemExit(main())
