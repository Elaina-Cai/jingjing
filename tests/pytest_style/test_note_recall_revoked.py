"""需要 pytest 夹具的那条 note_recall 用例，搬到 pytest 侧跑。

legacy 的 tests/test_note_recall.py 是 main() 逐个直调，没有 tmp_path/monkeypatch；
混在 TESTS 列表里会让整份文件红。这里按文件路径加载同一条用例，喂进夹具。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_LEGACY = ROOT / "tests" / "test_note_recall.py"


def _legacy():
    spec = importlib.util.spec_from_file_location("_note_recall_legacy", _LEGACY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revoked_note_source_is_not_recalled(tmp_path, monkeypatch):
    _legacy().test_revoked_note_source_is_not_recalled(tmp_path, monkeypatch)
