"""P4/P5 的回归：能念的字、音色包、角色包 zip。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from core.identity import pack_zip
from core.speech import tts_text, voice_pack


# ---------------- 哪些字能念 ----------------

def test_emoji_and_brackets_are_dropped():
    spoken = tts_text.normalize_for_speech('好耶😀（笑着）我们走吧！')
    assert '😀' not in spoken
    assert '笑着' not in spoken
    assert '好耶' in spoken and '我们走吧' in spoken


def test_markdown_and_urls_are_dropped():
    spoken = tts_text.normalize_for_speech('看 **[这里](http://x.y/z)** https://a.b/c 结尾')
    assert 'http' not in spoken
    assert '**' not in spoken
    assert '这里' in spoken and '结尾' in spoken


def test_silence_markers_mean_no_speech():
    for marker in ('[静默]', '[silent]', '（静默）', '好，[不说话]'):
        assert tts_text.normalize_for_speech(marker) == ''
        assert tts_text.has_silence_marker(marker)
    assert not tts_text.is_speakable('[静默]')
    assert tts_text.is_speakable('嗯，我在。')


def test_long_text_is_cut_at_sentence_end():
    long = '今天很安静。' * 60
    spoken = tts_text.normalize_for_speech(long, max_chars=50)
    assert len(spoken) <= 50
    assert spoken.endswith('。')


def test_empty_and_whitespace_is_safe():
    assert tts_text.normalize_for_speech('') == ''
    assert tts_text.normalize_for_speech('   \n  ') == ''
    assert tts_text.normalize_for_speech(None) == ''


# ---------------- 音色包 ----------------

def test_voice_pack_defaults_when_missing(tmp_path):
    pack = voice_pack.load_pack('no_such_slug_xyz')
    assert pack['voice'] == voice_pack._FALLBACK['voice']
    assert voice_pack.voice_of('no_such_slug_xyz')
    assert voice_pack.canon_text('no_such_slug_xyz') == ''


def test_voice_pack_reads_shipped_pack():
    # 2026-10-26：出厂包从 edge 切到了 cosyvoice3（本地 WSL 服务，cv3.ping() 为真），
    # 断言改成「引擎必须是运行时认识的那几个之一」——比写死 'edge' 更严，也不会一换引擎就红。
    from core.speech import tts_engine as _te

    pack = voice_pack.load_pack('jingjing')
    assert pack['engine'] in _te.ENGINES, '出厂包的 engine=%r 不在 %r 里' % (pack['engine'], _te.ENGINES)
    assert pack['voice']
    assert voice_pack.canon_text('jingjing')


# ---------------- 角色包 zip ----------------

def _make_pack(tmp_path: Path, slug: str = 'demo') -> Path:
    root = tmp_path / 'assets'
    faces = root / slug / 'faces'
    (faces / 'gallery').mkdir(parents=True)
    (faces / 'pack.json').write_text(json.dumps(
        {'kind': 'faces', 'name': '演示', 'license': {'redistribute': True}}), encoding='utf-8')
    (faces / 'canon.txt').write_text('我有圆耳朵。', encoding='utf-8')
    (faces / 'gallery' / '01.png').write_bytes(b'PNG-FAKE')
    voice = root / slug / 'voice'
    voice.mkdir(parents=True)
    (voice / 'pack.json').write_text(json.dumps(
        {'voice': 'zh-CN-XiaoyiNeural', 'license': {'redistribute': True}}), encoding='utf-8')
    return root


def test_export_then_import_roundtrip(tmp_path):
    src = _make_pack(tmp_path, 'demo')
    data, name = pack_zip.export_pack_bytes('demo', root=src)
    assert data and name.endswith('.zip')
    dst = tmp_path / 'imported'
    out = pack_zip.import_pack(data, root=dst)
    assert out['ok'], out
    assert out['slug'] == 'demo'
    files = {Path(p).name for p in out['written']}
    assert {'pack.json', 'canon.txt', '01.png'} <= files
    assert (dst / 'demo' / 'faces' / 'canon.txt').read_text(encoding='utf-8') == '我有圆耳朵。'


def test_import_refuses_overwrite_without_flag(tmp_path):
    src = _make_pack(tmp_path, 'demo')
    data, _name = pack_zip.export_pack_bytes('demo', root=src)
    out1 = pack_zip.import_pack(data, root=tmp_path / 'dst')
    assert out1['ok']
    out2 = pack_zip.import_pack(data, root=tmp_path / 'dst')
    assert not out2['ok'] and '已存在' in out2['why']
    out3 = pack_zip.import_pack(data, root=tmp_path / 'dst', overwrite=True)
    assert out3['ok'], out3


def test_import_rejects_path_traversal(tmp_path):
    buf = tmp_path / 'evil.zip'
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('evil/faces/../../escape.txt', 'boom')
        zf.writestr('evil/faces/pack.json', '{}')
    out = pack_zip.import_pack(buf, root=tmp_path / 'dst')
    assert out['ok']
    assert not (tmp_path / 'escape.txt').exists()
    assert any('parent_ref' in s for s in out['skipped'])


def test_import_rejects_absolute_path(tmp_path):
    buf = tmp_path / 'abs.zip'
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('/etc/passwd', 'x')
        zf.writestr('abs/faces/pack.json', '{}')
    out = pack_zip.import_pack(buf, root=tmp_path / 'dst')
    assert out['ok']
    assert any('absolute_path' in s for s in out['skipped'])


def test_export_missing_slug_is_graceful(tmp_path):
    data, why = pack_zip.export_pack_bytes('nobody_here', root=tmp_path / 'empty')
    assert data == b''
    assert '没有形象包目录' in why


def test_summary_reads_slots(tmp_path):
    src = _make_pack(tmp_path, 'demo')
    pack = src / 'demo' / 'faces' / 'pack.json'
    pack.write_text(json.dumps({'name': '演示', 'slots': {'a': {}, 'b': {}}}), encoding='utf-8')
    info = pack_zip.pack_summary('demo', root=src)
    assert info['exists'] and info['name'] == '演示'
    assert info['slots'] == ['a', 'b']
    assert info['images'] == ['01.png']


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
