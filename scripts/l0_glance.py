"""L0 只读观察期的一次性体检：数信号、查残留租约、给结论。

跑法：.venv_it\\Scripts\\python.exe -X utf8 -u scripts/l0_glance.py [--days 7] [--json]

为什么单独一个脚本：L0 的判据是「7 天里有没有出过事」，靠记性一定会忘；
把判据写成代码，到期一条命令就能回答「能不能进 L1」。

只读：不写账本、不改开关、不清理任何东西。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = ROOT / "data" / ".locks"
LOG_DIR = ROOT / "logs"

#: 观察期到期日（过期会打印「该看了」）
DUE_DATE = "2026-10-27"
#: 观察期长度（天）
MIN_DAYS = 7
#: 心跳超过这个秒数就当「可能卡死」
STALE_AFTER_S = 120.0
#: 每会话每天 input_busy 超过这个数就提示看 p90
BUSY_PER_DAY_WARN = 5.0

#: 七个信号 → 日志里可能出现的字样（任一命中即计数一次）
SIGNALS: dict[str, tuple[str, ...]] = {
    "device_guard_shadow": ("device_guard_shadow",),
    "input_busy": ("input_busy", "UiBusy"),
    "lease_stuck": ("lease_stuck",),
    "dirty_composer": ("dirty_composer",),
    "blocked_capability": ("blocked_capability", "blocked:capability"),
    "correction": ("correction_deliver", "correction_shadow", "更正："),
    "legacy_exit_shadow": ("legacy_exit_shadow",),
}

#: 影子档里「不该出现却出现了」的 exit_status（`blocked:*` 是正常的：闸门在拦）
SHADOW_BAD_STATUSES = ("ok", "deliver_failed")

#: 结论档位（顺序即优先级）
LEVEL_STOP = "停"
LEVEL_FIX = "修"
LEVEL_WAIT = "等"
LEVEL_PASS = "过"


def count_signals(text: str, signals: dict[str, tuple[str, ...]] | None = None) -> dict[str, int]:
    """数每个信号在文本里出现了多少次（同一行命中多个字样只算一次）。"""

    table = signals if signals is not None else SIGNALS
    counts = {name: 0 for name in table}
    for line in (text or "").splitlines():
        for name, needles in table.items():
            if any(n and n in line for n in needles):
                counts[name] += 1
    return counts


def exit_status_mix(text: str) -> dict[str, int]:
    """数影子档里每种 ``exit_status`` 出现几次（解析不动就当没出现，不抛）。"""

    mix: dict[str, int] = {}
    for line in (text or "").splitlines():
        if "legacy_exit_shadow" not in line:
            continue
        for part in line.replace("\t", " ").split():
            if part.startswith("exit_status="):
                status = part.split("=", 1)[1].strip().strip('"\'') or "unknown"
                mix[status] = mix.get(status, 0) + 1
                break
    return mix


def shadow_alarms(mix: dict[str, int] | None = None) -> dict[str, int]:
    """影子档里需要人看一眼的两种：``ok``（闸门放行了？）与 ``deliver_failed``（新出口炸了）。"""

    mix = mix or {}
    return {name: int(mix.get(name, 0)) for name in SHADOW_BAD_STATUSES if int(mix.get(name, 0)) > 0}


def scan_logs(paths: list[Path] | None = None, *, log_dir: Path | None = None) -> str:
    """把所有日志读成一段文本（读不动就跳过，不抛）。"""

    if paths is None:
        base = Path(log_dir or LOG_DIR)
        paths = sorted(base.glob("*.log")) if base.is_dir() else []
    chunks: list[str] = []
    for path in paths:
        try:
            chunks.append(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def stale_leases(lock_dir: Path | None = None, *, now: float | None = None,
                 stale_after_s: float = STALE_AFTER_S) -> list[dict]:
    """返回心跳过期的租约（可能卡死）；目录不存在就当没有。"""

    base = Path(lock_dir or LOCK_DIR)
    if not base.is_dir():
        return []
    now = time.time() if now is None else now
    out: list[dict] = []
    for path in sorted(base.glob("*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        heartbeat = state.get("heartbeat_at")
        try:
            age = now - float(heartbeat)
        except (TypeError, ValueError):
            age = float("inf")
        if age > stale_after_s:
            out.append({
                "resource": state.get("resource", path.stem),
                "owner": state.get("owner", ""),
                "pid": state.get("pid"),
                "age_s": round(age, 1),
            })
    return out


def days_left(due: str = DUE_DATE, *, today: date | None = None) -> int:
    """离到期还有几天（已过期为负）。"""

    today = date.today() if today is None else today
    return (date.fromisoformat(due) - today).days


def days_observed(*, since: str | None = None, due: str = DUE_DATE, today: date | None = None) -> float:
    """已经观察了几天（没给起点就按「观察期长度 - 剩余天数」算）。"""

    today = date.today() if today is None else today
    if since:
        return max(0.0, (today - date.fromisoformat(since)).days * 1.0)
    return max(0.0, MIN_DAYS - days_left(due, today=today) * 1.0)


def verdict(counts: dict[str, int], *, days: float, stale: list[dict] | None = None,
            min_days: int = MIN_DAYS, busy_per_day_warn: float = BUSY_PER_DAY_WARN,
            mix: dict[str, int] | None = None) -> tuple[str, str]:
    """给一句结论。顺序即优先级：卡死 > 能力不足 > 影子档异常 > 天数不够 > 通过。"""

    stale = stale or []
    if stale or counts.get("lease_stuck", 0) > 0:
        who = stale[0] if stale else {}
        where = f"（{who.get('resource')} by {who.get('owner') or '?'} pid={who.get('pid')} {who.get('age_s')}s）" if who else ""
        return LEVEL_STOP, f"有租约卡死{where}：先查 data/.locks/*.json 的 pid，taskkill /PID <pid>，别升 L1"
    alarms = shadow_alarms(mix)
    if alarms:
        detail = "、".join(f"{k}×{v}" for k, v in alarms.items())
        return LEVEL_FIX, (
            f"影子档出现「不该出现」的 exit_status（{detail}）：先查为什么放行"
            "（`WECHAT_SEND` 是不是被打开过 / 新出口是不是真的发出去了），别升档"
        )
    if counts.get("blocked_capability", 0) > 0:
        return LEVEL_FIX, f"能力自检挡了 {counts['blocked_capability']} 次：先修探针（dpi/identity 都曾误判过），别升档"
    if days < min_days:
        return LEVEL_WAIT, f"观察期没满：已观察 {days:g} 天，还差 {min_days - days:g} 天"
    busy = counts.get("input_busy", 0) / max(1.0, days)
    if busy > busy_per_day_warn:
        return LEVEL_PASS, f"L0 通过（但 input_busy 每天 {busy:.1f} 次，先看 input_wait_ms p90）"
    return LEVEL_PASS, "L0 通过：可以进 L1（自己给自己发，7 天）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L0 只读观察期体检")
    parser.add_argument("--days", type=float, default=None, help="已观察天数（默认按到期日推算）")
    parser.add_argument("--since", default="", help="观察起点 YYYY-MM-DD")
    parser.add_argument("--due", default=DUE_DATE, help=f"到期日（默认 {DUE_DATE}）")
    parser.add_argument("--log-dir", default="", help="日志目录（默认 logs/）")
    parser.add_argument("--lock-dir", default="", help="租约目录（默认 data/.locks）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    log_dir = Path(args.log_dir) if args.log_dir else LOG_DIR
    lock_dir = Path(args.lock_dir) if args.lock_dir else LOCK_DIR
    text = scan_logs(log_dir=log_dir)
    counts = count_signals(text)
    mix = exit_status_mix(text)
    stale = stale_leases(lock_dir)
    days = args.days if args.days is not None else days_observed(since=args.since, due=args.due)
    level, message = verdict(counts, days=days, stale=stale, mix=mix)
    left = days_left(args.due)

    report = {
        "due": args.due,
        "days_left": left,
        "days_observed": round(days, 1),
        "counts": counts,
        "exit_status": mix,
        "stale": stale,
        "level": level,
        "verdict": message,
        "log_dir": str(log_dir),
        "bytes": len(text),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("L0 只读观察期体检")
    print(f"  日志：{log_dir}（读入 {len(text)} 字符）")
    print(f"  观察：已 {days:g} 天 / 需 {MIN_DAYS} 天；到期 {args.due}（还剩 {left} 天）")
    print("  信号：")
    for name, value in counts.items():
        print(f"    {name:22s} {value}")
    if mix:
        print("  影子档 exit_status：")
        for name, value in sorted(mix.items(), key=lambda kv: -kv[1]):
            flag = "  ← 要看" if name in SHADOW_BAD_STATUSES else ""
            print(f"    {name:22s} {value}{flag}")
    print(f"  残留租约（心跳 > {STALE_AFTER_S:g}s）：{len(stale)}")
    for row in stale:
        print(f"    - {row['resource']} by {row['owner'] or '?'} pid={row['pid']} {row['age_s']}s")
    print(f"  结论【{level}】：{message}")
    if left < 0:
        print(f"  ⚠ 已经过期 {-left} 天——该看了（跑一次这条命令就是看）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
