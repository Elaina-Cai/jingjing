"""安装向导（scripts/setup_wizard.py）回归测试。

只用标准库；临时目录放项目内 ``.tmp_test``（本沙箱的 tempfile 目录不可写）。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import setup_wizard as wiz  # noqa: E402

TMP = ROOT / ".tmp_test" / f"wizard_{os.getpid()}"


def _fresh(name: str) -> Path:
    d = TMP / name
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_python_check() -> None:
    ok, version = wiz.check_python()
    assert ok, version
    assert version.count(".") == 2


def test_set_env_values_updates_and_appends() -> None:
    d = _fresh("env")
    p = d / ".env"
    p.write_text("# 注释\nA=1\nB=old\nB=old2\nYINYUE_FEATURE_X=0\n", encoding="utf-8")
    actions = wiz.set_env_values(p, {"A": "2", "B": "new", "C": "3"})
    text = p.read_text(encoding="utf-8")
    assert actions == {"A": "updated", "B": "updated x2", "C": "added"}, actions
    assert "A=2" in text and "A=1" not in text
    assert text.count("B=new") == 2 and "old" not in text
    assert text.endswith("C=3\n")
    assert "YINYUE_FEATURE_X=0" in text and "# 注释" in text


def test_set_env_values_keeps_commented_keys() -> None:
    d = _fresh("env_comment")
    p = d / ".env"
    p.write_text("# A=9\nA=1\n", encoding="utf-8")
    wiz.set_env_values(p, {"A": "2"})
    text = p.read_text(encoding="utf-8")
    assert "# A=9" in text and "A=2" in text and "A=1" not in text


def test_ensure_env_file() -> None:
    d = _fresh("ensure")
    (d / ".env.example").write_text("X=1\n", encoding="utf-8")
    path, how = wiz.ensure_env_file(d)
    assert how == "created_from_example"
    assert path.read_text(encoding="utf-8") == "X=1\n"
    path2, how2 = wiz.ensure_env_file(d)
    assert how2 == "exists" and path2 == path


def test_fix_autostart_bat() -> None:
    d = _fresh("bat")
    p = d / "autostart.bat"
    p.write_text('@echo off\ncd /d "C:\\old\\yinyue"\n', encoding="utf-8")
    status = wiz.fix_autostart_bat(p)
    assert status.startswith("fixed"), status
    text = p.read_text(encoding="utf-8")
    assert 'cd /d "%~dp0"' in text and "C:\\old" not in text
    assert wiz.fix_autostart_bat(p) == "ok"  # 幂等
    p2 = d / "nocd.bat"
    p2.write_text("@echo off\n", encoding="utf-8")
    assert wiz.fix_autostart_bat(p2) == "no_cd_line"


def test_duplicate_keys() -> None:
    d = _fresh("dupe")
    p = d / ".env"
    p.write_text("A=1\nA=2\n# A=3\nB=1\n", encoding="utf-8")
    assert wiz.duplicate_keys(p) == {"A": 2}


def test_collect_env_updates_non_interactive() -> None:
    env = {"PROVIDER": "deepseek", "WECHAT_SEND_ENABLED": "", "TEMPORAL_TIMEZONE": ""}
    updates = wiz.collect_env_updates(env, interactive=False)
    assert updates == {"WECHAT_SEND_ENABLED": "true", "TEMPORAL_TIMEZONE": "Asia/Shanghai"}
    filled = {
        "PROVIDER": "deepseek",
        "DEEPSEEK_API_KEY": "sk-x",
        "WECHAT_SEND_ENABLED": "true",
        "TEMPORAL_TIMEZONE": "Asia/Shanghai",
    }
    assert wiz.collect_env_updates(filled, interactive=False) == {}


def test_tables_match_doctor() -> None:
    import doctor  # noqa: E402 - 同一 scripts 目录

    assert wiz.REQUIRED_MODULES == doctor.REQUIRED_MODULES
    assert wiz.PROVIDER_KEY_FIELDS == doctor.PROVIDER_KEY_FIELDS


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    try:
        for test in tests:
            try:
                test()
                print(f"  ok   {test.__name__}")
            except AssertionError as exc:
                failed += 1
                print(f"  FAIL {test.__name__}: {exc!r}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"setup wizard checks: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
