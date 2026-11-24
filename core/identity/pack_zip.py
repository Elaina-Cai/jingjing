"""角色包打包 / 导入（开源分发单元）。

一个角色包就是一个 zip：:

    <slug>/faces/{pack.json, canon.txt, index.json, gallery/*.jpg}
    <slug>/voice/{pack.json, canon.txt, ref/*.wav}
    <slug>/card.json          （可选：卡片副本，导入时默认不覆盖现有卡片）
    README.md                 （来源声明与授权，导入时随包保留）

2026-10-26 P5：目的是让**别人**也能带自己的角色进来，所以这里一条硬规则——
不认识的角色名、可疑路径（绝对路径、.. 、符号链接）一律拒绝，宁可不导入，
也不要把别人的文件写到这台机器的别处去。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

from safelog import warn

from core.assets import paths as _asset_paths  # 2026-10-26 资产分区

ASSET_ROOT = _asset_paths.asset_root()
MAX_BYTES = 200 * 1024 * 1024      # 200 MB：一个形象包再大就不正常了
MAX_FILES = 2000
_SLUG = re.compile(r'[^0-9A-Za-z_\-]')


def _assets(root: str | Path | None = None) -> Path:
    return Path(root) if root else ASSET_ROOT


def _safe_slug(slug: str) -> str:
    return _SLUG.sub('', str(slug or ''))


def _iter_pack_files(slug: str, *, include_voice: bool, root: str | Path | None = None,
                     skip: tuple = ()):
    base = _asset_paths.pack_dir(slug, root=root)
    faces = base / 'faces'
    if 'faces' in skip or not faces.is_dir():
        return
    for item in sorted(faces.rglob('*')):
        if item.is_file():
            yield item, 'faces/' + item.relative_to(faces).as_posix()
    if include_voice and 'voice' not in skip:
        voice = base / 'voice'
        if voice.is_dir():
            for item in sorted(voice.rglob('*')):
                if item.is_file():
                    yield item, 'voice/' + item.relative_to(voice).as_posix()
    card = Path('data/persona/cards') / (slug + '.json')
    if card.is_file():
        yield card, 'card.json'


def export_pack_bytes(
    slug: str,
    *,
    include_voice: bool = True,
    root: str | Path | None = None,
    stub: bool = False,
) -> tuple[bytes, str]:
    """导出为 zip 字节流。返回 (bytes, 文件名)；失败时 bytes 为空、第二项是原因。"""
    clean = _safe_slug(slug)
    if not clean:
        return b'', 'slug 不合法'
    kinds = tuple(k for k in (('faces', 'voice') if include_voice else ('faces',)))
    blockers = _asset_paths.export_blockers(clean, kinds=kinds, root=root)
    if blockers and not stub:
        return b'', '不可导出（许可禁止再分发）：' + '；'.join(
            '%s(%s)' % (b.get('kind'), b.get('reason')) for b in blockers)
    skip = tuple(b.get('kind') for b in blockers) if stub else ()
    items = list(_iter_pack_files(clean, include_voice=include_voice, root=root, skip=skip))
    if not items and not (blockers and stub):
        # stub 模式允许“只有占位、没有实体条目”（被许可拦下的子包整段 skip 掉了）
        return b'', '这个 slug 没有形象包目录：%s' % (_assets(root) / clean / 'faces')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path, arc in items:
            try:
                zf.write(path, arcname='%s/%s' % (clean, arc))
            except Exception as exc:  # noqa: BLE001
                warn('core/identity/pack_zip.py export item failed',
                     file=str(path), err=repr(exc)[:200])
        for blocked in (blockers if stub else ()):
            zf.writestr('%s/%s/%s' % (clean, blocked.get('kind'), _asset_paths.STUB_NAME),
                        _asset_paths.stub_json(clean, blocked.get('kind'), root=root))
        zf.writestr('%s/README.md' % clean,
                    '角色包：%s\n\n包含 faces/（形象）与 voice/（音色）；'
                    'card.json 是卡片副本。\n导入：Web 后台“角色包导入”，'
                    '或 scripts 里的导入脚本。\n' % clean)
    data = buf.getvalue()
    if not data:
        return b'', '打包失败（没有可写内容）'
    return data, '%s-pack.zip' % clean


def export_pack(
    slug: str,
    out_path: str | Path,
    *,
    include_voice: bool = True,
    root: str | Path | None = None,
    stub: bool = False,
) -> Path | None:
    data, name = export_pack_bytes(slug, include_voice=include_voice, root=root, stub=stub)
    if not data:
        warn('core/identity/pack_zip.py export failed', why=name)
        return None
    target = Path(out_path)
    if target.suffix.lower() != '.zip':
        target = target.with_suffix('.zip')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _bad_name(name: str) -> str:
    """zip 里的名字有没有问题（问题就返回原因，空串表示安全）。"""
    pure = name.replace('\\', '/')
    if pure.startswith('/') or re.match(r'^[A-Za-z]:', pure):
        return 'absolute_path'
    if '..' in pure.split('/'):
        return 'parent_ref'
    return ''


def import_pack(
    source: str | Path | bytes,
    *,
    slug: str = '',
    overwrite: bool = False,
    install_card: bool = False,
    root: str | Path | None = None,
    cards_dir: str | Path | None = None,
) -> dict:
    """导入一个角色包。返回 {ok, slug, written, skipped, why}，永不抛。"""
    try:
        if isinstance(source, (bytes, bytearray)):
            handle = io.BytesIO(source)
        else:
            handle = open(source, 'rb')
        with zipfile.ZipFile(handle) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if not infos:
                return {'ok': False, 'why': 'zip 里没有文件'}
            if len(infos) > MAX_FILES:
                return {'ok': False, 'why': '文件数超过上限 %d' % MAX_FILES}
            total = sum(max(0, i.file_size) for i in infos)
            if total > MAX_BYTES:
                return {'ok': False, 'why': '解压后超过 %d MB' % (MAX_BYTES // 1024 // 1024)}
            tops = {i.filename.replace('\\', '/').split('/')[0] for i in infos}
            # 绝对路径条目的第一段会是空串，不能拿来当角色名
            candidates = sorted(x for x in tops if x)
            clean = _safe_slug(slug) or _safe_slug(candidates[0] if candidates else '')
            if not clean:
                return {'ok': False, 'why': '读不出角色名，请用 slug= 指定'}
            base = _assets(root) / clean
            if base.exists() and not overwrite:
                return {'ok': False, 'why': '已存在 %s ；要覆盖请明确允许覆盖' % base}
            written: list[str] = []
            skipped: list[str] = []
            for info in infos:
                name = info.filename.replace('\\', '/')
                bad = _bad_name(name)
                if bad:
                    skipped.append(name + ' (' + bad + ')')
                    continue
                parts = name.split('/')
                if len(parts) < 2:
                    skipped.append(name + ' (no_dir)')
                    continue
                rel = '/'.join(parts[1:])            # 去掉 zip 顶层目录
                if rel.startswith('card.json'):
                    if not install_card:
                        skipped.append(name + ' (card_not_installed)')
                        continue
                    target = Path(cards_dir or 'data/persona/cards') / (clean + '.json')
                elif rel.startswith('faces/') or rel.startswith('voice/'):
                    target = base / rel
                elif rel.lower().endswith('readme.md'):
                    target = base / 'README.md'
                else:
                    skipped.append(name + ' (unknown_dir)')
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, 'wb') as dst:
                    dst.write(src.read())
                written.append(str(target))
            if not written:
                return {'ok': False, 'why': '没有可导入的文件', 'skipped': skipped}
            return {'ok': True, 'slug': clean, 'written': written, 'skipped': skipped}
    except Exception as exc:  # noqa: BLE001
        warn('core/identity/pack_zip.py import failed', err=repr(exc)[:300])
        return {'ok': False, 'why': '导入失败：' + repr(exc)[:200]}
    finally:
        try:
            handle.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def pack_summary(slug: str, *, root: str | Path | None = None) -> dict:
    """给 Web 用的包摘要（有哪些图、有没有音色）。"""
    clean = _safe_slug(slug)
    base = _assets(root) / clean
    faces = base / 'faces'
    voice = base / 'voice'
    images = []
    try:
        if faces.is_dir():
            for item in sorted((faces / 'gallery').glob('*')):
                if item.is_file():
                    images.append(item.name)
    except Exception:  # noqa: BLE001
        pass
    pack = {}
    try:
        path = faces / 'pack.json'
        if path.is_file():
            pack = json.loads(path.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        pack = {}
    return {
        'slug': clean,
        'exists': faces.is_dir(),
        'name': str(pack.get('name') or ''),
        'images': images,
        'slots': sorted((pack.get('slots') or {}).keys()),
        'voice': voice.is_dir(),
    }
