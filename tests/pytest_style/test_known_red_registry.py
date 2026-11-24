"""``tests/known_red.yaml`` 自己得先能读、读出来得像个表（DSH-019 副产物）。

起因：2026-10-26 那版台账里有两个条目丢了 ``- file: "…"`` 行，
``yaml.safe_load`` 直接报 ``ParserError``，整个 ``scripts/run_all_tests.py`` 起不来——
而因为没人跑全量，这个坏法活了很久。所以把“读得出来”本身变成一条测试。
es"""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
TESTS = ROOT / "tests"
REGISTRY = TESTS / "known_red.yaml"

#: 合法的 ``root_cause`` 分类（见文件头部注释）。
ROOT_CAUSES = {"sys_path", "doc_deleted", "drift", "env", "pytest_style"}
REQUIRED = ("file", "root_cause", "reason", "owner", "since")


def _entries():
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    assert isinstance(data, dict), "known_red.yaml 顶层应是 mapping"
    return data.get("entries") or []


def test_registry_parses():
    entries = _entries()
    assert isinstance(entries, list), "entries 应是序列（每条以 - file: 开头）"


def test_every_entry_is_complete():
    for entry in _entries():
        missing = [k for k in REQUIRED if not str(entry.get(k, "")).strip()]
        assert not missing, "条目缺字段 %s：%r" % (missing, entry)
        assert entry["root_cause"] in ROOT_CAUSES, "root_cause 写错了：%r" % (entry["root_cause"],)


def test_every_entry_points_at_a_real_file():
    ghost = [e["file"] for e in _entries() if not (TESTS / e["file"]).exists()]
    assert not ghost, "应用已删的测试不该留在台账里（请出表）：%s" % ghost
