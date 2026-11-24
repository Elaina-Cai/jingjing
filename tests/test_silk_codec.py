# -*- coding: utf-8 -*-
# silk 编码链路：只要 pilk 在，就必须真的产出微信认的 silk
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from core.speech import silk_codec, tts_engine, voice_message, voice_send


def _tone(path: Path, *, seconds: float = 0.6, rate: int = 24000, freq: float = 440.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(rate * seconds)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b''.join(struct.pack('<h', int(6000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))
        w.writeframes(frames)
    return path


needs_pilk = pytest.mark.skipif(not silk_codec.available(), reason='pilk 没装')


@needs_pilk
def test_wav_to_silk_has_tencent_header(tmp_path):
    wav = _tone(tmp_path / 'a.wav')
    silk = silk_codec.wav_to_silk(wav, out_path=tmp_path / 'a.silk', use_cache=False)
    assert silk is not None
    head = Path(silk).read_bytes()[:10]
    assert head.startswith(b'\x02#!SILK_V3')
    assert silk_codec.is_silk(silk) is True


@needs_pilk
def test_duration_is_plausible(tmp_path):
    wav = _tone(tmp_path / 'b.wav', seconds=0.6)
    silk = silk_codec.wav_to_silk(wav, out_path=tmp_path / 'b.silk', use_cache=False)
    ms = silk_codec.silk_duration_ms(silk)
    assert 300 <= ms <= 1200, ms


def test_is_silk_rejects_wav(tmp_path):
    wav = _tone(tmp_path / 'c.wav')
    assert silk_codec.is_silk(wav) is False
    assert silk_codec.is_silk(tmp_path / 'nope.silk') is False


@needs_pilk
def test_build_voice_message_end_to_end(tmp_path, monkeypatch):
    def fake_synth(text, **kw):
        return _tone(Path(kw['out_path']), seconds=0.5)

    monkeypatch.setattr(tts_engine, 'synth', fake_synth)
    out = voice_message.build_voice_message('今天也慢慢来，不着急。', out_dir=str(tmp_path))
    assert out['ok'] is True, out
    assert out['spoken']
    assert silk_codec.is_silk(out['silk'])
    assert out['duration_ms'] > 0


def test_silence_marker_is_not_spoken(tmp_path):
    out = voice_message.build_voice_message('[静默]', out_dir=str(tmp_path))
    assert out['ok'] is False
    assert out['reason'] == 'silence'


def test_wechat_capability_is_honest():
    cap = voice_send.voice_capability('wechat')
    assert cap['supported'] is False
    assert cap['reason'] == 'wechat_no_voice_api'
    sent = voice_send.send_voice(platform='wechat', target='x', silk_path='y.silk')
    assert sent['ok'] is False and sent['sent'] is False


def test_unknown_platform_is_refused():
    cap = voice_send.voice_capability('telegram')
    assert cap['supported'] is False
    assert cap['reason'] == 'unknown_platform'


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
