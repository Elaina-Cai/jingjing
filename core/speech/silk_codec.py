# -*- coding: utf-8 -*-
# 语音条编码：wav -> silk（微信/QQ 要的就是这个格式）
#
# 为什么不用 ffmpeg：ffmpeg 不支持 silk，它是 Skype/SILK 私有编码。
# 用 pilk（纯 python 轮子，py3.14 也能装），tencent=True 时会在流前面加 \x02，
# 这个前导字节就是微信/QQ 认的 tencent 变体；没有它，客户端会当成非法文件。
from __future__ import annotations

import hashlib
import os
from pathlib import Path

CACHE_DIR = Path('data/silk_cache')
SILK_RATE = 24000          # 微信语音条实测就是 24 kHz 单声道
SILK_HEADERS = (b'\x02#!SILK_V3', b'#!SILK_V3')
_CHUNK = 1024 * 256


def available() -> bool:
    try:
        import pilk  # noqa: F401
        return True
    except Exception:
        return False


def is_silk(path) -> bool:
    try:
        p = Path(str(path))
        if not p.is_file() or p.stat().st_size < 12:
            return False
        head = p.open('rb').read(12)
    except OSError:
        return False
    return any(head.startswith(h) for h in SILK_HEADERS)


def _cache_path(path: Path) -> Path:
    h = hashlib.md5()
    with path.open('rb') as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / (h.hexdigest() + '.silk')


def wav_to_silk(wav_path, *, out_path='', pcm_rate: int = SILK_RATE, tencent: bool = True, use_cache: bool = True):
    # 返回要发给客户端的那条 silk 路径；失败返回 None（调用方必须诚实报失败）
    src = Path(str(wav_path))
    if not src.is_file():
        return None
    if out_path:
        dst = Path(str(out_path))
    elif use_cache:
        dst = _cache_path(src)
        if is_silk(dst):
            return dst
    else:
        dst = src.with_suffix('.silk')
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pilk
    except Exception:
        return None
    try:
        pilk.encode(str(src), str(dst), pcm_rate=int(pcm_rate), tencent=bool(tencent))
    except Exception:
        return None
    return dst if is_silk(dst) else None


def silk_duration_ms(silk_path) -> int:
    try:
        import pilk
        return int(pilk.get_duration(str(silk_path)) or 0)
    except Exception:
        return 0


def silk_to_wav(silk_path, *, out_path=''):
    src = Path(str(silk_path))
    if not is_silk(src):
        return None
    dst = Path(str(out_path)) if out_path else src.with_suffix('.decoded.wav')
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pilk
    except Exception:
        return None
    try:
        if hasattr(pilk, 'silk_to_wav'):
            pilk.silk_to_wav(str(src), str(dst))
        else:
            pilk.decode(str(src), str(dst))
    except Exception:
        return None
    return dst if dst.is_file() and dst.stat().st_size > 44 else None

def hash_text(text: str) -> str:
    # 文案指纹：同句话复用同一个 wav / silk，省一次 TTS
    return hashlib.md5(str(text or '').encode('utf-8')).hexdigest()[:16]
