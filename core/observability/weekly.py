"""周报：把量具算成一张纸（U-29）。

口径全部写死在这里，且**分母缺失时写「未采集」而不是填 0**：

| 指标 | 算式 | 分母 |
|---|---|---|
| 弃权率 | `abstain + skip` / 回合数 | `turn_trace` 这一周的行数 |
| 指回率（basis 覆盖率） | `basis != '[]'` / 回合数 | 同上 |
| 重复发送率 | `duplicate` / `sent` | `delivery_receipts` 这一周 |
| 越权被挡 | `blocked` / 全部回执 | 同上 |
| 锁等待 p90 | `input_wait_ms` 样本 p90 | 进程内量具（重启即清零） |
| 预算/回合中止 | `job_events.kind in (stopped, handed_off)` | `job_events` 这一周 |

「未采集」与「0」是两件事：前者是**没测**，后者是**测了确实是零**。报表里必须能分清。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence
from safelog import warn

log = logging.getLogger(__name__)

DAY_S = 86400.0
DEFAULT_DAYS = 7

#: 判的「不说话」类决定——弃权率的分子。
QUIET_DECISIONS = ("abstain", "skip", "silent")


@dataclass(frozen=True)
class Metric:
    """一个指标。``value is None`` = 未采集（不是 0）。"""

    name: str
    value: float | None
    unit: str = ""
    basis: str = ""
    note: str = ""

    @property
    def text(self) -> str:
        if self.value is None:
            return "未采集"
        if self.unit == "%":
            return f"{self.value * 100:.1f}%"
        if float(self.value).is_integer():
            return f"{int(self.value)}"
        return f"{self.value:.1f}"


@dataclass(frozen=True)
class WeeklyReport:
    days: int = DEFAULT_DAYS
    at: str = ""
    metrics: tuple[Metric, ...] = ()
    notes: tuple[str, ...] = ()

    def metric(self, name: str) -> Metric | None:
        for m in self.metrics:
            if m.name == name:
                return m
        return None

    def value(self, name: str) -> float | None:
        got = self.metric(name)
        return None if got is None else got.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "at": self.at,
            "metrics": [
                {"name": m.name, "value": m.value, "unit": m.unit, "basis": m.basis, "note": m.note}
                for m in self.metrics
            ],
            "notes": list(self.notes),
        }

    def markdown(self) -> str:
        groups = (
            ("一、这一周发生了什么", ("turns", "decisions.respond", "decisions.ask")),
            ("二、她说不说、指不指得回", ("abstain_rate", "basis_coverage")),
            ("三、发得对不对", ("receipts.sent", "receipts.unconfirmed", "receipts.failed",
                                "blocked_rate", "duplicate_rate",
                                # §29：说发出去了，到底有没有凭据（原来落在「其它」里）
                                "truth.verified", "truth.unverified", "truth.rewritten", "truth.rate",
                                # §29.5：有凭据 ≠ 是这句话（反向核对）
                                "truth.reverse_checked", "truth.reverse_bad",
                                # §45：被自己拦下来的话，被哪道闸拦的（旁证 logs/blocked.jsonl）
                                "blocked.gate_total", "blocked.gate_allowlist",
                                "blocked.gate_content", "blocked.gate_outbound",
                                "blocked.gate_spont")),
            ("四、手有没有被卡住", ("input_wait_p90_ms", "lease_stuck", "blocked_capability", "dirty_composer_blocked")),
            ("五、事有没有办完", ("jobs.done", "jobs.awaiting", "jobs.stopped")),
            ("六、她是怎么停的", ("restarts.total", "restarts.bad", "restarts.all_stop",
                               "restarts.engine")),
        )
        lines = [f"# 周报（最近 {self.days} 天）", "", f"生成时间：{self.at}", ""]
        shown: set[str] = set()
        for title, names in groups:
            rows = [m for m in self.metrics if m.name in names]
            if not rows:
                continue
            lines.extend([f"## {title}", "", "| 指标 | 值 | 分母/口径 |", "|---|---|---|"])
            for m in rows:
                shown.add(m.name)
                basis = m.basis or m.note or "—"
                lines.append(f"| {m.name} | {m.text} | {basis} |")
            lines.append("")
        rest = [m for m in self.metrics if m.name not in shown]
        if rest:
            lines.extend(["## 七、其它", "", "| 指标 | 值 | 分母/口径 |", "|---|---|---|"])
            for m in rest:
                lines.append(f"| {m.name} | {m.text} | {m.basis or m.note or '—'} |")
            lines.append("")
        if self.notes:
            lines.extend(["## 口径与未采集", ""])
            lines.extend(f"- {n}" for n in self.notes)
            lines.append("")
        return "\n".join(lines)


def _now_iso(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() if now is None else now))


def meta_db_path() -> str:
    """报表读的库：``META_DB_PATH`` 优先，否则 ``data/yinyue_meta.db``。"""
    import os

    return os.environ.get("META_DB_PATH") or "data/yinyue_meta.db"


def _meta_engine() -> Any:
    """**只读**句柄。

    不能用 ``get_meta_repo().engine``：那是可写句柄，生产路径上会被 write_guard 拒
    （``ProductionWriteRefused: 拒绝写入……不给 meta 库的可写 engine``）——报表只需要读，
    所以走 ``db.write_guard.read_only_engine``，守卫不拦只读。
    """
    try:
        from db.write_guard import read_only_engine

        return read_only_engine(meta_db_path())
    except Exception:  # noqa: BLE001 - 没有库就是「未采集」
        warn("core/observability/weekly.py:137 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def _rows(engine: Any, sql: str, params: Mapping[str, Any]) -> list[tuple[Any, ...]] | None:
    """查得到就返回行（可能是空表），**查不到返回 None**——两者在报表里必须分开。"""
    if engine is None:
        return None
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(text(sql), dict(params))]
    except Exception as exc:  # noqa: BLE001 - 表不在 / 列变了都算未采集
        log.info("weekly_query_failed sql=%s error=%r", sql.split()[2:4], exc)
        return None


def _turn_metrics(*, engine: Any, since: float) -> list[Metric]:
    rows = _rows(
        engine,
        "SELECT decision, COUNT(*) FROM turn_trace WHERE ts >= :since GROUP BY decision",
        {"since": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))},
    )
    if rows is None:
        return [
            Metric("turns", None, "回合", "turn_trace 这一周", "查不到（库没开 / 表不在）"),
            Metric("abstain_rate", None, "%", "（abstain+skip）/回合", "分母缺失"),
            Metric("basis_coverage", None, "%", "basis≠'[]' /回合", "分母缺失"),
        ]
    counts = {str(r[0]): int(r[1]) for r in rows}
    total = sum(counts.values())
    if total == 0:
        return [
            Metric("turns", 0.0, "回合", "turn_trace 这一周", "这一周 0 条（TURN_TRACE 可能没开）"),
            Metric("abstain_rate", None, "%", "（abstain+skip）/回合", "这一周没有回合"),
            Metric("basis_coverage", None, "%", "basis≠'[]' /回合", "这一周没有回合"),
        ]
    quiet = sum(v for k, v in counts.items() if k in QUIET_DECISIONS)
    covered = _rows(
        engine,
        "SELECT COUNT(*) FROM turn_trace WHERE ts >= :since AND basis != '[]'",
        {"since": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))},
    )
    with_basis = int(covered[0][0]) if covered else 0
    out = [
        Metric("turns", float(total), "回合", "turn_trace 这一周"),
        Metric("abstain_rate", quiet / total if total else None, "%", f"（{'/'.join(QUIET_DECISIONS)}）{quiet}/{total}"),
        Metric("basis_coverage", with_basis / total if total else None, "%", f"有原文的 {with_basis}/{total}"),
    ]
    for name, value in sorted(counts.items()):
        out.append(Metric(f"decisions.{name}", float(value), "回合", f"占 {value}/{total}"))
    return out


def _receipt_metrics(*, engine: Any, since: float) -> list[Metric]:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
    rows = _rows(
        engine,
        "SELECT status, COUNT(*) FROM delivery_receipts WHERE ts >= :since GROUP BY status",
        {"since": stamp},
    )
    if rows is None:
        return [
            Metric("receipts.sent", None, "条", "delivery_receipts 这一周", "查不到（库没开 / 表不在）"),
            Metric("blocked_rate", None, "%", "blocked/全部回执", "分母缺失"),
            Metric("duplicate_rate", None, "%", "duplicate/sent", "分母缺失"),
        ]
    counts = {str(r[0]): int(r[1]) for r in rows}
    total = sum(counts.values())
    if total == 0:
        return [
            Metric("receipts.sent", 0.0, "条", "delivery_receipts 这一周", "这一周 0 条"),
            Metric("blocked_rate", None, "%", "blocked/全部回执", "这一周没有回执"),
            Metric("duplicate_rate", None, "%", "duplicate/sent", "这一周没有回执"),
        ]
    sent = counts.get("sent", 0)
    dup = counts.get("duplicate", 0)
    out = [Metric(f"receipts.{k}", float(v), "条", f"占 {v}/{total}") for k, v in sorted(counts.items())]
    out.append(Metric("blocked_rate", counts.get("blocked", 0) / total if total else None, "%",
                      f"blocked={counts.get('blocked', 0)}/{total}"))
    out.append(Metric("duplicate_rate", dup / sent if sent else None, "%",
                      f"duplicate={dup}/sent={sent}"))
    return out


def _job_metrics(*, engine: Any, since: float) -> list[Metric]:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
    rows = _rows(
        engine,
        "SELECT kind, COUNT(*) FROM job_events WHERE ts >= :since GROUP BY kind",
        {"since": stamp},
    )
    if rows is None:
        return [
            Metric("jobs.done", None, "件", "job_events 这一周", "查不到（库没开 / 表不在）"),
            Metric("jobs.awaiting", None, "件", "同上", "分母缺失"),
            Metric("jobs.stopped", None, "件", "budget/turns 停因", "分母缺失"),
        ]
    counts = {str(r[0]): int(r[1]) for r in rows}
    return [
        Metric("jobs.done", float(counts.get("done", 0)), "件", f"done/{sum(counts.values())} 个事件"),
        Metric("jobs.awaiting", float(counts.get("awaiting", 0)), "件", "在等人"),
        Metric("jobs.stopped", float(counts.get("handed_off", 0)), "件", "交接出去"),
    ]


def _process_metrics(*, metrics: Mapping[str, Any] | None) -> list[Metric]:
    if metrics is None:
        return [
            Metric("input_wait_p90_ms", None, "ms", "进程内量具", "本次进程没采"),
            Metric("lease_stuck", None, "次", "同上", "本次进程没采"),
            Metric("blocked_capability", None, "次", "同上", "本次进程没采"),
            Metric("dirty_composer_blocked", None, "次", "同上", "本次进程没采"),
        ]
    p90 = metrics.get("p90") or {}
    counts = metrics.get("counts") or {}
    return [
        Metric("input_wait_p90_ms", float(p90.get("input_wait_ms") or 0.0) or None, "ms", "进程内，重启清零"),
        Metric("lease_stuck", float(counts.get("lease_stuck", 0)), "次", "心跳过 2×TTL"),
        Metric("blocked_capability", float(counts.get("blocked_capability", 0)), "次", "能力自检没过"),
        Metric("dirty_composer_blocked", float(counts.get("dirty_composer_blocked", 0)), "次", "输入框有残留、已拦住"),
    ]


def _receipt_truth_metrics(*, days: int, summary: Any = None) -> list[Metric]:
    """§28：说「发出去了」到底有没有凭据。数据源是 `loop/receipts.py::summary`。

    单独一组指标的理由：`receipts.*` 那组看的是回执（blocked/duplicate），而这一组看的是
    「队列说发了」与「微信里真有这条」对不对得上 —— 前者是过程，后者是结果。
    """
    got: dict = {}
    try:
        fn = summary
        if fn is None:
            from loop import receipts as _rc
            fn = _rc.summary
        got = dict(fn(days=float(days)) or {})
    except Exception as exc:  # noqa: BLE001 - 一个数据源坏掉不许把整张周报炸掉
        log.warning(f"weekly_receipt_truth_failed: err={repr(exc)[:120]}")
        got = {}
    if not got or got.get("error"):
        return [
            Metric("truth.verified", None, "条", "队列里说发出去的", "查不到（world.db 没开 / 表不在）"),
            Metric("truth.unverified", None, "条", "sent 但证不出来", "分母缺失"),
            Metric("truth.rewritten", None, "条", "已回写 unverified", "分母缺失"),
            Metric("truth.rate", None, "%", "verified/（verified+unverified+rewritten）", "分母缺失"),
        ]
    claimed = int(got.get("claimed") or 0)
    return [
        Metric("truth.verified", float(got.get("verified") or 0), "条",
               "说发出去 %d 条里真有这条" % claimed),
        Metric("truth.unverified", float(got.get("unverified") or 0), "条", "还写着 sent 却证不出来"),
        Metric("truth.rewritten", float(got.get("rewritten") or 0), "条", "已回写 unverified（2026-11-04 起）"),
        # `%` 单位下 `Metric.text` 自己会 ×100（同 abstain_rate / blocked_rate），这里给 0~1。
        Metric("truth.rate", float(got.get("rate") or 0.0) if claimed else None, "%",
               "verified/（verified+unverified+rewritten）"),
    ]
def _reverse_metrics(*, days: int, reverse: Any = None) -> list[Metric]:
    """§29.5：说「有凭据」的那些，微信里那条消息**真的就是这句话**吗。

    为什么要单独一组：`truth.*` 只问「这个 msg_id 在不在」，不验内容。E-47 那次
    「两条队列行认领同一个 msg_id」就是靠人眼发现的——同一句话挂错行时，三层判据全过。
    这一组是反向核对：把 `messages` 里的正文与会话，和队列行的 `payload.text` / `chat_id`
    对上。数据源是 `loop/receipts.py::reverse_check`（只读）。
    """
    got: dict = {}
    missing = "查不到（world.db 没开 / 表不在）"
    try:
        fn = reverse
        if fn is None:
            from loop import receipts as _rc
            fn = _rc.reverse_check
        got = dict(fn() or {})
    except Exception as exc:  # noqa: BLE001 - 一个数据源坏掉不许把整张周报炸掉
        log.warning(f"weekly_reverse_metrics_failed: err={repr(exc)[:120]}")
        got = {}
    if not got or got.get("error"):
        return [
            Metric("truth.reverse_checked", None, "条", "拿微信里那条对正文与会话", missing),
            Metric("truth.reverse_bad", None, "条", "对不上的（正文 / 会话 / 那条不在）", "分母缺失"),
        ]
    seen = int(got.get("seen") or 0)
    checked = int(got.get("checked") or 0)
    problems = list(got.get("problems") or [])
    kinds: dict = {}
    for p in problems:
        k = str((p or {}).get("kind") or "?")
        kinds[k] = kinds.get(k, 0) + 1
    detail = "、".join("%s %d" % (k, n) for k, n in sorted(kinds.items())) or "无"
    # §42.5 第 2 条：只报「几条」不够 —— 想追还得跑脚本。这里带上前三条的队列号。
    sample = "、".join("#%s %s" % ((p or {}).get("queue_id"), (p or {}).get("kind"))
                      for p in problems[:3])
    if sample:
        detail = "%s（%s%s）" % (detail, sample, "…" if len(problems) > 3 else "")
    return [
        Metric("truth.reverse_checked", float(checked), "条",
               "sent %d 条里能反查 %d 条（另 %d 条还没有 msg_id）" % (seen, checked, seen - checked)),
        # `bad` 为 0 是「查了确实全对」，不是「没查」——没查的时候两个都是 `未采集`。
        Metric("truth.reverse_bad", float(len(problems)), "条", "对不上：%s" % detail),
    ]


def _restart_metrics(*, days: int, summary: Any = None) -> list[Metric]:
    """§33：这一周她（和监管她的人）是怎么停的。数据源是 `core/infra/exitlog.py::summary`。

    单独一组的理由：`receipts.*` 看她说了没、`truth.*` 看话到没到，这一组看**进程还在不在**。
    三件事一起看才有意义：总共退出几次、其中多少次是「总闸写的」（那是我或计划任务主动停的）、
    多少次是「不该停的那种」（崩溃 / 被掐 / 码不明）。混成一堆数字时，一次崩溃和一次正常退出长得一样。
    """
    got: dict = {}
    missing = "查不到（logs/exits.jsonl 还没记过）"
    try:
        fn = summary
        if fn is None:
            from core.infra import exitlog as _el
            fn = _el.summary
        got = dict(fn(hours=float(days) * 24.0) or {})
    except Exception as exc:  # noqa: BLE001 - 一个数据源坏掉不许把整张周报炸掉
        log.warning(f"weekly_restart_metrics_failed: err={repr(exc)[:120]}")
        got = {}
    by_why = dict(got.get("by_why") or {})
    by_role = dict(got.get("by_role") or {})
    if not got.get("total"):
        return [
            Metric("restarts.total", None, "次", "这一周记到的退出", missing),
            Metric("restarts.bad", None, "次", "崩溃 / 被掐 / 码不明", missing),
            Metric("restarts.all_stop", None, "次", "总闸写的（主动停）", missing),
            Metric("restarts.engine", None, "次", "引擎退出（不等于崩溃）", missing),
        ]
    total = int(got.get("total") or 0)
    good = int(by_why.get("all_stop") or 0) + int(by_why.get("clean") or 0) + int(by_why.get("already_running") or 0)
    bad = max(0, total - good)
    return [
        Metric("restarts.total", float(total), "次", "这一周记到的退出（含主动停与互斥早退）"),
        Metric("restarts.bad", float(bad), "次",
               "不该停的那种：%d 次里 %d 次不是 all_stop／clean／already_running" % (total, bad)),
        Metric("restarts.all_stop", float(by_why.get("all_stop") or 0), "次", "总闸（logs/all.stop）写的"),
        Metric("restarts.engine", float(by_role.get("engine") or 0), "次",
               "引擎退出 %d 次；重启次数不等于崩溃次数，要看 why 那一列" % int(by_role.get("engine") or 0)),
    ]
def _blocked_metrics(*, days: int, blocked: Any = None) -> list[Metric]:
    """§45：这一周有多少话被**她自己**拦下来了、被哪道闸拦的。

    读的是 `logs/blocked.jsonl`（四道闸共用的旁证）。**文件不存在时给 `None`，不给 0**：
    「一次都没拦过」和「根本没记过」是完全相反的结论，混成 0 就会读错。
    """
    missing = "查不到（logs/blocked.jsonl 还没记过）"
    got: dict = {}
    try:
        fn = blocked
        if fn is None:
            from core.observability import blocked_log as _bl

            fn = _bl.summarize
        got = dict(fn(days=int(days)) or {})
    except Exception as exc:  # noqa: BLE001 - 一个数据源坏掉不许把整张周报炸掉
        log.warning(f"weekly_blocked_metrics_failed: err={repr(exc)[:120]}")
        got = {}
    empty = [
        Metric("blocked.gate_total", None, "条", "这一周被自己拦下来的话", missing),
        Metric("blocked.gate_allowlist", None, "条", "不在说话白名单里", missing),
        Metric("blocked.gate_content", None, "条", "内容闸（钱 / 地址 / 替人答应 / 承诺）", missing),
        Metric("blocked.gate_outbound", None, "条", "外发闸（去重 / 太密 / 空话）", missing),
        Metric("blocked.gate_spont", None, "条", "自发闸（没人点她名 / 太密）", missing),
    ]
    if not got or got.get("missing") or not got.get("ok", True):
        return empty
    by = dict(got.get("by_category") or {})
    total = int(got.get("total") or 0)
    named = (
        ("白名单", int(by.get("allowlist") or 0)),
        ("内容闸", int(by.get("content") or 0)),
        ("外发闸", int(by.get("outbound") or 0)),
        ("自发闸", int(by.get("spont") or 0)),
        ("其它", int(by.get("other") or 0)),
    )
    dist = "、".join("%s %d 条" % (k, v) for k, v in named if v) or "一条都没有"
    return [
        Metric("blocked.gate_total", float(total), "条", "这一周被自己拦下来的话：%s" % dist),
        Metric("blocked.gate_allowlist", float(by.get("allowlist") or 0), "条", "不在说话白名单里"),
        Metric("blocked.gate_content", float(by.get("content") or 0), "条",
               "内容闸：%d 条里 %d 条是内容闸挡的" % (total, int(by.get("content") or 0))),
        Metric("blocked.gate_outbound", float(by.get("outbound") or 0), "条", "外发闸（去重 / 太密 / 空话）"),
        Metric("blocked.gate_spont", float(by.get("spont") or 0), "条", "自发闸（没人点她名 / 太密）"),
    ]


def build_weekly(
    *,
    days: int = DEFAULT_DAYS,
    now: float | None = None,
    engine: Any = None,
    metrics: Mapping[str, Any] | None = None,
    turn_metrics: Callable[..., list[Metric]] | None = None,
    receipt_metrics: Callable[..., list[Metric]] | None = None,
    job_metrics: Callable[..., list[Metric]] | None = None,
    truth_metrics: Callable[..., list[Metric]] | None = None,
    restart_metrics: Callable[..., list[Metric]] | None = None,
    reverse_metrics: Callable[..., list[Metric]] | None = None,
    blocked_metrics: Callable[..., list[Metric]] | None = None,
) -> WeeklyReport:
    """算一张周报。所有数据源都可注入（测试不碰真库、真量具）。"""
    now = time.time() if now is None else float(now)
    since = now - max(1, int(days)) * DAY_S
    eng = _meta_engine() if engine is None else engine
    got: list[Metric] = []
    got.extend((turn_metrics or _turn_metrics)(engine=eng, since=since))
    got.extend((receipt_metrics or _receipt_metrics)(engine=eng, since=since))
    got.extend((job_metrics or _job_metrics)(engine=eng, since=since))
    got.extend((truth_metrics or _receipt_truth_metrics)(days=int(days)))
    got.extend((reverse_metrics or _reverse_metrics)(days=int(days)))
    got.extend((blocked_metrics or _blocked_metrics)(days=int(days)))
    got.extend((restart_metrics or _restart_metrics)(days=int(days)))
    got.extend(_process_metrics(metrics=metrics))
    notes = [
        "`未采集` ≠ 0：前者是没测，后者是测了确实是零。",
        "锁等待与能力阻断来自进程内量具，进程重启即清零——跨周比较要看日志，不看这张表。",
        "弃权率的分母是 turn_trace 的行数；TURN_TRACE=off 时这张表是空的，别把它读成「她从不弃权」。",
        "restarts.* 读的是 logs/exits.jsonl（2026-11-05 起才有）：在它之前的历史只能看 logs/*.out，那张表在 §33；",
    ]
    report = WeeklyReport(days=int(days), at=_now_iso(now), metrics=tuple(got), notes=tuple(notes))
    log.info("weekly_report days=%d metrics=%d", report.days, len(report.metrics))
    return report
