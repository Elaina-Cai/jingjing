# -*- coding: utf-8 -*-
# 语音存档（2026-10-26 定：发送只发文字，语音先在本地做成、能听、能翻旧账）
#
# 存两个东西：
#   data/voice_out/<日期>/<时间>_<指纹>.wav   —— 能直接双击听的原始音频
#   data/voice_out/<日期>/<时间>_<指纹>.silk  —— 将来发 QQ 语音条要用的格式
#   data/voice_out/index.jsonl                —— 一行一条：说了什么、多长、用什么引擎
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

OUT_ROOT = Path('data/voice_out')
INDEX_NAME = 'index.jsonl'
KEEP_DAYS = 30


def _index() -> Path:
    return OUT_ROOT / INDEX_NAME


def save(built: dict, *, label: str = '', copy: bool = True) -> dict:
    # built 就是 voice_message.build_voice_message 的返回值；失败的直接返回原因，不落盘
    out = {'ok': False, 'reason': '', 'wav': '', 'silk': '', 'index': str(_index())}
    if not built or not built.get('ok'):
        out['reason'] = 'nothing_to_save'
        return out
    stamp = time.strftime('%Y%m%d-%H%M%S')
    day = time.strftime('%Y%m%d')
    folder = OUT_ROOT / day
    folder.mkdir(parents=True, exist_ok=True)
    base = '%s_%s' % (time.strftime('%H%M%S'), label or Path(str(built.get('wav') or 'x')).stem[:12])
    saved = {}
    for key, suffix in (('wav', '.wav'), ('silk', '.silk')):
        src = str(built.get(key) or '')
        if not src or not Path(src).is_file():
            continue
        dst = folder / (base + suffix)
        try:
            if copy:
                shutil.copy2(src, dst)
            else:
                dst = Path(src)
        except OSError:
            continue
        saved[key] = str(dst)
    if not saved:
        out['reason'] = 'copy_failed'
        return out
    row = {'at': stamp, 'label': str(label or ''), 'spoken': str(built.get('spoken') or ''),
           'duration_ms': int(built.get('duration_ms') or 0),
           'engine': str(built.get('engine') or ''), 'used_engine': str(built.get('used_engine') or ''),
           'voice': str(built.get('voice') or ''),
           'wav': saved.get('wav', ''), 'silk': saved.get('silk', '')}
    try:
        with _index().open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError:
        pass
    out.update({'ok': True, 'wav': row['wav'], 'silk': row['silk'], 'row': row})
    return out


def recent(limit: int = 10) -> list:
    p = _index()
    if not p.is_file():
        return []
    rows = []
    try:
        for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return rows[-limit:][::-1]


def play(path) -> bool:
    # 只在 Windows 上真放；别的地方/失败都返回 False，不做假动作
    p = Path(str(path))
    if not p.is_file():
        return False
    try:
        import winsound
    except Exception:
        return False
    try:
        winsound.PlaySound(str(p), winsound.SND_FILENAME)
        return True
    except Exception:
        return False


def purge(days: int = KEEP_DAYS) -> int:
    # 顺手清旧档：只删目录里的 wav/silk，不动 index.jsonl（历史账本留着）
    cutoff = time.time() - max(1, int(days)) * 86400
    removed = 0
    if not OUT_ROOT.is_dir():
        return 0
    for folder in OUT_ROOT.iterdir():
        if not folder.is_dir():
            continue
        try:
            if folder.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        for f in folder.iterdir():
            if f.suffix.lower() in ('.wav', '.silk'):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed
