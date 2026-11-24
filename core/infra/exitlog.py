"""谁退出的、什么码、为什么 —— 一行一条 JSON，追加写，绝不互相踩。

为什么单独一个模块（2026-11-05，主人「要」）：一夜重启 23 次，四家各拉一部分
（`scripts/supervisor.py`、`scripts/desk_supervisor.py`、`autostart.bat` 的 bat 级循环、
计划任务 `\\yinyue_watchdog`），每家只在各自的 `.out` 里写一句中文，**没有任何地方能回答
「这一夜到底谁退了、什么码、为什么」**。E-74 的教训是「写不出来 和 没发生 长得一样」，
这里是它的下一层：**写得出来，但散在四个地方，等于写不出来。**

口径：
* 只管**退出**（以及"因为总闸而退出"），不管启动 —— 启动次数可以从这里反推；
* `rc` 保留原值，`why` 是给人看的归类（见 `why_from_rc`）；
* 写失败只 `log.warning` 一次，绝不影响调用方（这是账本，不是闸门）；
* 文件固定 `logs/exits.jsonl`，只追加，不重写、不轮转（一年也就几十 KB）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "logs" / "exits.jsonl"

#: 谁在退。新增角色要同时补 `scripts/exit_report.py` 的展示名。
ROLES: tuple[str, ...] = ("scheduler", "engine", "desk", "supervisor", "desk_supervisor", "bat")

#: `scripts/supervisor.py` 的 RESTART_CODE：主动重启，不是故障。
RESTART_CODE = 42


def wrap_main(fn: Callable[[], Any], *, role: str = "scheduler", detail: str = "",
              stop_file: Any = None, stop_files: Any = None, path: Any = None) -> Any:
    """跑一个入口函数，**退出时**记一行（§33.5 第 2 条，2026-11-05 主人批的 ①+②）。

    为什么要有这一层：之前只有监管器视角的「它退了，码是 X」，谁也说不清**是谁让它退的**——
    总闸（`logs/all.stop`）、控制台 Ctrl+C、还是它自己崩了。三者在监管器眼里都是「子进程退出」。

    `stop_file` 只认一个总闸；引擎要同时认 `logs/loop.stop` 与 `logs/all.stop`，用 `stop_files`（任一在即算）。

    行为：正常返回 → `rc=0`（有总闸时记 `all_stop`，否则 `clean`）；
    `SystemExit` → 用它自己的码；`KeyboardInterrupt` → `interrupted`；其它异常 → 记一笔再**原样抛**。
    账本写不进去只告警，绝不影响退出本身。
    """
    def _gate_files() -> tuple:
        got = []
        if stop_file is not None:
            got.append(stop_file)
        if stop_files:
            got.extend([x for x in stop_files if x is not None])
        return tuple(got)

    def _stopped() -> bool:
        # 2026-11-05：要在**退出那一刻**看总闸。第一版在入口处算一次，于是「运行中被总闸关掉」
        # 永远记成 clean（13:45 那次真账就是 clean 而不是 all_stop）。
        n_bad = 0
        for one in _gate_files():
            try:
                if Path(one).exists():
                    return True
            except OSError:
                n_bad += 1
                continue
        if n_bad:
            log.warning(f"exitlog.gate_check_failed: n={n_bad}（总闸文件 stat 不动，按「没被关」处理）")
        return False

    # 2026-11-06（B）：入口先记一条「我起来了」，顺带算出上一条账到现在空了多久。
    # 机器睡眠/断电/崩溃之后进程重新起来，中间空了多久以前没人记。
    record_start(role, detail=detail, path=path)
    try:
        got = fn()
    except KeyboardInterrupt:
        record(role, rc=1, why="interrupted", detail=(detail + "｜Ctrl+C").strip("｜"), path=path)
        raise
    except SystemExit as exc:
        code = exc.code
        rc = code if isinstance(code, int) else (0 if code is None else 1)
        record(role, rc=rc, why=("all_stop" if _stopped() else ""),
               detail=(detail + "｜SystemExit").strip("｜"), path=path)
        raise
    except BaseException as exc:  # noqa: BLE001 - 记一笔再原样抛，绝不吞
        record(role, rc=1, detail=("%s｜未捕获异常 %r" % (detail, exc))[:200], path=path)
        raise
    record(role, rc=0, why=("all_stop" if _stopped() else "clean"), detail=detail, path=path)
    return got


__all__ = ["wrap_main", "LOG", "RESTART_CODE", "ROLES", "record", "record_start", "summary", "tail", "why_from_rc"]


def why_from_rc(rc: Any, *, stopped: bool = False) -> str:
    """把退出码翻成人话。``stopped=True`` 表示「因为总闸/哨兵而退出」，优先级最高。

    Windows 上几个常见的码：``0xC000013A`` = 控制台被关 / Ctrl+C（`-1073741510`），
    ``0xC0000005`` = 访问越界（`-1073741819`），``-1`` = 被 `taskkill` 掐。
    """
    if stopped:
        return "all_stop"
    if rc is None:
        return "unknown"
    try:
        n = int(rc)
    except (TypeError, ValueError):
        return "rc:%r" % (rc,)
    if n == 0:
        return "clean"
    if n == RESTART_CODE:
        return "restart_requested"
    if n in (-1, 1):
        return "killed"
    if n in (-1073741510, 3221225786):
        return "console_closed"
    if n in (-1073741819, 3221225477):
        return "access_violation"
    if n > 0x7FFFFFFF:
        return "crash:0x%08X" % (n & 0xFFFFFFFF)
    return "rc:%d" % n


def record(role: str, *, rc: Any = None, why: str = "", pid: int | None = None,
           child: str = "", detail: str = "", now: float | None = None,
           path: Path | str | None = None) -> bool:
    """记一条退出。返回是否写成功（写失败不影响调用方）。"""
    target = Path(path) if path is not None else LOG
    at = float(now if now is not None else time.time())
    row = {
        "at": at,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)),
        "role": str(role or ""),
        "rc": rc if rc is None or isinstance(rc, (int, float)) else str(rc),
        "why": str(why or why_from_rc(rc)),
        "pid": int(pid) if isinstance(pid, int) else (os.getpid() if pid is None else None),
        "child": str(child or ""),
        "detail": str(detail or "")[:200],
    }
    return _append(row, target)


def _append(row: dict, target: Path) -> bool:
    """把一行写进账本（先轮转、再追加）。写不进去只告警，绝不影响调用方。"""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            from core.infra import log_rotate as _lr

            _how = _lr.rotate_if_needed(str(target))
            if _how not in ("noop", "missing", "rename"):
                log.warning(f"exitlog.rotate: path={target} how={_how}")
        except Exception as _rex:  # noqa: BLE001 - 轮不动也得把这条记下来
            log.warning(f"exitlog.rotate_failed: path={target} err={_rex!r}")
        line = json.dumps(row, ensure_ascii=False) + chr(10)
        with open(target, "a", encoding="utf-8", newline=chr(10)) as fh:
            fh.write(line)
        return True
    except OSError as exc:
        log.warning(f"exitlog.write_failed: path={target} err={exc!r}")
        return False


def record_start(role: str, *, detail: str = "", now: float | None = None,
                 path: Path | str | None = None, pid: int | None = None) -> dict:
    """记一条「我起来了」，并算出上一次同类角色到这一次**空了多久**（`gap_hours`）。

    为什么要有这一条：2026-11-06 02:54 机器睡到 14:34，账本里只留下
    `engine rc=None orphan_cleaned 父进程已死` 与 `scheduler rc=3`——读起来像崩溃，
    真相是「她不在 11.7 小时」。`gap_hours` 只跟**同类角色的上一条退出行**比，
    第一条就是 None（没有「空了多久」这回事）。启动行 `kind="start"`，不参与退出统计。
    """
    target = Path(path) if path is not None else LOG
    at = float(now if now is not None else time.time())
    prev_at, prev_ts = 0.0, ""
    n_bad_at = 0
    for one in _rows(path=target):
        if str(one.get("role") or "") != str(role or ""):
            continue
        if str(one.get("kind") or "") == "start":
            continue
        try:
            got = float(one.get("at") or 0.0)
        except (TypeError, ValueError) as exc:
            n_bad_at += 1
            log.warning(f"exitlog.bad_at: role={role} ts={one.get('ts')} err={exc!r}")
            continue
        if got > prev_at:
            prev_at, prev_ts = got, str(one.get("ts") or "")
    if n_bad_at:
        log.warning(f"exitlog.start_skipped: lines={n_bad_at}（这些行 at 读不懂，按「没上一条」处理）")
    gap_h = max(0.0, (at - prev_at) / 3600.0) if prev_at else None
    row = {
        "at": at,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)),
        "role": str(role or ""),
        "kind": "start",
        "rc": None,
        "why": "start",
        "pid": int(pid) if isinstance(pid, int) else (os.getpid() if pid is None else None),
        "child": "",
        "detail": str(detail or "")[:200],
        "gap_hours": None if gap_h is None else round(gap_h, 2),
        "gap_from": prev_ts,
    }
    row["ok"] = _append(row, target)
    if gap_h is not None and gap_h >= 1.0:
        log.warning(f"exitlog.started: role={role} gap_hours={gap_h:.1f} prev={prev_ts or '?'}")
    return row


def _rows(*, path: Path | str | None = None) -> list[dict]:
    target = Path(path) if path is not None else LOG
    if not target.exists():
        return []
    out: list[dict] = []
    n_bad = 0
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning(f"exitlog.read_failed: path={target} err={exc!r}")
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            got = json.loads(line)
        except ValueError:
            n_bad += 1
            continue
        if isinstance(got, dict):
            out.append(got)
    if n_bad:
        log.warning(f"exitlog.bad_lines: file={target} n={n_bad}（这些行读不懂，已跳过）")
    return out


def tail(n: int = 20, *, path: Path | str | None = None) -> list[dict]:
    """最近 n 条（新的在后）。"""
    rows = _rows(path=path)
    return rows[-int(n or 20):]


def summary(*, hours: float = 24.0, now: float | None = None,
            path: Path | str | None = None) -> dict:
    """最近 hours 小时的退出账：按角色、按归类各一个计数，外加最近 20 条。

    2026-11-06（B）：启动行（``kind == "start"``）**不算退出**，不进 ``total`` /
    ``by_role`` / ``by_why``；它们单独进 ``starts``，其中空了一小时以上的进 ``gaps``
    （``role`` / ``gap_hours`` / ``gap_from``）——那是「她不在」的物证
    （机器睡眠、断电、崩溃后重启），以前只留下 `orphan_cleaned`，读起来像崩溃。
    """
    base = float(now if now is not None else time.time())
    since = base - max(0.0, float(hours)) * 3600.0
    by_role: dict[str, int] = {}
    by_why: dict[str, int] = {}
    kept: list[dict] = []
    starts: list[dict] = []
    gaps: list[dict] = []
    for row in _rows(path=path):
        try:
            at = float(row.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        if at < since:
            continue
        kept.append(row)
        if str(row.get("kind") or "") == "start":
            starts.append(row)
            try:
                gap_h = float(row.get("gap_hours"))
            except (TypeError, ValueError):
                gap_h = 0.0
            if gap_h >= 1.0:
                gaps.append({"role": str(row.get("role") or ""), "gap_hours": round(gap_h, 2),
                             "ts": str(row.get("ts") or ""), "gap_from": str(row.get("gap_from") or "")})
            continue
        by_role[str(row.get("role") or "")] = by_role.get(str(row.get("role") or ""), 0) + 1
        by_why[str(row.get("why") or "")] = by_why.get(str(row.get("why") or ""), 0) + 1
    return {"total": len(kept) - len(starts), "starts": len(starts), "gaps": gaps,
            "by_role": by_role, "by_why": by_why,
            "recent": kept[-20:], "hours": float(hours), "since": since}
