"""音色包：`data/persona/assets/<slug>/voice/`。

2026-10-26 P4：和形象包（faces）对称——代码里不写死任何角色。卡片用
`"voice": "<slug>"` 引用；没有包就退回默认音色，功能不缺口。

包里有什么：
  pack.json  {"engine": "edge", "voice": "zh-CN-XiaoyiNeural", "rate": "+0%",
              "pitch": "+0Hz", "style": "软乎乎、慢半拍；正经事立刻板起脸"}
  canon.txt  音色参考文本（念这段话最像她；换本地 GPT-SoVITS 时用它当参考音频的台词）
  ref/*.wav  参考音频（可选，edge-tts 不需要；本地音色克隆要用）
"""

from __future__ import annotations

import json
from pathlib import Path

from safelog import warn

from core.assets import paths as _asset_paths  # 2026-10-26 资产分区：单一解析入口
_FALLBACK = {
    "engine": "edge",
    "voice": "zh-CN-XiaoyiNeural",
    "rate": "+0%",
    "pitch": "+0Hz",
    "style": "",
}


def asset_root() -> Path:
    return _asset_paths.asset_root()


def voice_dir(slug: str = "") -> Path:
    return _asset_paths.kind_path(slug, "voice")


def load_pack(slug: str = "") -> dict:
    """读音色包；没有/坏了就返回默认设置（保证调用方永远拿得到能用的东西）。"""
    try:
        path = voice_dir(slug) / "pack.json"
        if not path.is_file():
            return dict(_FALLBACK)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(_FALLBACK)
        out = dict(_FALLBACK)
        out.setdefault("sapi_voice", "")
        for key in ("engine", "voice", "rate", "pitch", "style", "sapi_voice"):
            if data.get(key):
                out[key] = str(data[key])
        fallbacks = data.get("fallbacks")
        if isinstance(fallbacks, list):
            out["fallbacks"] = [str(x) for x in fallbacks]
        return out
    except Exception as exc:  # noqa: BLE001
        warn("core/speech/voice_pack.py load failed", err=repr(exc)[:200])
        return dict(_FALLBACK)


def canon_text(slug: str = "", *, budget: int = 400) -> str:
    """念这段话最像她（换本地音色时的参考台词）。"""
    try:
        path = voice_dir(slug) / "canon.txt"
        if not path.is_file():
            return ""
        text = " ".join(path.read_text(encoding="utf-8").split())
        return text[: max(0, int(budget))] if budget else text
    except Exception:  # noqa: BLE001
        return ""


def ref_files(slug: str = "") -> list[Path]:
    """参考音频（本地音色克隆用；edge-tts 不需要）。"""
    try:
        d = voice_dir(slug) / "ref"
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir() if p.suffix.lower() in (".wav", ".mp3", ".flac"))
    except Exception:  # noqa: BLE001
        return []


def voice_of(slug: str = "") -> str:
    return load_pack(slug).get("voice") or _FALLBACK["voice"]


def style_hint(slug: str = "") -> str:
    """给文字层的语气提示（TTS 只是把它念出来，不该由引擎决定语气）。"""
    return load_pack(slug).get("style") or ""
