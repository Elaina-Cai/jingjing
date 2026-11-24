"""进程临时目录固定：把 ``TEMP``/``TMP``/``TMPDIR``/``SQLITE_TMPDIR`` 钉到可写目录。

**为什么要这个模块**：SQLite 在需要临时 B 树 / 大排序 / 某些 ``RETURNING`` 中间结果
时会去系统临时目录建文件。调度器由计划任务/启动器拉起时，``TEMP`` 可能指向一个已被
清理、或当前用户无权写入的目录；这时 SQLite 只报 ``unable to open database file``
（SQLITE_CANTOPEN），从错误文本完全看不出是临时目录的问题，而且新建连接也一样失败
（连接池 dispose 无效）。固定到仓库内 ``logs/tmp`` 后这一类失败不再可能发生。

**必须尽早调用**：在进程打开任何 SQLite 连接之前。驱动/内核会缓存临时目录，晚设无效。
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import structlog
from safelog import warn

log = structlog.get_logger()

__all__ = ["pin_temp_dir", "usable_dir"]

_ENV_KEYS = ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR")


def usable_dir(path: Path) -> bool:
    """``path`` 能否创建并真实写入一个探针文件（写完即删）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".probe_{os.getpid()}_{int(time.time() * 1000)}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        warn("core/infra/tempdir.py:36 OSError", err=repr(__import__("sys").exc_info()[1]))
        return False


def default_candidates(repo_root: Path | None = None) -> list[Path]:
    """按优先级给出候选临时目录：仓库内 ``logs/tmp`` → ``%LOCALAPPDATA%\\Temp`` → 现值。"""
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    out = [root / "logs" / "tmp"]
    local = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        out.append(Path(local) / "Temp")
    current = str(tempfile.gettempdir() or "").strip()
    if current:
        out.append(Path(current))
    return out


def pin_temp_dir(
    *,
    prefer: Path | None = None,
    candidates: list[Path] | None = None,
) -> str:
    """把进程临时目录钉到第一个可写候选；返回选中的路径（全不可写时返回 ``""``）。

    ``prefer`` 用于测试/运维显式指定；全不可写时**不阻断启动**，只写一条
    ``temp_dir_unusable`` 警告（此时连日志目录都靠既有 TEMP，能记多少记多少）。
    """
    tried: list[Path] = []
    if prefer is not None:
        tried.append(Path(prefer))
    tried.extend(candidates if candidates is not None else default_candidates())

    chosen: Path | None = None
    for candidate in tried:
        if usable_dir(candidate):
            chosen = candidate
            break
    if chosen is None:
        log.warning("temp_dir_unusable", tried=[str(p) for p in tried])
        return ""

    value = str(chosen)
    for key in _ENV_KEYS:
        os.environ[key] = value
    tempfile.tempdir = value
    log.info("temp_dir_pinned", path=value, writable=True)
    return value
