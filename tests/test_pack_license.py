# 许可闸门：不可再分发的子包不许进角色包（2026-10-26）
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from core.assets import paths
from core.identity import pack_zip


def _make_pack(root: Path, slug: str, *, kinds=("faces",), license_block=None, private=False) -> Path:
    base = (root / paths.PRIVATE_ZONE / slug) if private else (root / slug)
    for kind in kinds:
        d = base / kind
        d.mkdir(parents=True, exist_ok=True)
        payload = {"slug": slug, "kind": kind}
        if license_block is not None:
            payload["license"] = license_block
        (d / "pack.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        (d / "gallery").mkdir(exist_ok=True)
        (d / "gallery" / "a.png").write_bytes(b"not-a-real-png")
    return base


def _names(data: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.namelist()


def test_non_redistributable_is_refused(tmp_path):
    _make_pack(tmp_path, "alpha", license_block={"redistribute": False})
    data, why = pack_zip.export_pack_bytes("alpha", root=tmp_path)
    assert data == b""
    assert "不可导出" in why


def test_missing_license_block_is_refused(tmp_path):
    _make_pack(tmp_path, "beta")
    data, why = pack_zip.export_pack_bytes("beta", root=tmp_path)
    assert data == b""
    assert "不可导出" in why


def test_stub_keeps_no_payload(tmp_path):
    _make_pack(tmp_path, "gamma", kinds=("faces", "voice"), license_block={"redistribute": False})
    data, why = pack_zip.export_pack_bytes("gamma", root=tmp_path, stub=True)
    assert data, why
    names = _names(data)
    assert "gamma/faces/REQUIRES_USER_SUPPLY.json" in names
    assert not any(n.endswith("a.png") for n in names)


def test_redistributable_exports_payload(tmp_path):
    _make_pack(tmp_path, "delta", license_block={"redistribute": True})
    data, why = pack_zip.export_pack_bytes("delta", root=tmp_path)
    assert data, why
    names = _names(data)
    assert "delta/faces/gallery/a.png" in names


def test_private_zone_wins_over_public(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ASSET_ROOT_ENV, str(tmp_path))
    _make_pack(tmp_path, "eps", private=False)
    _make_pack(tmp_path, "eps", private=True)
    assert paths.zone_of("eps") == paths.PRIVATE_ZONE
    assert paths.kind_dir("eps", "faces") == tmp_path / paths.PRIVATE_ZONE / "eps" / "faces"


def test_real_jingjing_pack_is_not_redistributable():
    if paths.kind_dir("jingjing", "faces") is None:
        return
    assert paths.redistributable("jingjing", "faces") is False
    data, why = pack_zip.export_pack_bytes("jingjing")
    assert data == b""
    assert "不可导出" in why


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
