"""只读诊断：一次同步轮为什么能跑 >1 小时（threads 焦点判定热路径耗时归因）。

背景（task-18/task-19 的现场）：
    ``logs/scheduler.stop`` 写下后调度器 5 分钟没退 —— 主线程卡在
    ``scheduler/main.py`` 的 ``_run_async(run_sync_pipeline())``，而 asyncio 线程正跑在
    ``core/routing/router.py::_prefetch_group_focus`` → ``core.context.threads.prefetch_focus``
    → ``compute_focus`` → ``calculate_focus`` → ``_calculate`` → ``_rollup`` → ``_beyond_gap``
    → ``core/timing.get()``。哨兵检查在主循环里，所以同步轮一钉死，优雅停止通道就失效。

本脚本干什么（**只读、离线、不调 LLM、不写任何库**）：
    1) 取最近 ``--days`` 天里消息最多的会话（或 ``--chat`` 指定），按
       ``core.context.chat_window.load_messages`` 的同一口径读窗口行；
    2) 在窗口的若干前缀长度上，量 ``calculate_focus`` 一次的真实耗时，以及
       ``_rollup`` / ``_beyond_gap`` / ``_timing_qa_gap`` / ``timing.get`` / ``_env_float``
       / ``_attribution_of`` / ``_anchor_of_row`` 的调用次数与累计耗时；
    3) 给「一次焦点判定 = 多少行 × 多少簇 × 多少次 env 读」的机械解释，并外推到
       「一次同步轮能跑几条消息」。

跑法（推荐隔离根；脚本自身也是 read-only engine，不会写生产库）：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\run_isolated.ps1 scripts\\diagnose_sync_round.py
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\run_isolated.ps1 scripts\\diagnose_sync_round.py --sizes 200,600,1200,2400,4800 --json .test_tmp\\task18\\diag.json

产物：stdout 表格 + ``--json`` 指定的文件（默认 ``.test_tmp/task18/diagnose_sync_round.json``）。
写进文档的读数：docs/decisions/穷举整改_同步轮卡死.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.context.threads as threads  # noqa: E402
import core.timing as timing  # noqa: E402
from core.bootstrap import get_meta_db_path  # noqa: E402
from db.write_guard import read_only_engine  # noqa: E402

EVENT_TYPES = ("message", "question", "reply", "request")


def _busiest_chat(days: int) -> tuple[str, int]:
    """最近 days 天消息最多的 chat_id（只读）。"""
    from sqlalchemy import text as sql

    since = datetime.now() - timedelta(days=max(1, int(days)))
    engine = read_only_engine(get_meta_db_path())
    with engine.connect() as conn:
        row = conn.execute(
            sql(
                "select chat_id, count(*) as n from terminal_events "
                "where occurred_at >= :since and event_type in ('message','question','reply','request') "
                "and chat_id is not null and chat_id != '' "
                "group by chat_id order by n desc limit 1"
            ),
            {"since": since.replace(tzinfo=None)},
        ).mappings().first()
    if not row:
        return "", 0
    return str(row["chat_id"]), int(row["n"])


def _load_window(chat_id: str, days: int) -> list[Any]:
    """与 core.context.chat_window.load_messages 同一 SQL，但走 read_only_engine。"""
    from sqlalchemy import text as sql

    from core.context.chat_window import _dedupe_self_echoes, messages_from_rows

    since = datetime.now() - timedelta(days=max(1, int(days)))
    engine = read_only_engine(get_meta_db_path())
    with engine.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                sql(
                    "select id, event_id, occurred_at, event_type, actor_person_id, content, "
                    "parent_event_id, recipient_person_ids_json, metadata_json "
                    "from terminal_events "
                    "where chat_id = :chat_id and occurred_at >= :since "
                    "and event_type in ('message','question','reply','request') "
                    "order by occurred_at, sequence"
                ),
                {"chat_id": str(chat_id), "since": since.replace(tzinfo=None)},
            ).mappings()
        ]
        names: dict[str, str] = {}
        for r in conn.execute(
            sql(
                "select pi.identity_value, pe.display_name "
                "from person_identities pi "
                "join person_entities pe on pe.person_id = pi.person_id "
                "where pe.display_name is not null and pe.display_name != ''"
            )
        ).mappings():
            names.setdefault(str(r["identity_value"]), str(r["display_name"]))
    for row in rows:
        if isinstance(row.get("occurred_at"), str):
            row["occurred_at"] = datetime.fromisoformat(row["occurred_at"])
    return list(_dedupe_self_echoes(messages_from_rows(rows, names)))

# ------------------------------------------------------------------ counters
class _Stats:
    """一次测量窗口内的调用次数与累计秒数。"""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.secs: dict[str, float] = {}
        self.clusters: list[int] = []

    def add(self, name: str, dt: float) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        self.secs[name] = self.secs.get(name, 0.0) + dt


@contextmanager
def _instrument() -> Iterable[_Stats]:
    """给热路径函数套计数器（只在本进程内改属性，退出时原样还回）。"""
    stats = _Stats()
    targets: list[tuple[Any, str]] = [
        (timing, "_env_float"),
        (timing, "get"),
        (threads, "_timing_qa_gap"),
        (threads, "_beyond_gap"),
        (threads, "_attribution_of"),
        (threads, "_anchor_of_row"),
        (threads, "_rollup"),
        (threads, "_calculate"),
    ]
    saved: list[tuple[Any, str, Any]] = []
    for mod, name in targets:
        orig = getattr(mod, name, None)
        if orig is None:
            continue

        def _make(orig: Any = orig, name: str = name) -> Callable[..., Any]:
            def wrapper(*a: Any, **k: Any) -> Any:
                t0 = time.perf_counter()
                out = orig(*a, **k)
                stats.add(name, time.perf_counter() - t0)
                if name == "_rollup" and isinstance(out, tuple) and out:
                    try:
                        stats.clusters.append(len(out[0]))
                    except TypeError:
                        pass
                return out

            return wrapper

        setattr(mod, name, _make())
        saved.append((mod, name, orig))
    try:
        yield stats
    finally:
        for mod, name, orig in saved:
            setattr(mod, name, orig)


def _timing_get_cost(samples: int = 2000) -> dict[str, Any]:
    """单独量 ``core.timing.get()``：每次读几个 env。"""
    n = 0

    def _count(*a: Any, **k: Any) -> Any:
        nonlocal n
        n += 1
        return _env_orig(*a, **k)

    _env_orig = timing._env_float
    timing._env_float = _count  # type: ignore[assignment]
    try:
        t0 = time.perf_counter()
        for _ in range(samples):
            timing.get()
        dt = time.perf_counter() - t0
    finally:
        timing._env_float = _env_orig  # type: ignore[assignment]
    return {
        "samples": samples,
        "seconds": dt,
        "us_per_call": dt / samples * 1e6,
        "env_reads_per_call": n / samples,
    }


def _measure(rows: Sequence[Any], chat_id: str, size: int) -> dict[str, Any] | None:
    window = list(rows[-size:]) if size > 0 else []
    if len(window) < 2:
        return None
    focus_input = threads.FocusInput(
        chat_id=str(chat_id),
        platform="wechat",
        account_id="diag",
        current=window[-1],
        window_rows=tuple(window[:-1]),
        prev_focus=None,
        now=None,
    )
    with _instrument() as stats:
        t0 = time.perf_counter()
        result = threads.calculate_focus(focus_input)
        total = time.perf_counter() - t0
    reach = _reach_len(window)
    return {
        "size": len(window),
        "seconds": total,
        "ms_per_row": total / len(window) * 1e3,
        "rollup_clusters": max(stats.clusters) if stats.clusters else -1,
        "calls": dict(sorted(stats.calls.items())),
        "secs": {k: round(v, 6) for k, v in sorted(stats.secs.items())},
        "degraded": bool(getattr(result, "degraded", False)),
        "reach_chars": reach,
    }


def _reach_len(window: Sequence[Any]) -> int:
    """窗口里被 threads 真正取用的字符总量（粗略；只用于解释量级）。"""
    total = 0
    for row in window:
        total += len(str(getattr(row, "content", "") or ""))
    return total


def _fmt_table(rows: list[dict[str, Any]]) -> str:
    head = "size   secs    ms/row  clusters  beyond_gap  timing.get  env_reads  rollup_ms  calc_ms"
    lines = [head]
    for r in rows:
        c = r["calls"]
        s = r["secs"]
        lines.append(
            "{size:>5}  {secs:>7.2f} {msrow:>7.3f} {cl:>9} {bg:>11} {tg:>11} {er:>10} {rm:>9.1f} {cm:>8.1f}".format(
                size=r["size"],
                secs=r["seconds"],
                msrow=r["ms_per_row"],
                cl=r["rollup_clusters"],
                bg=c.get("_beyond_gap", 0),
                tg=c.get("get", 0),
                er=c.get("_env_float", 0),
                rm=s.get("_rollup", 0.0) * 1e3,
                cm=s.get("_calculate", 0.0) * 1e3,
            )
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="只读诊断：同步轮焦点判定热路径耗时")
    ap.add_argument("--chat", default="", help="会话 id（默认取最近 --days 天消息最多的）")
    ap.add_argument("--days", type=int, default=7, help="窗口回看天数（与 chat_window 默认一致）")
    ap.add_argument("--sizes", default="200,600,1200,2400,4800", help="窗口前缀长度，逗号分隔")
    ap.add_argument("--json", default=str(ROOT / ".test_tmp" / "task18" / "diagnose_sync_round.json"))
    args = ap.parse_args()

    sizes = [int(x) for x in str(args.sizes).split(",") if x.strip()]
    chat_id = args.chat.strip()
    if not chat_id:
        chat_id, n = _busiest_chat(args.days)
        print("[pick] 最近 %d 天消息最多会话：%s（%d 条）" % (args.days, chat_id, n))
    if not chat_id:
        print("[fail] 没找到会话；用 --chat 指定一个")
        return 2

    rows = _load_window(chat_id, args.days)
    print("[load] chat=%s 窗口行=%d（days=%d，read_only_engine）" % (chat_id, len(rows), args.days))
    if len(rows) < 2:
        print("[fail] 窗口行不足两行，无法判定")
        return 2

    tg = _timing_get_cost()
    print(
        "[timing] get() = %.1f us/次，每次读 %.1f 个 env（模块 docstring：故意不缓存）"
        % (tg["us_per_call"], tg["env_reads_per_call"])
    )

    measured: list[dict[str, Any]] = []
    for size in sizes:
        m = _measure(rows, chat_id, size)
        if not m:
            print("[skip] size=%d（窗口不足）" % size)
            continue
        measured.append(m)
        print(
            "[size %5d] %.2fs  %s" % (m["size"], m["seconds"], json.dumps(m["calls"], ensure_ascii=False))
        )

    print("")
    print(_fmt_table(measured))

    payload: dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "chat_id": chat_id,
        "days": args.days,
        "window_rows": len(rows),
        "timing_get": tg,
        "measured": measured,
    }
    if measured:
        biggest = measured[-1]
        per_turn = float(biggest["seconds"])
        payload["extrapolate"] = {
            "window_rows": biggest["size"],
            "seconds_per_turn": per_turn,
            "turns_per_hour": (3600.0 / per_turn) if per_turn > 0 else None,
            "note": "同步轮每条群消息回合各算一次焦点（prefetch_focus::compute_focus）",
        }
        if per_turn > 0:
            print(
                "\n[extrapolate] 一次焦点判定（窗口 %d 行）= %.2fs ⇒ 单进程 1 小时最多 %.1f 次；"
                "一次同步轮若有 K 条群消息，K 超过 %.0f 就会 >1 小时。"
                % (biggest["size"], per_turn, 3600.0 / per_turn, 3600.0 / per_turn)
            )

    out_path = Path(args.json)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[json] %s" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
