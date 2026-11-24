"""改文件的事务壳（2026-10-28，PIT-051）。

要守的三件事：
1. 预检不过 → 一个字都不写（唯一会失败的那步在最前面）。
2. 中途写失败 → 全部回滚（不留半成品）。
3. 锚点必须在**演进中的内容**上唯一（这是按行号改代码的反面）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.infra.migrate import Migration  # noqa: E402


def _mkroot(tmp_path):
    (tmp_path / "a.py").write_text("X = 1\nY = 2\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("Z = 3\n", encoding="utf-8")
    return tmp_path


def test_missing_anchor_writes_nothing(tmp_path):
    root = _mkroot(tmp_path)
    m = Migration(root).replace("a.py", "NOT_THERE", "Q = 9")
    res = m.apply()
    assert res["ok"] is False and res["errors"]
    assert (root / "a.py").read_text(encoding="utf-8") == "X = 1\nY = 2\n"
    assert not (root / "backups").exists()


def test_ambiguous_anchor_writes_nothing(tmp_path):
    root = _mkroot(tmp_path)
    (root / "a.py").write_text("Q = 0\nQ = 0\n", encoding="utf-8")
    res = Migration(root).replace("a.py", "Q = 0", "Q = 1").apply()
    assert res["ok"] is False
    assert "2 次" in res["errors"][0]
    assert (root / "a.py").read_text(encoding="utf-8") == "Q = 0\nQ = 0\n"


def test_second_op_sees_first_edit(tmp_path):
    """两条改动落在同一文件：第二条的锚点要在第一条之后的内容里找。"""
    root = _mkroot(tmp_path)
    res = (Migration(root)
           .replace("a.py", "X = 1", "X = 10\nM = 5")
           .replace("a.py", "M = 5", "M = 6")
           .apply())
    assert res["ok"] is True, res
    assert (root / "a.py").read_text(encoding="utf-8") == "X = 10\nM = 6\nY = 2\n"


def test_dry_run_changes_nothing(tmp_path):
    root = _mkroot(tmp_path)
    res = Migration(root).replace("a.py", "X = 1", "X = 7").apply(dry_run=True)
    assert res["ok"] is True and res["written"] == []
    assert (root / "a.py").read_text(encoding="utf-8") == "X = 1\nY = 2\n"


def test_backup_keeps_original(tmp_path):
    root = _mkroot(tmp_path)
    res = Migration(root).replace("a.py", "X = 1", "X = 7").apply()
    assert res["ok"] is True
    bdir = res["backup_dir"]
    assert os.path.isdir(bdir)
    assert "X = 1" in open(os.path.join(bdir, "a.py"), encoding="utf-8").read()


def test_failed_write_rolls_everything_back(tmp_path, monkeypatch):
    root = _mkroot(tmp_path)
    m = Migration(root).replace("a.py", "X = 1", "X = 7").replace("b.py", "Z = 3", "Z = 4")
    real_write = type(root).write_text
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        if self.name == "b.py":
            raise OSError("disk full")
        return real_write(self, *a, **kw)

    monkeypatch.setattr(type(root), "write_text", flaky)
    res = m.apply()
    monkeypatch.setattr(type(root), "write_text", real_write)
    assert res["ok"] is False and res["rolled_back"] is True
    assert (root / "a.py").read_text(encoding="utf-8") == "X = 1\nY = 2\n"
