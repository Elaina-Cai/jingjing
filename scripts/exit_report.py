"""退出原因账：把「谁退了、什么码、为什么」合成一张表。

两个来源：
1. `logs/exits.jsonl` —— 2026-11-05 起 `core.infra.exitlog.record` 写的（准）；
2. 历史文本 —— `supervisor.out` / `desk_supervisor.out` / `watchdog.log` 里已经存在的行（能读多少读多少），
   以及 `loop_engine.log` 里 `engine_log_sink_ready` 的次数（引擎重启次数只能这样数）。

为什么要有它（2026-11-05，主人「要」）：一夜重启 23 次，四家各写各的 `*.out`，谁都说不出
「谁退了、什么码」；bat 那边十七次全是 `code=0`，而 `0` 恰好是 `scripts/supervisor.py`
正常返回的值 —— 于是「崩了」和「干净退出」在纸上长得一模一样（E-74 的下一层）。

用法：
    python scripts/exit_report.py                 # 最近 24 小时
    python scripts/exit_report.py --hours 6       # 最近 6 小时
    python scripts/exit_report.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"

#: 角色 -> 给人看的名字
SHOWN: dict[str, str] = {
    "scheduler": "主程序",
    "engine": "引擎",
    "desk": "发送工",
    "supervisor": "监管器",
    "desk_supervisor": "desk 监管器",
    "bat": "bat 循环",
}

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_BAT = re.compile(r"^\[周.\s+(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})\.\d+\]\s*(.*)$")
_EXIT_RC = re.compile(r"子进程退出 rc=(-?\d+)")
_ORPHAN = re.compile(r"清掉孤儿引擎 pid=(\d+)")
_BAT_EXIT = re.compile(r"supervisor exited code=(-?\d+) fails=(\d+)")
_DESK_EXIT = re.compile(r"desk 退出 rc=(None|-?\d+)")


def _at_of(line: str, *, fallback: float) -> tuple[float, bool]:
    """取行首时间戳。取不到就用文件 mtime，并返回 (t, False) 表示「时间不精确」。"""
    hit = _TS.match(line)
    if hit:
        try:
            return float(time.mktime(time.strptime(hit.group(1), "%Y-%m-%d %H:%M:%S"))), True
        except ValueError:
            return fallback, False
    hit = _BAT.match(line)
    if hit:
        y, mo, d, hh, mm, ss, _rest = hit.groups()
        try:
            return float(time.mktime((int(y), int(mo), int(d), int(hh), int(mm), int(ss), 0, 0, -1))), True
        except (ValueError, OverflowError):
            return fallback, False
    return fallback, False


def _why(rc):
    from core.infra import exitlog
    return exitlog.why_from_rc(rc)


def parse_supervisor_out(text: str, *, mtime: float = 0.0) -> list[dict]:
    """`logs/supervisor.out`：子进程退出、孤儿引擎、总闸、早退。"""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        at, exact = _at_of(line, fallback=mtime)
        row = {"at": at, "exact": exact, "source": "supervisor.out", "role": "", "rc": None,
               "why": "", "detail": line[:160]}
        hit = _EXIT_RC.search(line)
        if hit:
            rc = int(hit.group(1))
            row.update(role="scheduler", rc=rc, why=_why(rc))
            out.append(row)
            continue
        hit = _ORPHAN.search(line)
        if hit:
            row.update(role="engine", why="orphan_cleaned", detail=line[:160])
            out.append(row)
            continue
        if "收到 logs/all.stop" in line or "logs/all.stop 在：监管器退出" in line:
            row.update(role="supervisor", why="all_stop")
            out.append(row)
            continue
        if "已有监管器在跑，本实例退出" in line or "监管器退出（拉起我的" in line:
            row.update(role="supervisor", why="already_running")
            out.append(row)
            continue
        if "引擎卡死" in line and "已 kill pid=" in line:
            row.update(role="engine", why="killed_by_supervisor")
            out.append(row)
    return out


def parse_desk_supervisor_out(text: str, *, mtime: float = 0.0) -> list[dict]:
    """`logs/desk_supervisor.out`：desk 退出与总闸。被打断的行（多进程交错写）直接忽略。"""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "[desk-supervisor]" not in line:
            continue
        at, exact = _at_of(line, fallback=mtime)
        row = {"at": at, "exact": exact, "source": "desk_supervisor.out", "role": "desk",
               "rc": None, "why": "", "detail": line[:160]}
        hit = _DESK_EXIT.search(line)
        if hit:
            rc = None if hit.group(1) == "None" else int(hit.group(1))
            row.update(rc=rc, why=_why(rc))
            out.append(row)
            continue
        if "看到 all.stop，退出" in line:
            row.update(role="desk_supervisor", why="all_stop")
            out.append(row)
    return out


def parse_watchdog_log(text: str, *, mtime: float = 0.0) -> list[dict]:
    """`logs/watchdog.log`（bat 级循环）：只记「supervisor 退出了，码是多少」。"""
    out: list[dict] = []
    for line in text.splitlines():
        hit = _BAT_EXIT.search(line)
        if not hit:
            continue
        at, exact = _at_of(line, fallback=mtime)
        rc = int(hit.group(1))
        out.append({"at": at, "exact": exact, "source": "watchdog.log", "role": "supervisor",
                    "rc": rc, "why": _why(rc), "detail": line.strip()[:160]})
    return out


def count_engine_starts(text: str) -> int:
    """`loop_engine.log` 里 `engine_log_sink_ready` 的条数＝引擎真正起过的次数。"""
    return sum(1 for line in text.splitlines() if "engine_log_sink_ready" in line)


def _read(path: Path) -> tuple[str, float]:
    if not path.exists():
        return "", 0.0
    try:
        return path.read_text(encoding="utf-8", errors="replace"), path.stat().st_mtime
    except OSError:
        return "", 0.0


def collect(*, logs_dir: Path | str | None = None, hours: float = 24.0,
            now: float | None = None) -> dict:
    """合并两个来源，按时间倒序。返回 ``{"rows","gaps","since","hours","by_why","by_role","engine_starts"}``。

    `logs_dir` 同时决定账本（`exits.jsonl`）与三份 `.out` 的目录：测试与「回看某一份日志」
    都不该悄悄读生产账本。
    """
    from core.infra import exitlog

    base = float(now if now is not None else time.time())
    since = base - max(0.0, float(hours)) * 3600.0
    d = Path(logs_dir) if logs_dir is not None else LOGS

    rows: list[dict] = []
    gaps: list[dict] = []
    # `--logs-dir` 也要管账本本身：以前这里没传 path，测试与「看历史」都只能读生产的
    # `logs/exits.jsonl`（E-111）。
    for row in exitlog.summary(hours=hours, now=base, path=(d / "exits.jsonl")).get("recent") or []:
        if str(row.get("kind") or "") == "start":
            # 2026-11-06（B）：启动行不是「退出」，单独当「空过的时段」列（她不在的证据）。
            try:
                gap_h = float(row.get("gap_hours"))
            except (TypeError, ValueError):
                gap_h = 0.0
            if gap_h >= 1.0:
                gaps.append({"at": float(row.get("at") or 0.0), "role": str(row.get("role") or ""),
                             "gap_hours": round(gap_h, 2),
                             "gap_from": str(row.get("gap_from") or "")})
            continue
        rows.append({"at": float(row.get("at") or 0.0), "exact": True, "source": "exits.jsonl",
                     "role": str(row.get("role") or ""), "rc": row.get("rc"),
                     "why": str(row.get("why") or ""),
                     "detail": str(row.get("detail") or "")[:160]})

    for name, parser in (("supervisor.out", parse_supervisor_out),
                         ("desk_supervisor.out", parse_desk_supervisor_out),
                         ("watchdog.log", parse_watchdog_log)):
        text, mtime = _read(d / name)
        rows.extend(parser(text, mtime=mtime))

    starts = count_engine_starts(_read(d / "loop_engine.log")[0])

    kept = [r for r in rows if float(r.get("at") or 0.0) >= since]
    # 同一份 .out 里常有连续十几行一模一样的「看到 all.stop，退出」（多进程交错追加），
    # 计数会失真；这里把「同来源 + 同角色 + 同归类 + 时间相差 < 2 秒」的折叠成一条。
    folded: list[dict] = []
    for r in kept:
        prev = folded[-1] if folded else None
        if (prev is not None and prev["source"] == r["source"] and prev["role"] == r["role"]
                and prev["why"] == r["why"]
                and abs(float(r["at"]) - float(prev["at"])) < 2.0):
            prev["detail"] = prev["detail"] or r["detail"]
            continue
        folded.append(dict(r))
    kept = folded
    kept.sort(key=lambda r: float(r.get("at") or 0.0))
    by_why: dict[str, int] = {}
    by_role: dict[str, int] = {}
    for r in kept:
        by_why[r["why"]] = by_why.get(r["why"], 0) + 1
        by_role[r["role"]] = by_role.get(r["role"], 0) + 1
    gaps.sort(key=lambda g: float(g.get("at") or 0.0))
    return {"rows": kept, "gaps": gaps[-20:], "since": since, "hours": float(hours),
            "by_why": by_why, "by_role": by_role, "engine_starts": starts}


def render(got: dict) -> str:
    rows = got.get("rows") or []
    lines = ["退出原因账（最近 %.0f 小时）" % float(got.get("hours") or 0)]
    for g in got.get("gaps") or []:
        lines.append("  ! %s 空了 %.1f 小时（上一条 %s）—— 这段时间她不在：机器睡眠/断电/崩溃" % (
            SHOWN.get(str(g.get("role") or ""), str(g.get("role") or "")),
            float(g.get("gap_hours") or 0.0), str(g.get("gap_from") or "?")))
    if not rows:
        lines.append("  这一段没有任何退出记录。")
    else:
        lines.append("  时间                 角色         rc          归类              证据")
        for r in rows[-60:]:
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(float(r["at"])))
            if not r.get("exact"):
                when += "~"
            rc = "" if r.get("rc") is None else str(r.get("rc"))
            lines.append("  %-20s %-10s %-12s %-16s %s" % (
                when, SHOWN.get(r["role"], r["role"]), rc, r["why"], r["source"]))
        lines.append("")
        lines.append("  按归类：" + "，".join("%s %d" % (k, v) for k, v in
                                         sorted(got["by_why"].items(), key=lambda kv: -kv[1])))
        lines.append("  按角色：" + "，".join("%s %d" % (SHOWN.get(k, k), v) for k, v in
                                         sorted(got["by_role"].items(), key=lambda kv: -kv[1])))
    lines.append("  引擎起过的次数（loop_engine.log 里 engine_log_sink_ready）：%d" %
                 int(got.get("engine_starts") or 0))
    lines.append("  注：带 ~ 的时间来自文件 mtime（那行本身没时间戳）。")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="谁退了、什么码、为什么")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--logs-dir", default="")
    ns = ap.parse_args(argv)
    got = collect(logs_dir=(ns.logs_dir or None), hours=ns.hours)
    if ns.json:
        print(json.dumps({k: v for k, v in got.items() if k != "rows"}, ensure_ascii=False))
        for r in got["rows"]:
            print(json.dumps(r, ensure_ascii=False))
    else:
        print(render(got))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
