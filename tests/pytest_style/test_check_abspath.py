"""配置绝对路径门禁：写死的盘符路径必须存在（防改名后静默指空）。"""
from scripts import check as c


def test_missing_absolute_path_is_flagged(tmp_path):
    env = tmp_path / ".env"
    env.write_text(r"X=C:\Users\Elaina\definitely_missing_dir\file.db", encoding="utf-8")
    got = c.check_abspath([str(env)])
    assert got and "definitely_missing_dir" in got[0]


def test_existing_absolute_path_is_ok(tmp_path):
    target = tmp_path / "exists.db"
    target.write_text("x", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("X=" + str(target), encoding="utf-8")
    assert c.check_abspath([str(env)]) == []


def test_wildcards_are_ignored(tmp_path):
    env = tmp_path / ".env"
    env.write_text(r"X=C:\Users\Elaina\*.db", encoding="utf-8")
    assert c.check_abspath([str(env)]) == []
