"""Windows ACL 收紧（2026-10-28，CB-138）。

要守的两件事：
1. `chmod 0600` 在 Windows 上等于什么都没做——本模块必须走 icacls。
2. 目录用 `(OI)(CI)`、文件不用，且永远不带 `/T`（surfcore 两份实现带过同一个 bug）。
"""
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.infra import acl  # noqa: E402


def _acl_src() -> str:
    return open(os.path.join(ROOT, "core", "infra", "acl.py"), encoding="utf-8").read()


def _grants(target: Path) -> list[str]:
    return acl.grants_for(target, "someone")


def test_dir_grant_uses_inheritance_flags(tmp_path):
    g = _grants(tmp_path)
    assert "/inheritance:r" in g
    assert "someone:(OI)(CI)F" in g


def test_file_grant_has_no_inheritance_flags(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("x", encoding="utf-8")
    g = _grants(f)
    assert "someone:F" in g
    assert not any("(OI)(CI)" in x for x in g)


def test_never_uses_recursive_flag(tmp_path):
    f = tmp_path / "y.txt"
    f.write_text("y", encoding="utf-8")
    for target in (tmp_path, f):
        assert "/T" not in _grants(target)


def test_no_user_is_refused(tmp_path):
    try:
        acl.grants_for(tmp_path, "")
    except ValueError:
        return
    raise AssertionError("空用户名必须报错，不许静默生成 icacls 命令")


def test_missing_path_reports_failure_not_success(tmp_path):
    res = acl.tighten(str(tmp_path / "does-not-exist"))
    assert res["ok"] is False
    assert res["reason"] == "路径不存在"


def test_dry_run_lists_dir_and_files_without_executing(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b", encoding="utf-8")
    res = acl.tighten(str(tmp_path), dry_run=True)
    assert res["ok"] is True
    assert res["applied"] == 0
    assert len(res["commands"]) == 4  # 目录 + a.txt + sub + b.txt
    assert all(c[0] == "icacls" for c in res["commands"])


def test_module_does_not_rely_on_chmod():
    src = open(os.path.join(ROOT, "core", "infra", "acl.py"), encoding="utf-8").read()
    assert "os.chmod" not in src

def test_report_says_unknown_not_ok_when_it_cannot_read(tmp_path):
    """读不到 ACL 的时候不许报 ok —— 「不知道」和「没人能读」是两件事。"""
    res = acl.report([str(tmp_path / "does-not-exist")])
    assert res["level"] == "unknown"
    assert res["ok"] is False


def test_report_flags_inherited_entries(tmp_path):
    d = tmp_path / "cards"
    d.mkdir()
    (d / "a.json").write_text("{}", encoding="utf-8")
    res = acl.report([str(d)])
    # 新建目录默认继承上层 ACE：必须被看见（warn 或 fail），不许沉默
    assert res["level"] in ("warn", "fail"), res
    assert res["rows"][str(d)] >= 1
    assert res["inherited"] or res["strangers"]


def test_tighten_failure_is_logged_not_swallowed():
    src = _acl_src()
    assert 'warn("acl_tighten_failed"' in src, "收紧失败必须落一条事件，否则没人知道「没做成」"


def test_tightened_target_list_lives_in_one_place():
    """「该收紧哪些目录」只有一份声明：CLI 与 checkup 都问它，不许各写一遍。

    注意锁的是**清单常量**，不是路径字符串本身——`console/executor.py` 会往卡目录写文件、
    `core/identity/pack_zip.py` 会把它打包，它们合法地提到这个路径，不该被这条锁管。
    """
    src = _acl_src()
    assert 'TIGHTENED = ("data/persona/cards",)' in src
    assert "or (list(TIGHTENED)" in src, "CLI 的默认目标必须来自同一份清单"
    checkup = open(os.path.join(ROOT, "console", "checkup.py"), encoding="utf-8").read()
    assert "_acl.report()" in checkup, "巡检必须问 acl，不许自带一份目录清单"
    hits = set()
    root = Path(ROOT)
    for sub in ("core", "console", "loop", "scripts", "scheduler"):
        for f in (root / sub).rglob("*.py"):
            if "TIGHTENED" in f.read_text(encoding="utf-8-sig", errors="replace"):
                hits.add(f.relative_to(root).as_posix())
    assert hits == {"core/infra/acl.py"}, f"清单被别处引用：{sorted(hits)}"
