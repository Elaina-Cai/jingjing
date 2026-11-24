"""五项设备量具：名字白名单、计数、p90、不写盘。

量具最危险的失效不是"算错"，而是**静默为零**——名字打错、字段写错、没人调，
报表上看起来一切正常。所以这里把「名字必须在白名单里」做成硬失败。
"""

from __future__ import annotations

import logging
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.gui import metrics

CHECKS: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append(name)
    if cond:
        print(f"  ok   {name}")
        return
    FAILED.append(f"{name} {detail}".strip())
    print(f"  FAIL {name} {detail}".strip())


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


def t_names() -> None:
    check("六项齐全", len(metrics.METRIC_NAMES) == 6, str(metrics.METRIC_NAMES))
    check("没有重复", len(set(metrics.METRIC_NAMES)) == 6)
    check(
        "六项与设计同名",
        set(metrics.METRIC_NAMES)
        == {
            "input_wait_ms",
            "preempt_count",
            "dirty_composer_cleared",
            "starvation_skips",
            "blocked_capability",
            "lease_stuck",
        },
        str(metrics.METRIC_NAMES),
    )


def t_unknown_name_fails_closed() -> None:
    metrics.reset()
    raised = ""
    try:
        metrics.bump("input_wait_p99")
    except ValueError as exc:
        raised = str(exc)
    check("打错名字 = 硬失败", "未知量具" in raised, raised)
    check("打错名字不入账", all(v == 0 for v in metrics.counts().values()), str(metrics.counts()))


def t_bump_counts_and_returns() -> None:
    metrics.reset()
    first = metrics.bump("preempt_count")
    second = metrics.bump("preempt_count", reason="higher_prio")
    check("返回值是累计数", (first, second) == (1, 2), f"{first},{second}")
    check("counts 反映累计", metrics.counts()["preempt_count"] == 2)
    check("未打过的量具为 0", metrics.counts()["starvation_skips"] == 0)


def t_fields_go_to_log_only() -> None:
    metrics.reset()
    cap = Capture()
    logger = logging.getLogger("core.execution.gui.metrics")
    logger.addHandler(cap)
    logger.setLevel(logging.INFO)
    try:
        metrics.bump("blocked_capability", reason="dpi_mismatch", pid=1234)
    finally:
        logger.removeHandler(cap)
    joined = "\n".join(cap.records)
    check("字段进日志", "reason=dpi_mismatch" in joined and "pid=1234" in joined, joined)
    check("日志带量具名", "blocked_capability" in joined, joined)


def t_field_budget() -> None:
    metrics.reset()
    raised = ""
    try:
        metrics.bump("input_wait_ms", **{f"f{i}": i for i in range(metrics.MAX_FIELDS + 1)})
    except ValueError as exc:
        raised = str(exc)
    check("字段太多硬失败", "字段太多" in raised, raised)


def t_p90() -> None:
    metrics.reset()
    for i in range(1, 101):
        metrics.bump("input_wait_ms", value=i)
    got = metrics.p90("input_wait_ms")
    check("p90 落在 90 附近", 88.0 <= got <= 92.0, str(got))
    check("空样本 p90 = 0", metrics.p90("starvation_skips") == 0.0)
    check("非数值 value 不进样本", metrics.p90("preempt_count") == 0.0)


def t_samples_bounded() -> None:
    metrics.reset()
    for i in range(metrics._MAX_SAMPLES + 50):
        metrics.bump("input_wait_ms", value=i)
    with metrics._lock:
        kept = len(metrics._samples["input_wait_ms"])
    check("样本有上限（不无限长）", kept == metrics._MAX_SAMPLES, str(kept))


def t_counts_is_a_copy() -> None:
    metrics.reset()
    metrics.bump("starvation_skips")
    snap = metrics.counts()
    snap["starvation_skips"] = 999
    check("counts 是拷贝", metrics.counts()["starvation_skips"] == 1, str(metrics.counts()))
    metrics.reset()
    check("reset 清空", all(v == 0 for v in metrics.counts().values()), str(metrics.counts()))


def t_thread_safe() -> None:
    metrics.reset()

    def work() -> None:
        for _ in range(10):
            metrics.bump("dirty_composer_cleared")

    threads = [threading.Thread(target=work) for _ in range(100)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    check("100 线程 × 10 次 = 1000", metrics.counts()["dirty_composer_cleared"] == 1000, str(metrics.counts()["dirty_composer_cleared"]))


def t_snapshot_shape() -> None:
    metrics.reset()
    metrics.bump("input_wait_ms", value=2500)
    snap = metrics.snapshot()
    check("snapshot 覆盖六项", set(snap) == set(metrics.METRIC_NAMES), str(list(snap)))
    check("snapshot 每项有 n 与 p90", all({"n", "p90"} <= set(v) for v in snap.values()), str(snap))


def t_static_no_ledger_write() -> None:
    src = pathlib.Path("core/execution/gui/metrics.py").read_text(encoding="utf-8")
    check("量具不碰账本", "from core.terminal" not in src and "TerminalEventStore(" not in src)
    check("量具不落盘", "sqlite3" not in src and "open(" not in src and "write_text" not in src)
    check("只写日志", "log.info(" in src)


def main() -> int:
    for fn in (
        t_names,
        t_unknown_name_fails_closed,
        t_bump_counts_and_returns,
        t_fields_go_to_log_only,
        t_field_budget,
        t_p90,
        t_samples_bounded,
        t_counts_is_a_copy,
        t_thread_safe,
        t_snapshot_shape,
        t_static_no_ledger_write,
    ):
        fn()
    print(f"RESULT {len(FAILED)} failed / {len(CHECKS)} checks")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
