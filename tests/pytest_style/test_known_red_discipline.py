"""known_red 台账的巡检锁。

台账的用处是「把这些红记下来，别装作不知道」。它一旦自己走形，用处就反过来：

- 少一条 —— 那份红没人管了，下次报红会被当成新债重新分析一遍；
- 指向不存在的文件 —— 这行永远没人跑，也没人会发现；
- 指向脚本车道根本跑不到的文件 —— 它在台账里，却既等不到 KNOWN-RED 也等不到 FIXED，
  等于把一份红藏在「已登记」四个字后面；
- 同一条写两次 —— loader 建 dict 时静默覆盖，前一条的 root_cause 悄悄丢了。

所以这里**不数数量**（数量只该减，钉死会逼人删证据），只钉七件形状上的事。
判据是 `violations()` 这个纯函数：真台账喂一遍，假台账也喂一遍（每条规则都要有
一份「故意写坏」的证据，否则规则写成恒真也没人知道）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
LEDGER = ROOT / "tests" / "known_red.yaml"
RUNNER = ROOT / "scripts" / "run_all_tests.py"

#: 只有这四种「为什么红」值得登记。新增一种要先想清楚它能不能被机器判，
#: 判不了就得写进 reason 里，不能靠加一类来含糊过去。
KINDS = ("drift", "doc_deleted", "sys_path", "env")
REQUIRED = ("file", "owner", "reason", "root_cause", "since")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLACEHOLDER_OWNERS = {"", "-", "?", "todo", "tbd", "unknown", "待定", "nobody"}
_MIN_REASON = 10


def _runner():
    """按路径加载门禁的 runner，借用它自己的入口判据（不另立一套标准）。"""
    spec = importlib.util.spec_from_file_location("_known_red_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw() -> list:
    return yaml.safe_load(LEDGER.read_text(encoding="utf-8"))["entries"]


def violations(entries, tests_dir: pathlib.Path, has_entry) -> list:
    """台账形状的问题清单（空 = 没问题）。纯函数：喂假数据就能验证规则会不会响。"""
    problems: list = []
    seen: dict = {}
    for index, entry in enumerate(entries):
        who = entry.get("file") or "#%d" % index
        seen[who] = seen.get(who, 0) + 1
        missing = [key for key in REQUIRED if key not in entry]
        if missing:
            problems.append("%s: 缺字段 %s" % (who, missing))
            continue
        if entry["root_cause"] not in KINDS:
            problems.append("%s: root_cause=%r 不在 %s" % (who, entry["root_cause"], list(KINDS)))
        if str(entry["owner"]).strip().lower() in _PLACEHOLDER_OWNERS:
            problems.append("%s: owner 是占位值 %r" % (who, entry["owner"]))
        if len(str(entry["reason"]).strip()) < _MIN_REASON:
            problems.append("%s: reason 太短（%r），等于没写" % (who, entry["reason"]))
        if not _DATE.match(str(entry["since"])):
            problems.append("%s: since=%r 不是 YYYY-MM-DD" % (who, entry["since"]))
        path = tests_dir / who
        if not path.exists():
            problems.append("%s: tests/ 下没有这个文件" % who)
        elif not has_entry(path):
            problems.append(
                "%s: 脚本车道认不出入口（没有 __main__ / run_all(...)），这行永远等不到红也等不到绿" % who
            )
    for name, count in seen.items():
        if count > 1:
            problems.append("%s: 同一份文件登记了 %d 次（loader 建 dict 会静默覆盖）" % (name, count))
    return problems


# ---------------------------------------------------------------- 真台账


def test_real_ledger_has_no_shape_problems():
    entries = _raw()
    assert entries, "台账是空的：要么真的全绿（那就把文件一起删掉），要么整理时丢了"
    assert violations(entries, ROOT / "tests", _runner()._has_entry_point) == []


def test_loader_sees_exactly_the_entries_in_the_file():
    entries = _raw()
    loaded = _runner().load_known_red()
    assert len(loaded) == len(entries), "条目数与 loader 读到的不一致"
    assert set(loaded) == {entry["file"] for entry in entries}


# ---------------------------------------------------------- 规则自己的反证


def _entry(**over):
    base = {
        "file": "test_demo.py",
        "owner": "lead",
        "reason": "模块已被删除，用例还留着",
        "root_cause": "doc_deleted",
        "since": "2026-10-29",
    }
    base.update(over)
    return base


def _fake(tmp_path, name="test_demo.py", entry_point=True):
    path = tmp_path / name
    path.write_text(
        "if __name__ == '__main__':\n    raise SystemExit(0)\n" if entry_point else "x = 1\n",
        encoding="utf-8",
    )
    return path


def test_clean_entry_passes(tmp_path):
    _fake(tmp_path)
    assert violations([_entry()], tmp_path, lambda p: True) == []


def test_unknown_kind_is_caught(tmp_path):
    _fake(tmp_path)
    problems = violations([_entry(root_cause="broken")], tmp_path, lambda p: True)
    assert any("root_cause" in item for item in problems), problems


def test_missing_field_is_caught(tmp_path):
    _fake(tmp_path)
    entry = _entry()
    entry.pop("root_cause")
    problems = violations([entry], tmp_path, lambda p: True)
    assert any("缺字段" in item for item in problems), problems


def test_vague_reason_and_placeholder_owner_are_caught(tmp_path):
    _fake(tmp_path)
    problems = violations([_entry(reason="红", owner="TBD")], tmp_path, lambda p: True)
    assert any("owner" in item for item in problems), problems
    assert any("reason" in item for item in problems), problems


def test_missing_file_is_caught(tmp_path):
    problems = violations([_entry(file="test_gone.py")], tmp_path, lambda p: True)
    assert any("没有这个文件" in item for item in problems), problems


def test_unreachable_entry_is_caught(tmp_path):
    _fake(tmp_path)
    problems = violations([_entry()], tmp_path, lambda p: False)
    assert any("认不出入口" in item for item in problems), problems


def test_duplicate_entry_is_caught(tmp_path):
    _fake(tmp_path)
    problems = violations([_entry(), _entry()], tmp_path, lambda p: True)
    assert any("登记了 2 次" in item for item in problems), problems
