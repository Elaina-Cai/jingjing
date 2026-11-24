"""测试侧唯一的临时目录入口。

为什么需要它
------------
本机沙箱下子进程**写不进**系统 TEMP（``PermissionError [Errno 13]``）。``tempfile``
找不到可写候选目录时，``tempfile._candidate_tempdir_list()`` 的最后一项是
``os.getcwd()``，于是 ``tempfile.gettempdir()`` 静默**回落到仓库根**。
2026-10-01 23:14 那轮 ``scripts/run_all_tests.py``（135/153）大面积变红就是它：

* 隔离根落成 ``<仓库根>/yinyue_test_root_20261001_231419_794fd7``，仓库被污染出
  ``gate_*`` / ``tmp*`` / ``corr_*`` / ``jieba.cache``；
* 更要命的是 SQLite：win32 VFS 用 ``GetTempPath()``（读 ``TMP``/``TEMP``）取临时
  目录，路径不可写时直接 ``SQLITE_CANTOPEN``，报成
  ``OperationalError: unable to open database file``——18 个测试同时红，而同一份
  代码在 TEMP 可写的 20:22 那轮是 152/152。

基座优先级（**只有这两个**）
----------------------------
1. ``YINYUE_TEST_TMP``：显式指定，交给调用方；
2. ``ROOT/.test_tmp``：仓库内、workspace 里，子进程一定有写权。

**绝不**把「``gettempdir()`` 落在 ROOT 之内」当基座——那正是故障形态本身。
导入本模块即把当前进程的 ``TEMP``/``TMP``/``TMPDIR``/``SQLITE_TMPDIR`` 与
``tempfile.tempdir`` 一起钉到基座：SQLite 的 ``GetTempPath`` 与 jieba 的缓存都按
环境变量走，钉住它们才算真修好，只把目录换个地方建是治标。

用法::

    from _tempdirs import make_db_dir

    tmp = make_db_dir("gate_")     # 代替 tempfile.mkdtemp(prefix="gate_")
    ...
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 显式指定基座的环境变量（优先级 1）。
ENV_BASE = "YINYUE_TEST_TMP"

#: 仓库内兜底基座（优先级 2）。刻意用 ``mkdir`` 而不是 ``mkdtemp``：默认 ACL，
#: 受限环境里 sqlite 才写得进去。
FALLBACK_BASE = ROOT / ".test_tmp"

#: 一并改道的环境变量：TEMP/TMP 是 Windows 的约定，TMPDIR 是 POSIX 的，
#: SQLITE_TMPDIR 是 SQLite 自己的。
TEMP_ENV_KEYS = ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR")

_base: Path | None = None
_source = "ROOT/.test_tmp"


def _writable(path: Path) -> bool:
    """``path`` 能不能建目录并写文件（真写一次，别信声明）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_probe_{uuid.uuid4().hex}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _inside_repo(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(ROOT)
    except OSError:
        return False


def system_temp_status() -> tuple[bool, str]:
    """系统临时目录是否**不可用**，以及原因（供打印诊断行）。"""
    try:
        system = Path(tempfile.gettempdir())
    except Exception as exc:  # noqa: BLE001 - 拿不到也要能继续
        return True, f"gettempdir() 取不到: {exc}"
    if _inside_repo(system):
        # 这正是 23:14 那轮的形态：tempfile 回落到 CWD（仓库根）。
        return True, f"gettempdir() 回落进仓库根（{system}）"
    if not _writable(system):
        return True, f"{system} 不可写"
    return False, str(system)


def temp_base() -> Path:
    """返回（并按需创建）本进程的临时基座；结果进程内缓存。"""
    global _base, _source
    if _base is not None:
        return _base

    explicit = (os.environ.get(ENV_BASE) or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if _writable(candidate):
            _base = candidate.resolve()
            _source = ENV_BASE
            return _base
        print(f"[tmp] {ENV_BASE}={candidate} 不可写，改用 {FALLBACK_BASE}")
    else:
        unusable, why = system_temp_status()
        if unusable:
            print(f"[tmp] 系统临时目录不可用（{why}），改用 {FALLBACK_BASE}")

    FALLBACK_BASE.mkdir(parents=True, exist_ok=True)
    _base = FALLBACK_BASE
    _source = "ROOT/.test_tmp"
    return _base


def make_db_dir(prefix: str = "tmp_") -> Path:
    """在基座下建一个唯一子目录并返回 ``Path``（``mkdtemp`` 的替代品）。

    与 ``tempfile.mkdtemp`` 的区别只有两点，都是刻意的：

    * 建在基座下，**不会**因为系统 TEMP 不可用而落进仓库根；
    * 用 ``Path.mkdir``（继承默认 ACL）而不是 ``0o700``，受限环境里 sqlite
      才连得上。

    生命周期与 ``mkdtemp`` 一致：调用方自己 ``shutil.rmtree``。
    """
    path = temp_base() / f"{prefix}{uuid.uuid4().hex[:16]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def install_process_tempdir() -> Path:
    """把本进程的临时目录钉到基座，返回基座。

    SQLite（``GetTempPath``）和 jieba（缓存文件）都按 ``TMP``/``TEMP`` 找临时目录；
    系统 TEMP 不可写时 SQLite 直接 ``unable to open database file``。改环境变量对
    它们**立刻**生效（逐次读），所以这一步不是装饰。
    """
    base = temp_base()
    for key in TEMP_ENV_KEYS:
        os.environ[key] = str(base)
    tempfile.tempdir = str(base)
    return base


def describe() -> str:
    """一行摘要，给 runner 打印实际基座用。"""
    return f"{temp_base()} (来自 {_source})"


install_process_tempdir()
