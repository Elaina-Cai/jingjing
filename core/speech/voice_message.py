# -*- coding: utf-8 -*-
# 一句话 -> 一条语音条（wav + silk），不含发送
#
# 分工：tts_text 决定能不能念、念什么；tts_engine 出 wav；silk_codec 转微信格式。
# 发送在 voice_send 里，且会诚实报能力（微信 PC 没有发语音的接口）。
from __future__ import annotations

from pathlib import Path

from core.speech import silk_codec, tts_engine
from core.speech.tts_text import has_silence_marker, normalize_for_speech
from core.speech import voice_pack
import structlog

log = structlog.get_logger()


def _fail(out: dict, reason: str) -> dict:
    out['reason'] = reason
    log.warning('voice_build_failed', reason=reason, engine=str(out.get('engine') or ''),
                chars=len(out.get('spoken') or ''))
    return out


def build_voice_message(text: str, *, slug: str = '', out_dir: str = '', want_engine: str = '',
                        max_chars: int = 200) -> dict:
    out = {'ok': False, 'reason': '', 'spoken': '', 'wav': '', 'silk': '',
           'duration_ms': 0, 'engine': '', 'used_engine': '', 'voice': ''}
    raw = str(text or '')
    if has_silence_marker(raw):
        out['reason'] = 'silence'
        return out
    spoken = normalize_for_speech(raw, max_chars=max_chars)
    out['spoken'] = spoken
    if not spoken:
        return _fail(out, 'nothing_to_say')
    if not silk_codec.available():
        return _fail(out, 'silk_encoder_missing')
    style = voice_pack.load_pack(slug) or {}
    target = Path(str(out_dir)) if out_dir else Path('data/tts_cache')
    engine_name = want_engine or str(style.get('engine') or '')
    wav, used = tts_engine.synth_ex(spoken, engine=engine_name,
                           voice=str(style.get('voice') or ''),
                           rate=str(style.get('rate') or ''),
                           pitch=str(style.get('pitch') or ''),
                           sapi_voice=str(style.get('sapi_voice') or ''),
                           out_path=str(target / (silk_codec.hash_text(spoken) + '.wav')))
    out['engine'] = engine_name
    out['used_engine'] = used
    out['voice'] = str(style.get('voice') or '')
    if wav is None:
        return _fail(out, 'tts_failed')
    out['wav'] = str(wav)
    silk = silk_codec.wav_to_silk(wav)
    if silk is None:
        return _fail(out, 'silk_failed')
    out['silk'] = str(silk)
    out['duration_ms'] = silk_codec.silk_duration_ms(silk)
    out['ok'] = True
    return out
