# 资产分区（2026-10-26）：把「能不能开源」写进路径，而不是靠记性。
#
#   <asset_root>/private/<slug>/<kind>/   第三方 / 有版权 / 作者禁止再分发
#   <asset_root>/<slug>/<kind>/           可随包分发（自己画的、CC0、明确授权）
#   <model_root>/<engine>/                大件权重，跟具体角色无关
#
# 唯一解析入口：其他模块不再自己拼 "data/persona/assets"。
# 没写 license 块的包一律当作「不可再分发」（fail closed，默认安全）。
from __future__ import annotations

import json
import os
from pathlib import Path

ASSET_ROOT_ENV = 'YINYUE_ASSET_ROOT'
MODEL_ROOT_ENV = 'YINYUE_MODEL_ROOT'
DEFAULT_ASSET_ROOT = Path('data/persona/assets')
DEFAULT_MODEL_ROOT = Path('data/models')
PRIVATE_ZONE = 'private'
PUBLIC_ZONE = 'public'
STUB_NAME = 'REQUIRES_USER_SUPPLY.json'
_MODEL_ENGINES = ('gpt_sovits', 'rvc', 'cosyvoice3', 'silk', 'piper')


def asset_root() -> Path:
    raw = (os.environ.get(ASSET_ROOT_ENV) or '').strip()
    return Path(raw) if raw else DEFAULT_ASSET_ROOT


def model_root() -> Path:
    raw = (os.environ.get(MODEL_ROOT_ENV) or '').strip()
    return Path(raw) if raw else DEFAULT_MODEL_ROOT


def model_dir(engine: str) -> Path:
    return model_root() / str(engine or '').strip()


def known_engines() -> tuple:
    return _MODEL_ENGINES


def private_dir(slug: str = '', kind: str = '') -> Path:
    base = asset_root() / PRIVATE_ZONE / str(slug or '').strip()
    return base / str(kind).strip() if kind else base


def public_dir(slug: str = '', kind: str = '') -> Path:
    base = asset_root() / str(slug or '').strip()
    return base / str(kind).strip() if kind else base


def pack_dir(slug: str = '', *, root: 'str | Path | None' = None) -> Path:
    slug = str(slug or '').strip()
    if root is not None:
        return Path(root) / slug
    if slug and private_dir(slug).is_dir():
        return private_dir(slug)
    return public_dir(slug)


def kind_dir(slug: str = '', kind: str = '', *, root: 'str | Path | None' = None):
    d = pack_dir(slug, root=root) / str(kind or '').strip()
    return d if d.is_dir() else None


def kind_path(slug: str = '', kind: str = '', *, root: 'str | Path | None' = None) -> Path:
    return pack_dir(slug, root=root) / str(kind or '').strip()


def zone_of(slug: str = '', *, root: 'str | Path | None' = None) -> str:
    slug = str(slug or '').strip()
    if root is not None:
        return 'custom'
    return PRIVATE_ZONE if slug and private_dir(slug).is_dir() else PUBLIC_ZONE


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def pack_json(slug: str = '', kind: str = 'faces', *, root: 'str | Path | None' = None) -> dict:
    d = kind_dir(slug, kind, root=root)
    return _read_json(d / 'pack.json') if d else {}


def license_of(slug: str = '', kind: str = 'faces', *, root: 'str | Path | None' = None) -> dict:
    return dict((pack_json(slug, kind, root=root).get('license') or {}))


def redistributable(slug: str = '', kind: str = 'faces', *, root: 'str | Path | None' = None) -> bool:
    lic = license_of(slug, kind, root=root)
    return bool(lic.get('redistribute')) is True


def export_blockers(slug: str = '', *, kinds=('faces', 'voice'), root: 'str | Path | None' = None) -> list:
    out = []
    for kind in tuple(kinds or ()):
        if kind_dir(slug, kind, root=root) is None:
            continue
        lic = license_of(slug, kind, root=root)
        if lic.get('redistribute') is True:
            continue
        reason = 'no_license_block' if not lic else 'license_forbids'
        out.append({'kind': str(kind), 'zone': zone_of(slug, root=root), 'reason': reason,
                    'source': str(lic.get('source') or ''), 'author': str(lic.get('author') or ''),
                    'note': str(lic.get('note') or '')})
    return out


def stub_payload(slug: str = '', kind: str = '', *, root: 'str | Path | None' = None) -> dict:
    lic = license_of(slug, kind, root=root)
    return {'slug': str(slug), 'kind': str(kind), 'required': True, 'user_supplied': True,
            'why': '这个子包不可随角色包再分发；请自行准备后放到 %s/<slug>/%s/' % (asset_root().as_posix(), kind),
            'license': lic}


def stub_json(slug: str = '', kind: str = '', *, root: 'str | Path | None' = None) -> str:
    return json.dumps(stub_payload(slug, kind, root=root), ensure_ascii=False, indent=2)


def provenance(model_key: str) -> dict:
    return _read_json(model_dir(model_key) / 'provenance.json')
