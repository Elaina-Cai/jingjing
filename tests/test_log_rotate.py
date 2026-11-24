r"""日志轮转（core.infra.log_rotate）的钉子。

跑法::

    & .\.venv_it\Scripts\python.exe -X utf8 -u tests\test_log_rotate.py

覆盖：上限判定 / rename 链位移 / 副本数上界 / 句柄占用时退 copy+truncate /
env 覆盖与关闭 / 日期后缀历史文件不被碰 / 行完整性 / CLI 的 --dry-run 与 --report。
"""

from __future__ import annotations

import io as _io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.getcwd())
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：不给 dir= 时系统 TEMP 不可写会静默回落到仓库根。
from _tempdirs import temp_base  # noqa: E402

import core.infra.log_rotate as R  # noqa: E402  - 需要先把仓库根放进 sys.path

_PASS = 0
_FAIL: list[str] = []


def ok(name: str, cond: bool, detail: object = "") -> None:
    global _PASS
    if cond:
        _PASS += 1
    else:
        _FAIL.append(f"{name} -> {detail!r}")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def test_noop_under_limit() -> None:
    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        path = Path(tmp) / "launcher_service.log"
        _write(path, "hello\n")
        action = R.rotate_if_needed(path, R.Policy(max_bytes=1024, backups=2))
        ok("未到上限 → noop", action == "noop", action)
        ok("未到上限不动内容", path.read_text(encoding="utf-8") == "hello\n")
        ok("未到上限不产生备份", not (Path(tmp) / "launcher_service.log.1").exists())


def test_rename_chain() -> None:
    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        path = Path(tmp) / "faulthandler.log"
        policy = R.Policy(max_bytes=10, backups=2)
        _write(path, "A" * 20)
        ok("第一次 → rename", R.rotate_if_needed(path, policy) == "rename")
        ok(".1 拿到旧内容", (Path(tmp) / "faulthandler.log.1").read_text() == "A" * 20)
        ok("当前文件已不在", not path.exists())
        _write(path, "B" * 20)
        ok("第二次 → rename", R.rotate_if_needed(path, policy) == "rename")
        ok(".1 是新的旧内容", (Path(tmp) / "faulthandler.log.1").read_text() == "B" * 20)
        ok(".2 是更早的", (Path(tmp) / "faulthandler.log.2").read_text() == "A" * 20)
        _write(path, "C" * 20)
        R.rotate_if_needed(path, policy)
        ok("副本数上界 = backups（.3 不存在）", not (Path(tmp) / "faulthandler.log.3").exists())
        ok(".2 已位移到最老一份", (Path(tmp) / "faulthandler.log.2").read_text() == "B" * 20)


def test_busy_by_default_and_force_truncate() -> None:
    """句柄被占用时默认返回 busy 且不动文件；只有显式 force 才复制+清空。"""
    real_replace = os.replace

    def boom(src, dst):  # 模拟「写入方持有句柄，rename 被拒」
        if str(src).endswith("launcher_service.log"):
            raise PermissionError(32, "另一个程序正在使用此文件")
        return real_replace(src, dst)

    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        path = Path(tmp) / "launcher_service.log"
        _write(path, "X" * 64)
        policy = R.Policy(max_bytes=16, backups=3)
        with mock.patch.object(R.os, "replace", side_effect=boom):
            action = R.rotate_if_needed(path, policy)
        ok("占用 → busy（默认不切）", action == "busy", action)
        ok("busy 不动当前文件", path.read_text() == "X" * 64)
        ok("busy 不产生备份", not (Path(tmp) / "launcher_service.log.1").exists())

    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        path = Path(tmp) / "launcher_service.log"
        _write(path, "X" * 64)
        policy = R.Policy(max_bytes=16, backups=3)
        with mock.patch.object(R.os, "replace", side_effect=boom):
            action = R.rotate_if_needed(path, policy, force_truncate=True)
        ok("force → copy_truncate", action == "copy_truncate", action)
        ok("force 时备份拿到完整内容", (Path(tmp) / "launcher_service.log.1").read_text() == "X" * 64)
        ok("force 时当前文件被清空但仍存在", path.exists() and path.stat().st_size == 0)


def test_missing_and_disabled() -> None:
    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        ok("不存在 → missing", R.rotate_if_needed(Path(tmp) / "nope.log") == "missing")
        path = Path(tmp) / "watchdog.log"
        _write(path, "Y" * 100)
        with mock.patch.dict(os.environ, {"YINYUE_LOG_ROTATE_DISABLE": "1"}):
            ok("关闭开关 → disabled", R.rotate_if_needed(path, R.Policy(10, 2)) == "disabled")
        ok("关闭时不动文件", path.read_text() == "Y" * 100)


def test_policy_env_override() -> None:
    base = R.policy_for("launcher_service.log")
    ok("默认策略来自 DEFAULT_POLICIES", (base.max_bytes, base.backups) == (10 * 1024 * 1024, 5), base)
    with mock.patch.dict(os.environ, {"YINYUE_LOG_ROTATE_MAX_MB": "7", "YINYUE_LOG_ROTATE_BACKUPS": "2"}):
        tuned = R.policy_for("launcher_service.log")
        ok("env 覆盖上限", tuned.max_bytes == 7 * 1024 * 1024, tuned)
        ok("env 覆盖份数", tuned.backups == 2, tuned)
    with mock.patch.dict(os.environ, {"YINYUE_LOG_ROTATE_MAX_MB": "not-a-number"}):
        ok("非法 env 回落默认", R.policy_for("launcher_service.log").max_bytes == 10 * 1024 * 1024)
    unknown = R.policy_for("something_new.log")
    ok("未知文件给保守默认", (unknown.max_bytes, unknown.backups) == (10 * 1024 * 1024, 3), unknown)


def test_history_with_date_suffix_untouched() -> None:
    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        path = Path(tmp) / "faulthandler.log"
        dated = Path(tmp) / "faulthandler.log.20261017"
        _write(path, "Z" * 40)
        _write(dated, "OLD-DATED\n")
        R.rotate_if_needed(path, R.Policy(10, 2))
        ok("日期后缀历史文件仍在", dated.exists() and dated.read_text() == "OLD-DATED\n")


def test_line_integrity_and_glob() -> None:
    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        path = Path(tmp) / "yinyue.log"
        lines = [json.dumps({"i": i, "event": "x"}, ensure_ascii=False) for i in range(300)]
        _write(path, "\n".join(lines) + "\n")
        policy = R.Policy(max_bytes=1024, backups=3)
        R.rotate_if_needed(path, policy)
        rotated = Path(tmp) / "yinyue.log.1"
        got = rotated.read_text(encoding="utf-8").splitlines()
        ok("行数不变", len(got) == 300, len(got))
        ok("每行仍是合法 JSON", all(json.loads(line)["event"] == "x" for line in got))
        picked = sorted(p.name for p in Path(tmp).glob("yinyue.log*"))
        ok("现有 glob（yinyue.log*）仍能同时看到新老文件", picked == ["yinyue.log.1"], picked)


def test_rotate_all_and_cli() -> None:
    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        for name in ("launcher_service.log", "faulthandler.log"):
            _write(Path(tmp) / name, "Q" * (11 * 1024 * 1024))
        with mock.patch.dict(os.environ, {"YINYUE_LOG_ROTATE_MAX_MB": "1", "YINYUE_LOG_ROTATE_BACKUPS": "2"}):
            result = R.rotate_all(tmp)
            ok("批量切分两部都 rename", result["launcher_service.log"] == "rename" and result["faulthandler.log"] == "rename", result)
            ok("未知/不存在条目也在结果里", "watchdog.log" in result and result["watchdog.log"] == "missing")
            sizes = {name: (Path(tmp) / name).stat().st_size for name in ("launcher_service.log.1", "faulthandler.log.1")}
            # 首次轮转是「整文件位移」，所以 .1 会等于轮转前的体积（文件本来超限）；
            # 「有上界」靠副本数 + 之后的每一次切分，见 test_real_measurement。
            ok("首次轮转保留原体积、不复制", all(size == 11 * 1024 * 1024 for size in sizes.values()), sizes)
        # --dry-run 不许动文件
        _write(Path(tmp) / "watchdog.log", "W" * 4096)
        before = (Path(tmp) / "watchdog.log").stat().st_size
        rc = R.main(["--dir", tmp, "--dry-run", "--only", "watchdog.log"])
        ok("--dry-run exit 0", rc == 0, rc)
        ok("--dry-run 不动文件", (Path(tmp) / "watchdog.log").stat().st_size == before)
        buf = _io.StringIO()
        with mock.patch("sys.stdout", buf):
            R.main(["--dir", tmp, "--report"])
        out = buf.getvalue()
        ok("--report 打印策略与占用", "launcher_service.log" in out and "policy MB" in out, out[:80])
        # --report 必须是只读的（曾经因为顺延到 rotate_all 把生产日志真切了）
        _write(Path(tmp) / "manual_scheduler.log", "M" * (2 * 1024 * 1024))
        before_m = (Path(tmp) / "manual_scheduler.log").stat().st_size
        buf2 = _io.StringIO()
        with mock.patch.dict(os.environ, {"YINYUE_LOG_ROTATE_MAX_MB": "1"}):
            with mock.patch("sys.stdout", buf2):
                R.main(["--dir", tmp, "--report"])
        ok("--report 不轮转", (Path(tmp) / "manual_scheduler.log").stat().st_size == before_m, before_m)
        ok("默认（无 --apply）只预览", "would rotate" in buf2.getvalue(), buf2.getvalue()[:60])
        with mock.patch.dict(os.environ, {"YINYUE_LOG_ROTATE_MAX_MB": "1"}):
            rc2 = R.main(["--dir", tmp, "--apply", "--only", "manual_scheduler.log"])
        ok("--apply 才真切分", rc2 == 0 and (Path(tmp) / "manual_scheduler.log.1").stat().st_size == before_m, rc2)


def test_real_measurement() -> None:
    """实测：固定体积的日志连转 4 次，份数不超 backups 且目录总量不再增长。"""
    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        path = Path(tmp) / "launcher_service.log"
        policy = R.Policy(max_bytes=64 * 1024, backups=2)

        def fill() -> None:
            with _io.open(path, "w", encoding="utf-8", newline="\n") as handle:
                for i in range(3000):
                    handle.write(f'{{"i": {i}, "pad": "{"p" * 40}"}}\n')

        totals = []
        for _round in range(4):
            fill()
            R.rotate_if_needed(path, policy)
            totals.append(sum(p.stat().st_size for p in Path(tmp).glob("launcher_service.log*")))
        names = sorted(p.name for p in Path(tmp).glob("launcher_service.log*"))
        ok("份数上界 = backups", len(names) <= policy.backups, names)
        ok("总量在第一次后就不再增长", totals[1] == totals[2] == totals[3], totals)
        ok("总量上界 ≈ backups × 单份体积", totals[-1] <= policy.backups * max(totals), totals)
        newest = Path(tmp) / names[-1]
        tail = newest.read_text(encoding="utf-8").splitlines()[-1]
        ok("轮转件尾部仍是完整 JSON 行", json.loads(tail)["pad"].startswith("p"), tail[:40])


def main() -> int:
    test_noop_under_limit()
    test_rename_chain()
    test_busy_by_default_and_force_truncate()
    test_missing_and_disabled()
    test_policy_env_override()
    test_history_with_date_suffix_untouched()
    test_line_integrity_and_glob()
    test_rotate_all_and_cli()
    test_real_measurement()
    print()
    if _FAIL:
        print(f"FAILED {len(_FAIL)} / {_PASS + len(_FAIL)}")
        for item in _FAIL:
            print(" -", item)
        return 1
    print(f"ALL PASSED ({_PASS})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
