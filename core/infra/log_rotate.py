"""日志轮转：给「不是 yinyue.log」的那几个只增长不切分的文件补上上限。

为什么单独一个模块：``yinyue.log`` 已经由 ``core.infra.logging.init_logging`` 里的
``RotatingFileHandler``（20MB × 10）管好；真正失控的是三条**不经过 logging** 的写入：
``launcher_service.log``（启动器 ``scripts/yinyue_launcher.py:_spawn`` 把调度器 stdout
重定向进去）、``faulthandler.log``（``run.py`` 直接 ``open(...,"a")``）、
``autostart_run.err.log``（计划任务侧重定向）。它们都持有长期句柄，Windows 上
**不能**对正在被独占打开的文件做 rename，所以这里两条路都留着：
先试 ``os.replace``（句柄已关闭时最干净）；失败就退 ``copy + truncate``（句柄还开着
时也能切分，代价是那一瞬间多写一份）。

约定（避免以后又散落）：上限与保留份数只在 :data:`DEFAULT_POLICIES` 写一次；
env 只做覆盖：``YINYUE_LOG_ROTATE_MAX_MB``（全局上限，MB）、
``YINYUE_LOG_ROTATE_BACKUPS``（全局保留份数）、
``YINYUE_LOG_ROTATE_DISABLE=1``（整体关掉，回滚用）。

命名约定：``<name>`` → ``<name>.1`` → ``<name>.2`` …，与 ``yinyue.log`` 一致，
这样 ``scripts/report_terminal_drops.py`` / ``scripts/report_recall_yield.py`` 现有
glob（``yinyue.log*``）不需要改。带日期后缀的历史文件（如
``faulthandler.log.20261017``）**永远不动**：它不匹配数字后缀的轮转序列。
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from safelog import warn
from core.infra.envflags import flag

__all__ = [
    "Policy",
    "DEFAULT_POLICIES",
    "policy_for",
    "rotate_if_needed",
    "rotate_all",
    "main",
]

_MB = 1024 * 1024


@dataclass(frozen=True)
class Policy:
    """单文件上限与保留份数。"""

    max_bytes: int
    backups: int


#: 唯一的策略来源。``yinyue.log`` 列在这里只是为了「读数一致」，实际切分由
#: ``RotatingFileHandler`` 负责（重复切分不会出错，只是不会发生）。
DEFAULT_POLICIES: dict[str, Policy] = {
    "yinyue.log": Policy(20 * _MB, 10),
    "launcher_service.log": Policy(10 * _MB, 5),
    "faulthandler.log": Policy(5 * _MB, 5),
    "autostart_run.err.log": Policy(10 * _MB, 5),
    "autostart_run.log": Policy(10 * _MB, 5),
    "watchdog.log": Policy(5 * _MB, 5),
    "manual_scheduler.log": Policy(10 * _MB, 3),
    # 2026-11-01：体检历史（social/arousal 散点，采集器每 2 秒 append 一条）。
    # 面板只显示最后 240 个点，10MB×3 ≈ 十几天的点数，再老的对谁都不可见。
    "history.jsonl": Policy(10 * _MB, 3),
    # 2026-11-05：被拦下来的话（四道闸共用的旁证，`loop/ports.py::_log_blocked` 追加）。
    # 一天最多几十行、一行 < 400 字节，1MB×2 ≈ 好几年；再老的对排障没有用。
    "blocked.jsonl": Policy(1 * _MB, 2),
    # 2026-11-05 §35.6 第 1 条：退出账本只追加不轮转。一天几十行、一行 ~200 字节，
    # 1MB×5 ≈ 几十年；保留多几代是因为「27 次启动 vs 9 条退出」那种账要对很久。
    "exits.jsonl": Policy(1 * _MB, 5),
}

_ENV_MAX_MB = "YINYUE_LOG_ROTATE_MAX_MB"
_ENV_BACKUPS = "YINYUE_LOG_ROTATE_BACKUPS"
_ENV_DISABLE = "YINYUE_LOG_ROTATE_DISABLE"


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        warn("core/infra/log_rotate.py:75 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return None
    return value if value > 0 else None


def rotation_disabled() -> bool:
    return flag(_ENV_DISABLE)


def policy_for(name: str) -> Policy:
    """取某个文件名的策略（env 覆盖优先，未知文件名给一个保守默认）。"""
    base = DEFAULT_POLICIES.get(Path(name).name)
    max_bytes = base.max_bytes if base else 10 * _MB
    backups = base.backups if base else 3
    global_mb = _env_int(_ENV_MAX_MB)
    if global_mb:
        max_bytes = global_mb * _MB
    global_backups = _env_int(_ENV_BACKUPS)
    if global_backups:
        backups = global_backups
    return Policy(max_bytes=max_bytes, backups=max(1, backups))


def _rotated(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _shift_chain(path: Path, backups: int) -> None:
    """把 ``.N-1`` 挪到 ``.N``（从老到新），最老的（``.backups``）丢弃。

    只处理**纯数字后缀**：``faulthandler.log.20261017`` 这种历史文件不参与、
    也不会被删 —— 这是「不许删历史」的落法。
    """
    oldest = _rotated(path, backups)
    if oldest.exists():
        try:
            oldest.unlink()
        except OSError:
            warn("core/infra/log_rotate.py:112 OSError", err=repr(__import__("sys").exc_info()[1]))
    for index in range(backups - 1, 0, -1):
        src = _rotated(path, index)
        if not src.exists():
            continue
        try:
            os.replace(src, _rotated(path, index + 1))
        except OSError:
            warn("core/infra/log_rotate.py:120 OSError", err=repr(__import__("sys").exc_info()[1]))
            shutil.copyfile(src, _rotated(path, index + 1))
            try:
                src.unlink()
            except OSError:
                warn("core/infra/log_rotate.py:124 OSError", err=repr(__import__("sys").exc_info()[1]))


def rotate_if_needed(path: str | Path, policy: Policy | None = None, *, force_truncate: bool = False) -> str:
    """按需切分一个文件，返回发生了什么。

    返回值：``noop``（没到上限）/ ``rename``（干净 rename）/
    ``busy``（句柄仍被占用，默认不动它）/ ``copy_truncate``（显式 force 时的复制+清空）/ ``missing`` / ``disabled``。
    """
    if rotation_disabled():
        return "disabled"
    target = Path(path)
    if not target.exists():
        return "missing"
    pol = policy or policy_for(target.name)
    try:
        size = target.stat().st_size
    except OSError:
        warn("core/infra/log_rotate.py:142 OSError", err=repr(__import__("sys").exc_info()[1]))
        return "missing"
    if size < pol.max_bytes:
        return "noop"
    try:
        # 先挪旧的备份链，再把当前文件挪成 .1
        _shift_chain(target, pol.backups)
        os.replace(target, _rotated(target, 1))
        return "rename"
    except OSError:
        # 句柄被占用（Windows 的常态：写入方 open 时不带 FILE_SHARE_DELETE）。
        #
        # 2026-10-18 实测教训：这里过去会「复制一份再 truncate(0)」，但写入方不会
        # 重置自己的文件偏移 —— truncate 之后它继续按老 offset 追加，结果生出一个
        # 前 310 MB 全是 NUL 的稀疏文件（数据没丢，但体积翻倍且形状骗人）。
        # 所以默认**不截断**，返回 ``busy`` 交给运维在「写入方已停」的窗口处理。
        warn("core/infra/log_rotate.py:151 OSError", err=repr(__import__("sys").exc_info()[1]))
        if not force_truncate:
            return "busy"
        try:
            _shift_chain(target, pol.backups)
            shutil.copyfile(target, _rotated(target, 1))
            with open(target, "r+b") as handle:
                handle.truncate(0)
            return "copy_truncate"
        except OSError:
            warn("core/infra/log_rotate.py:166 OSError", err=repr(__import__("sys").exc_info()[1]))
            return "failed"


def rotate_all(
    log_dir: str | Path = "logs",
    names: Iterable[str] | None = None,
    *,
    force_truncate: bool = False,
) -> dict[str, str]:
    """批量切分；``names`` 为空时按 :data:`DEFAULT_POLICIES` 全跑一遍。"""
    directory = Path(log_dir)
    selected = list(names) if names else list(DEFAULT_POLICIES)
    out: dict[str, str] = {}
    for name in selected:
        out[name] = rotate_if_needed(directory / name, force_truncate=force_truncate)
    return out


def _report(log_dir: Path) -> None:
    total = 0
    print(f"{'file':<28}{'MB':>10}  {'policy MB':>10}  backups")
    for name, policy in DEFAULT_POLICIES.items():
        path = log_dir / name
        size = path.stat().st_size if path.exists() else 0
        total += size
        print(f"{name:<28}{size / _MB:>10.2f}  {policy.max_bytes / _MB:>10.1f}  {policy.backups}")
    rolled = sorted(p for p in log_dir.glob("*.*") if p.name.rsplit(".", 1)[-1].isdigit())
    rolled_size = sum(p.stat().st_size for p in rolled)
    print(f"\n受管文件合计 {total / _MB:.1f} MB；轮转件合计 {rolled_size / _MB:.1f} MB（{len(rolled)} 个）")
    print(f"目录总计 {sum(p.stat().st_size for p in log_dir.glob('*') if p.is_file()) / _MB:.1f} MB")


def _preview(log_dir: Path, names: Iterable[str] | None = None) -> None:
    """只读预览：打印每个文件当前体积与上限，不碰任何文件。"""
    for name in (names or list(DEFAULT_POLICIES)):
        path = log_dir / name
        size = path.stat().st_size if path.exists() else 0
        pol = policy_for(name)
        print(f"{name:<28} {size / _MB:>8.2f} MB / {pol.max_bytes / _MB:.1f} MB → "
              f"{'would rotate' if size >= pol.max_bytes else 'ok'}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="日志轮转（只切分、不删既有历史）")
    parser.add_argument("--dir", default="logs", help="日志目录（默认 logs）")
    parser.add_argument("--only", nargs="+", default=None, help="只处理这些文件名")
    parser.add_argument("--apply", action="store_true", help="真的切分（不加就只预览）")
    parser.add_argument("--force-truncate", action="store_true",
                        help="句柄占用时允许复制+清空（会在活文件里留下 NUL 空洞，慎用）")
    parser.add_argument("--dry-run", action="store_true", help="--apply 的反面（默认行为，保留兼容）")
    parser.add_argument("--report", action="store_true", help="打印当前占用与策略")
    args = parser.parse_args(argv)

    log_dir = Path(args.dir)
    if args.report:
        _report(log_dir)
    if not args.apply or args.dry_run:
        # 默认只预览：任何「报告」形态都必须是只读的（2026-10-18 教训：
        # 只加 --report 会顺延到 rotate_all，把生产日志真切了）。
        _preview(log_dir, args.only)
        return 0
    result = rotate_all(log_dir, args.only, force_truncate=args.force_truncate)
    for name, action in result.items():
        if action != "noop":
            print(f"{name}: {action}")
            if action == "busy":
                print(f"  ↳ {name} 仍被进程占用；请在写入方停止的窗口再切，或加 --force-truncate（会留 NUL 空洞）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
