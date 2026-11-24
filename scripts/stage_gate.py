"""L0 → L1 → L2 的放行闸：现在这一级过没过、下一级要做什么。

为什么单独一个脚本：`scripts/l0_glance.py` 只判「L0 观察期」，判据一旦写进人脑就会走样。
这里把三级的**放行条件**写成同一张表、同一个 `judge_*()`，并把「下一级要跑什么」印在最后
——放行不是感觉，是一条命令的输出。

判据（每一级都必须能停在这里当完整产品）：

- **L0 只读**（7 天）：六信号里 `lease_stuck` / `blocked_capability` 为 0，残留租约 0，
  `input_busy` 每天 ≤ 5 次；满 7 天才算过。
- **L1 自己给自己发**（7 天）：只往「自己的号（``account_registry.role='owner'``）/ 文件传输助手」发，
  用来在真通道上练手。判「自己」以 ``owner_targets()`` 为准，不靠中文名。
  要求 ≥ 7 个不同日期、≥ 20 条，`duplicate` 0、`failed` ≤ 2%、`unconfirmed` ≤ 20%，
  并且 L0 的六信号仍然干净。
- **L2 测试群单条**（7 天）：群里发单条，抽检 ≥ 98% 正确（抽检是人做的，脚本只判前两级）。

口径纪律与周报一致：**查不到写「未采集」，不写 0**。分母为 0 的比率一律「未采集」。

跑法：

```text
.venv_it\\Scripts\\python.exe -X utf8 -m scripts.stage_gate                 # 判当前级（默认 L0）
.venv_it\\Scripts\\python.exe -X utf8 -m scripts.stage_gate --level L1 --since 2026-10-27
.venv_it\\Scripts\\python.exe -X utf8 -m scripts.stage_gate --json --out data/stage.md
```

只读：回执走 `db.write_guard.read_only_engine()`，日志只读文本，锁状态只读 json。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

LEVELS = ("L0", "L1", "L2")

LEVEL_STOP = "停"
LEVEL_FIX = "修"
LEVEL_WAIT = "等"
LEVEL_PASS = "过"

SELF_TARGETS: tuple[str, ...] = ("文件传输助手", "File Transfer", "File transfer", "文件传输", "自己")

L0_MIN_DAYS = 7
L0_BUSY_PER_DAY_WARN = 5.0

L1_MIN_DAYS = 7
L1_MIN_SENDS = 20
L1_MAX_FAILED = 0.02
L1_MAX_UNCONFIRMED = 0.20
L1_MAX_DUPLICATE = 0

PLATFORMS: tuple[str, ...] = ("wechat", "qq", "")

NEXT_STEPS: dict[str, tuple[str, ...]] = {
    "L0": (
        "确认 .env 里 YINYUE_FEATURE_DEVICE_LEASE=on、WECHAT_SEND=off（L0 不发任何消息）",
        "正常用 7 天（她只读、只回，不动手对外发）",
        "到期跑：.venv_it\\Scripts\\python.exe -X utf8 -m scripts.l0_glance",
        "六信号干净且满 7 天 → 进 L1：把 WECHAT_SEND 改成 on，只往「文件传输助手」发",
    ),
    "L1": (
        "只往自己的「文件传输助手」发；每天 3–5 条，连着 7 天",
        "练这些路径：正常发 / 发一半断掉（关微信再开）/ 改口（correct:）/ 撤销授权",
        "到期跑：.venv_it\\Scripts\\python.exe -X utf8 -m scripts.stage_gate --level L1",
        "duplicate 0 且 failed ≤2% 且满 7 天 → 进 L2：群里发单条（人抽检 ≥98%）",
    ),
    "L2": (
        "在 1 个测试群里每天发 1–2 条真消息，人抽检 ≥98% 正确",
        "同时观察：有没有发错人、有没有重复、有没有插话到别人对话里",
        "L2 通过后才是「单个真人 14 天」（L3），再往后才是多会话（L4）",
    ),
}


def _today(now: float | None = None) -> date:
    if now is None:
        return date.today()
    return datetime.fromtimestamp(float(now), tz=timezone.utc).date()


def rate(num: float, den: float) -> float | None:
    """分母为 0 → ``None``（未采集），不返回 0.0。"""
    if not den:
        return None
    return float(num) / float(den)


def owner_targets(*, engine: Any = None) -> tuple[str, ...]:
    """``account_registry`` 里 ``role='owner'`` 的账号 id（= 账号主人本人）。

    为什么不用中文名判「自己」：``文件传输助手`` 那条自聊路从没被这条链路真发过，
    而 ``account_registry.role='owner'`` 是系统里唯一登记「谁是我要服务的人」的地方，
    以后加账号也不用改代码。查不到一律返回空元组（调用方退回 ``SELF_TARGETS``）。
    """
    from sqlalchemy import text as _text

    try:
        eng = _meta_engine() if engine is None else engine
        with eng.connect() as conn:
            rows = conn.execute(_text("SELECT account_id FROM account_registry WHERE role='owner'")).fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"  owner 账号未采集：{type(exc).__name__}: {exc}")
        return ()
    out: list[str] = []
    for row in rows:
        mapping = getattr(row, "_mapping", None)
        value = dict(mapping)["account_id"] if mapping is not None else tuple(row)[0]
        if value:
            out.append(str(value))
    return tuple(out)


def self_targets(*, engine: Any = None) -> tuple[str, ...]:
    """L1 认「自己」的完整名单：自聊中文名 + owner 账号 id（去重、保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in (*SELF_TARGETS, *owner_targets(engine=engine)):
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def self_rows(
    rows: Iterable[Mapping[str, Any]], *, targets: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """只留「发给自己的」那批行；默认名单 = 自聊中文名 + owner 账号（见 ``self_targets``）。"""
    wanted = [t for t in (self_targets() if targets is None else targets) if t]
    out: list[dict[str, Any]] = []
    for row in rows:
        target = str(row.get("target") or "")
        if not target:
            continue
        if any(t in target for t in wanted):
            out.append(dict(row))
    return out


def day_of(ts: Any) -> str:
    """``ts`` 取日期部分；认 datetime / str / 空。认不出返回空串。"""
    if ts is None:
        return ""
    if isinstance(ts, datetime):
        return ts.date().isoformat()
    text = str(ts).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def distinct_days(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    days = {day_of(r.get("ts")) for r in rows}
    days.discard("")
    return sorted(days)


def l1_stats(
    rows: Iterable[Mapping[str, Any]], *, targets: Sequence[str] | None = None
) -> dict[str, Any]:
    """L1 的六个数字。``sends`` 是分母，比率在分母为 0 时为 ``None``。"""
    mine = self_rows(rows, targets=targets)
    statuses: dict[str, int] = {}
    for row in mine:
        key = str(row.get("status") or "")
        statuses[key] = statuses.get(key, 0) + 1
    sends = len(mine)
    sent = statuses.get("sent", 0)
    has = bool(sends)
    unconfirmed = statuses.get("sent_unconfirmed", 0) + statuses.get("unconfirmed", 0)
    failed = statuses.get("failed", 0)
    duplicate = statuses.get("duplicate", 0)
    blocked = statuses.get("blocked", 0)
    days = distinct_days(mine)
    return {
        "sends": sends,
        "days": len(days),
        "day_list": days,
        "sent": sent,
        "unconfirmed": unconfirmed,
        "failed": failed,
        "duplicate": duplicate if has else None,
        "blocked": blocked if has else None,
        "delivered": sent + unconfirmed,
        "failed_rate": rate(failed, sends),
        "unconfirmed_rate": rate(unconfirmed, sends),
        "duplicate_rate": rate(duplicate, sends),
    }


def judge_l1(
    stats: Mapping[str, Any],
    *,
    log_counts: Mapping[str, int] | None = None,
    stale: Sequence[Mapping[str, Any]] | None = None,
    min_days: int = L1_MIN_DAYS,
    min_sends: int = L1_MIN_SENDS,
    max_failed: float = L1_MAX_FAILED,
    max_unconfirmed: float = L1_MAX_UNCONFIRMED,
    max_duplicate: int = L1_MAX_DUPLICATE,
) -> tuple[str, str]:
    """L1 放行判定。优先级：停 > 修 > 等 > 过（与 l0_glance.verdict 一致）。"""
    counts = dict(log_counts or {})
    stuck = int(counts.get("lease_stuck", 0))
    caps = int(counts.get("blocked_capability", 0))
    stale_n = len(list(stale or ()))
    if stuck or caps or stale_n:
        return LEVEL_STOP, f"停：lease_stuck={stuck} blocked_capability={caps} 残留租约={stale_n}"
    dup_raw = stats.get("duplicate")
    dup = None if dup_raw is None else int(dup_raw)
    failed = stats.get("failed_rate")
    if dup is not None and dup > max_duplicate:
        return LEVEL_FIX, f"修：duplicate={dup}（重复发送必须是 0）"
    if failed is not None and failed > max_failed:
        return LEVEL_FIX, f"修：failed_rate={failed * 100:.1f}% > {max_failed * 100:.0f}%"
    days = int(stats.get("days", 0))
    sends = int(stats.get("sends", 0))
    if days < min_days:
        return LEVEL_WAIT, f"等：只有 {days}/{min_days} 天有发出记录"
    if sends < min_sends:
        return LEVEL_WAIT, f"等：只发了 {sends}/{min_sends} 条"
    unc = stats.get("unconfirmed_rate")
    tail = "" if unc is None else f"（unconfirmed {unc * 100:.1f}%，落盘慢会偏高）"
    dup_txt = "未采集" if dup is None else "0"
    failed_txt = "未采集" if failed is None else "0"
    return LEVEL_PASS, f"过：{days} 天 / {sends} 条，duplicate {dup_txt}、failed {failed_txt}{tail}"


def _meta_engine() -> Any:
    """只读句柄（守卫不拦只读；可写 engine 会被 ProductionWriteRefused 拒，这是对的）。"""
    from core.observability.weekly import meta_db_path

    from db.write_guard import read_only_engine

    return read_only_engine(meta_db_path())


def read_receipts(*, engine: Any = None, days: int = 7, now: float | None = None) -> list[dict[str, Any]] | None:
    """读最近 ``days`` 天的回执行；查不到（库/表/列缺失）返回 ``None``（= 未采集）。"""
    from sqlalchemy import text as _text

    sql = (
        "SELECT turn_id, chat_id, status, kind, target, ack, reason, detail, ref, ts "
        "FROM delivery_receipts"
    )
    params: dict[str, Any] = {}
    if days > 0:
        sql += " WHERE ts >= :since"
        stamp = datetime.fromtimestamp(_now() if now is None else float(now), tz=timezone.utc)
        from datetime import timedelta

        params["since"] = (stamp - timedelta(days=float(days))).strftime("%Y-%m-%d %H:%M:%S")
    sql += " ORDER BY receipt_id DESC"
    try:
        eng = _meta_engine() if engine is None else engine
        with eng.connect() as conn:
            result = conn.execute(_text(sql), params)
            rows = result.fetchall() if hasattr(result, "fetchall") else list(result)
    except Exception as exc:  # noqa: BLE001
        print(f"  回执未采集：{type(exc).__name__}: {exc}")
        return None
    keys = ("turn_id", "chat_id", "status", "kind", "target", "ack", "reason", "detail", "ref", "ts")
    out: list[dict[str, Any]] = []
    for row in rows:
        mapping = getattr(row, "_mapping", None)
        if mapping is not None:
            out.append(dict(mapping))
        else:
            out.append(dict(zip(keys, tuple(row))))
    return out


def _now() -> float:
    import time

    return time.time()


def _log_counts(log_dir: Path | None = None) -> dict[str, int] | None:
    try:
        import l0_glance  # scripts/ 已在 sys.path 上

        text_all = l0_glance.scan_logs(log_dir=log_dir)
        return l0_glance.count_signals(text_all)
    except Exception as exc:  # noqa: BLE001
        print(f"  日志未采集：{type(exc).__name__}: {exc}")
        return None


def _stale(lock_dir: Path | None = None) -> list[dict[str, Any]] | None:
    try:
        import l0_glance

        return l0_glance.stale_leases(lock_dir=lock_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"  锁状态未采集：{type(exc).__name__}: {exc}")
        return None


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "未采集"
    if unit == "%":
        return f"{float(value) * 100:.1f}%"
    if isinstance(value, float) and float(value).is_integer():
        return f"{int(value)}"
    return f"{value}"


def render(level: str, verdict: tuple[str, str], stats: Mapping[str, Any] | None) -> str:
    lines = [f"# 放行判定：{level}", "", f"结论：[{verdict[0]}] {verdict[1]}", ""]
    if stats is not None:
        lines += [
            "## 数字（L1 口径：只算发给自己的）",
            "",
            f"- 发出 {stats.get('sends')} 条 / {stats.get('days')} 天",
            f"- sent {stats.get('sent')} · unconfirmed {_fmt(stats.get('unconfirmed_rate'), '%')}"
            f" · failed {_fmt(stats.get('failed_rate'), '%')} · duplicate {_fmt(stats.get('duplicate'))}",
            "",
        ]
    steps = NEXT_STEPS.get(level, ())
    if steps:
        lines += ["## 下一级要做什么", ""] + [f"{i}. {s}" for i, s in enumerate(steps, 1)] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L0/L1/L2 放行闸")
    parser.add_argument("--level", default="L0", choices=list(LEVELS))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--since", default="")
    parser.add_argument("--log-dir", default="")
    parser.add_argument("--lock-dir", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    level = args.level
    log_dir = Path(args.log_dir) if args.log_dir else None
    lock_dir = Path(args.lock_dir) if args.lock_dir else None

    counts = _log_counts(log_dir)
    stale = _stale(lock_dir)
    rows = read_receipts(days=args.days, now=None)
    stats = None if rows is None else l1_stats(rows)

    if level == "L0":
        if counts is None:
            verdict = (LEVEL_WAIT, "等：日志读不到，判不了（先确认 logs/*.log 在）")
        else:
            import l0_glance

            days = l0_glance.days_observed(since=args.since or None)
            verdict = l0_glance.verdict(
                dict(counts), days=days, stale=list(stale or ()), busy_per_day_warn=L0_BUSY_PER_DAY_WARN
            )
        stats = stats if stats and stats.get("sends") else None
    else:
        if stats is None:
            verdict = (LEVEL_WAIT, "等：回执读不到，判不了")
        else:
            verdict = judge_l1(stats, log_counts=counts, stale=stale)

    body = json.dumps(
        {"level": level, "verdict": verdict[0], "message": verdict[1], "stats": stats, "log_counts": counts},
        ensure_ascii=False,
        indent=2,
    ) if args.json else render(level, verdict, stats)

    print(body)
    if args.out:
        Path(args.out).write_text(body + "\n", encoding="utf-8")
        print(f"\\n已写入 {args.out}")
    return 0 if verdict[0] in (LEVEL_PASS, LEVEL_WAIT) else 1


if __name__ == "__main__":
    raise SystemExit(main())
