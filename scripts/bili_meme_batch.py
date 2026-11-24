# -*- coding: utf-8 -*-
"""梗指南批量管线：video_list.tsv -> 下载音频 -> GPU 转写 -> LLM 纠错+词条化 -> JSON（断点续跑）。"""
import argparse
import asyncio
import json
import os
import pathlib
import random
import re
import sys
import time

os.environ.setdefault("HF_HUB_OFFLINE", "1")
ROOT = pathlib.Path(__file__).resolve().parents[1]


def _add_cuda_dll_dirs():
    """把 .venv_it 里 nvidia-*-cu12 的 bin 目录加进 DLL 搜索路径和 PATH（ctranslate2 GPU 需要）。"""
    import os as _os
    import site as _site

    roots = []
    try:
        roots.extend(_site.getsitepackages())
    except Exception:
        pass
    roots.append(str(pathlib.Path(__file__).resolve().parents[1] / ".venv_it" / "Lib" / "site-packages"))
    added = []
    for root in roots:
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
            d = (pathlib.Path(root) / sub).resolve()
            if d.exists() and str(d) not in added:
                try:
                    _os.add_dll_directory(str(d))
                except Exception:
                    pass
                added.append(str(d))
    if added:
        _os.environ["PATH"] = ";".join(added) + ";" + _os.environ.get("PATH", "")
    return added


_CUDA_DIRS = _add_cuda_dll_dirs()
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=False)

LIST = ROOT / "data/web_memes/video_list.tsv"
COOKIES = ROOT / "data/web_memes/bili_cookies.txt"
AUDIO = ROOT / "data/web_memes/audio"
TRANSCRIPTS = ROOT / "data/web_memes/transcripts"
EXTRACTED = ROOT / "data/web_memes/extracted"

SYSTEM = ("你是中文网络梗词条的整理者。工作分两步：\n"
          "1) 纠错：按上下文修正 ASR 错字（只改字、不改意思、不删内容；网络用语/专名按最可能的原词还原；繁体转简体）；\n"
          "2) 抽梗：只提取视频里真正讲的梗，不要广告口播、不要主播寒暄。\n"
          "只输出一个 JSON 对象，不要多余文字。")
USER = ('输出格式：\n'
        '{"memes":[{"key":"梗名","meaning":"一句话含义","origin":"出处/背景","example":"视频里的典型例句","aliases":["别名"]}],"corrections":[{"wrong":"...","right":"..."}]}\n'
        '梗 1-3 条。视频标题里 XX是什么梗 的那个 XX 就是主梗，必须提取（除非视频确实没讲任何梗）。视频标题参考：{title}。转写如下：\n{transcript}')

_whisper = None


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_list(limit=0, ids=None):
    rows = []
    if not LIST.exists():
        return rows
    for line in LIST.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].startswith("BV"):
            rows.append({"bvid": parts[0], "title": parts[1], "created": parts[2] if len(parts) > 2 else ""})
    if ids:
        rows = [r for r in rows if r["bvid"] in ids]
    if limit:
        rows = rows[:limit]
    return rows


def download(bvid):
    import yt_dlp
    AUDIO.mkdir(parents=True, exist_ok=True)
    for p in AUDIO.glob(f"{bvid}.*"):
        if p.stat().st_size > 10000 and p.suffix in (".m4a", ".mp3", ".webm", ".aac"):
            return p
    opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best",
            "outtmpl": str(AUDIO / f"{bvid}.%(ext)s"), "cookiefile": str(COOKIES),
            "socket_timeout": 40, "retries": 3, "noprogress": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.bilibili.com/video/{bvid}"])
    for p in AUDIO.glob(f"{bvid}.*"):
        if p.stat().st_size > 10000:
            return p
    raise RuntimeError("audio not found")


def transcribe(bvid, audio_path, model_name="medium", device="cuda"):
    global _whisper
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    dest = TRANSCRIPTS / f"{bvid}.txt"
    if dest.exists() and dest.stat().st_size > 50:
        return dest
    from faster_whisper import WhisperModel
    if _whisper is None:
        compute = "int8_float16" if device == "cuda" else "int8"
        try:
            _whisper = WhisperModel(model_name, device=device, compute_type=compute, local_files_only=True)
        except Exception:
            snapshot = pathlib.Path.home() / ".cache/huggingface/hub/models--Systran--faster-whisper-medium/snapshots/08e178d48790749d25932bbc082711ddcfdfbc4f"
            _whisper = WhisperModel(str(snapshot), device=device, compute_type=compute)
    segments, _info = _whisper.transcribe(
        str(audio_path), language="zh", beam_size=5, vad_filter=True,
        initial_prompt="以下是B站《梗指南》视频的中文口播，包含网络流行语和梗名。",
    )
    lines = []
    for seg in segments:
        mm = int(seg.start // 60)
        ss = int(seg.start % 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {seg.text.strip()}")
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


def llm_client():
    from llm.client import LLMClient
    return LLMClient()


MEME_ONLY_SYSTEM = (
    "你是中文网络梗词条的整理者。只做一件事：从口播转写里抽出视频讲到的梗，"
    '输出 JSON 对象 {"memes":[{"key":"梗名","meaning":"一句话含义","origin":"出处/背景",'
    '"example":"视频里的典型例句","aliases":["别名"]}]}；'
    "视频标题点名的梗必须出现在 memes 里；梗 1-3 条；不要输出纠错。"
)
MEME_ONLY_USER = "视频标题：{title}\n转写如下：\n{transcript}"


def _apply_corrections(text, corrections):
    out = str(text or "")
    for item in corrections or ():
        if not isinstance(item, dict):
            continue
        wrong = str(item.get("wrong") or "")
        right = str(item.get("right") or "")
        if wrong and right and wrong != right:
            out = out.replace(wrong, right)
    return out


def parse_json(text):
    t = str(text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="")
    ap.add_argument("--sleep", type=float, default=8.0)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--model", default="medium")
    args = ap.parse_args()
    ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    rows = load_list(args.limit, ids)
    EXTRACTED.mkdir(parents=True, exist_ok=True)
    log(f"videos to process: {len(rows)}")
    client = None
    ok = fail = skipped = 0
    for r in rows:
        bvid, title = r["bvid"], r["title"]
        if (EXTRACTED / f"{bvid}.json").exists():
            skipped += 1
            continue
        try:
            t0 = time.time()
            audio = download(bvid)
            t1 = time.time()
            transcript_path = transcribe(bvid, audio, args.model, args.device)
            t2 = time.time()
            if client is None:
                client = llm_client()
            text = transcript_path.read_text(encoding="utf-8")
            prompt = USER.replace("{title}", title).replace("{transcript}", text)
            res = client.chat(system=SYSTEM, user=prompt,
                              temperature=0.3, max_tokens=2600, agent_name="meme_extract")
            if asyncio.iscoroutine(res):
                res = asyncio.run(res)
            data = parse_json(str(res))
            if not isinstance(data, dict):
                raise RuntimeError("LLM JSON parse failed: %r" % (str(res)[:200],))
            data.setdefault("corrections", [])
            data.setdefault("memes", [])
            if not data["memes"]:
                corrected = _apply_corrections(text, data["corrections"])
                meme_prompt = MEME_ONLY_USER.replace("{title}", title).replace("{transcript}", corrected)
                res2 = client.chat(system=MEME_ONLY_SYSTEM, user=meme_prompt,
                                   temperature=0.3, max_tokens=800, agent_name="meme_extract_only")
                m2 = parse_json(str(res2))
                if isinstance(m2, dict):
                    data["memes"] = m2.get("memes") or []
                print("[fallback] meme-only memes=%d" % len(data["memes"]))
            data["bvid"] = bvid
            data["title"] = title
            (EXTRACTED / f"{bvid}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            (EXTRACTED / f"{bvid}.txt").write_text(str(res), encoding="utf-8")
            ok += 1
            log(f"ok {bvid} | dl {t1 - t0:.0f}s asr {t2 - t1:.0f}s | memes {len(data.get('memes', []))} corr {len(data.get('corrections', []))}")
        except Exception as exc:
            fail += 1
            log(f"FAIL {bvid}: {type(exc).__name__}: {str(exc)[:150]}")
        time.sleep(args.sleep + random.random() * 4)
    log(f"DONE ok={ok} fail={fail} skipped={skipped}")


if __name__ == "__main__":
    main()
