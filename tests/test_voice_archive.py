# -*- coding: utf-8 -*-
# 本地语音存档：能听、能翻旧账、失败不假装
from __future__ import annotations

import json
from pathlib import Path

from core.speech import voice_archive


def _built(tmp_path: Path, seconds: int = 1500) -> dict:
    wav = tmp_path / 'x.wav'
    wav.write_bytes(b'RIFF' + b'0' * 200)
    silk = tmp_path / 'x.silk'
    silk.write_bytes(b'\x02#!SILK_V3' + b'0' * 100)
    return {'ok': True, 'spoken': '今天也慢慢来。', 'wav': str(wav), 'silk': str(silk),
            'duration_ms': seconds, 'engine': 'sapi', 'used_engine': 'sapi', 'voice': 'huihui'}


def test_save_copies_and_indexes(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_archive, 'OUT_ROOT', tmp_path / 'voice_out')
    res = voice_archive.save(_built(tmp_path), label='demo')
    assert res['ok'] is True, res
    assert Path(res['wav']).is_file() and Path(res['silk']).is_file()
    rows = voice_archive.recent(5)
    assert rows and rows[0]['spoken'] == '今天也慢慢来。'
    assert rows[0]['duration_ms'] == 1500
    index = tmp_path / 'voice_out' / 'index.jsonl'
    assert json.loads(index.read_text(encoding='utf-8').splitlines()[0])['label'] == 'demo'


def test_save_refuses_failed_build(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_archive, 'OUT_ROOT', tmp_path / 'voice_out')
    res = voice_archive.save({'ok': False, 'reason': 'silence'})
    assert res['ok'] is False and res['reason'] == 'nothing_to_save'
    assert voice_archive.recent(3) == []


def test_play_missing_file_is_false(tmp_path):
    assert voice_archive.play(tmp_path / 'nope.wav') is False


def test_purge_only_removes_old_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_archive, 'OUT_ROOT', tmp_path / 'voice_out')
    res = voice_archive.save(_built(tmp_path), label='old')
    folder = Path(res['wav']).parent
    import os
    old = 0
    os.utime(folder, (old, old))
    removed = voice_archive.purge(days=1)
    assert removed >= 1
    assert not Path(res['wav']).exists()
    assert (tmp_path / 'voice_out' / 'index.jsonl').is_file()


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
