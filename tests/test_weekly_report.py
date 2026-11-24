r"""U-29 周报：`未采集` 与 `0` 必须是两件事。

钉住的边界：

- 查不到（没库/表不在）→ `未采集`；查得到但空表 → `0` + 备注「这一周 0 条」；
- 弃权率分子只认 `abstain/skip/silent`，分母是 turn_trace 总行数；
- 重复发送率 = duplicate / sent（sent 为 0 时 `未采集`，不是 0%）；
- 进程内量具没接 → 四行 `未采集`（不是 0 次）；
- 报表只读：模块里不许出现 INSERT/UPDATE/DELETE，也不许 import 说话层。

跑法：.venv_it\Scripts\python.exe -X utf8 -u tests/test_weekly_report.py
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.observability import weekly as W  # noqa: E402

COUNT = [0]
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(f"{name} {detail}".strip())
    print(f"  FAIL {name} {detail}".strip())


class _Conn:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine

    def execute(self, sql, params=None):
        self.engine.seen.append(" ".join(str(sql).split())[:60])
        return self.engine.queue.pop(0)

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeEngine:
    """按调用顺序发结果：每个元素都是 ``list[tuple]`` 或 ``None``（= 查不到）。"""

    def __init__(self, *results) -> None:
        self.queue = list(results)
        self.seen: list[str] = []

    def connect(self) -> _Conn:
        return _Conn(self)


def t_metric_text() -> None:
    print("[t_metric_text]")
    check("None 写未采集", W.Metric("x", None).text == "未采集")
    check("百分比一位小数", W.Metric("x", 0.1234, "%").text == "12.3%")
    check("整数不带小数点", W.Metric("x", 7.0).text == "7")
    check("小数一位", W.Metric("x", 2.55).text == "2.5")
    check("零显式写 0", W.Metric("x", 0.0).text == "0")


def t_missing_vs_empty() -> None:
    print("[t_missing_vs_empty]")
    missing = W._turn_metrics(engine=None, since=0.0)
    check("没库 = 未采集", all(m.value is None for m in missing), str([m.value for m in missing]))
    check("说清查不到", "查不到" in missing[0].note, missing[0].note)

    empty = W._turn_metrics(engine=FakeEngine([]), since=0.0)
    check("空表 = 0 回合", empty[0].value == 0.0, str(empty[0].value))
    check("空表有备注", "0 条" in empty[0].note, empty[0].note)
    check("空表时比率未采集", empty[1].value is None and empty[2].value is None)

    r_missing = W._receipt_metrics(engine=None, since=0.0)
    check("回执没库 = 未采集", all(m.value is None for m in r_missing))
    r_empty = W._receipt_metrics(engine=FakeEngine([]), since=0.0)
    check("回执空表 = 0 条", r_empty[0].value == 0.0)
    j_empty = W._job_metrics(engine=FakeEngine([]), since=0.0)
    check("事件空表 = 0 件", j_empty[0].value == 0.0)


def t_turn_rates() -> None:
    print("[t_turn_rates]")
    eng = FakeEngine([("respond", 6), ("abstain", 3), ("skip", 1)], [(8,)])
    got = W._turn_metrics(engine=eng, since=0.0)
    by = {m.name: m for m in got}
    check("回合数 = 10", by["turns"].value == 10.0, str(by["turns"].value))
    check("弃权率 = 4/10", abs(by["abstain_rate"].value - 0.4) < 1e-9, str(by["abstain_rate"].value))
    check("指回率 = 8/10", abs(by["basis_coverage"].value - 0.8) < 1e-9, str(by["basis_coverage"].value))
    check("分决策也列出来", by["decisions.abstain"].value == 3.0)
    check("口径写分母", "8/10" in by["basis_coverage"].basis, by["basis_coverage"].basis)
    check("查了两条 SQL", len(eng.seen) == 2, str(eng.seen))


def t_receipt_rates() -> None:
    print("[t_receipt_rates]")
    eng = FakeEngine([("sent", 9), ("failed", 1), ("duplicate", 1), ("blocked", 9)])
    got = {m.name: m for m in W._receipt_metrics(engine=eng, since=0.0)}
    check("重复率 = 1/9", abs(got["duplicate_rate"].value - 1 / 9) < 1e-9)
    check("越权被挡 = 9/20", abs(got["blocked_rate"].value - 0.45) < 1e-9)
    only_failed = {m.name: m for m in W._receipt_metrics(engine=FakeEngine([("failed", 2)]), since=0.0)}
    check("没有 sent 时重复率未采集", only_failed["duplicate_rate"].value is None)


def t_job_metrics() -> None:
    print("[t_job_metrics]")
    got = {m.name: m for m in W._job_metrics(engine=FakeEngine([("done", 3), ("handed_off", 1), ("awaiting", 2)]), since=0.0)}
    check("办成 = 3", got["jobs.done"].value == 3.0)
    check("等人 = 2", got["jobs.awaiting"].value == 2.0)
    check("交出去 = 1", got["jobs.stopped"].value == 1.0)


def t_process_metrics() -> None:
    print("[t_process_metrics]")
    none = W._process_metrics(metrics=None)
    check("没接量具 = 四行未采集", all(m.value is None for m in none) and len(none) == 4, str([m.value for m in none]))
    got = {m.name: m for m in W._process_metrics(metrics={"p90": {"input_wait_ms": 240.0},
                                                          "counts": {"lease_stuck": 2, "blocked_capability": 1,
                                                                     "dirty_composer_cleared": 5}})}
    check("p90 取到", got["input_wait_p90_ms"].value == 240.0)
    check("僵死次数", got["lease_stuck"].value == 2.0)
    check("残留清理次数", got["dirty_composer_cleared"].value == 5.0)
    zero = {m.name: m for m in W._process_metrics(metrics={"p90": {}, "counts": {}})}
    check("p90 为 0 记未采集", zero["input_wait_p90_ms"].value is None)
    check("计数为 0 记 0", zero["lease_stuck"].value == 0.0)


def t_report_shape() -> None:
    print("[t_report_shape]")
    def fake(**kw):
        return [W.Metric("turns", 10.0, "回合", "测试"), W.Metric("abstain_rate", 0.2, "%", "2/10")]

    r = W.build_weekly(days=7, now=1_000_000.0, engine=FakeEngine(),
                       turn_metrics=fake,
                       receipt_metrics=lambda **kw: [W.Metric("receipts.sent", 5.0, "条", "5/5")],
                       job_metrics=lambda **kw: [W.Metric("jobs.done", 1.0, "件", "1")],
                       metrics=None)
    check("days 记下", r.days == 7)
    check("metric() 取到", r.metric("turns").value == 10.0)
    check("value() 取不到给 None", r.value("nope") is None)
    text = r.markdown()
    for title in ("一、这一周发生了什么", "二、她说不说", "三、发得对不对", "四、手有没有被卡住", "五、事有没有办完"):
        check(f"有《{title}》", title in text)
    check("有口径段", "口径与未采集" in text)
    check("未采集≠0 写明", "`未采集` ≠ 0" in text)
    check("dict 有 metrics", len(r.as_dict()["metrics"]) >= 4)
    check("生成时间有值", bool(r.at))


def t_meta_path_env() -> None:
    print("[t_meta_path_env]")
    old = os.environ.get("META_DB_PATH")
    os.environ["META_DB_PATH"] = "x/y.db"
    try:
        check("META_DB_PATH 优先", W.meta_db_path() == "x/y.db", W.meta_db_path())
    finally:
        os.environ.pop("META_DB_PATH", None)
        # 隔离 runner 会注入自己的 META_DB_PATH（绝对路径）。「默认值」只能在 env
        # 真为空的窗口里断言，否则这条永远只在 CI 里红。
        try:
            check("默认 meta 库", W.meta_db_path() == "data/yinyue_meta.db",
                  W.meta_db_path())
        finally:
            if old is not None:
                os.environ["META_DB_PATH"] = old


def t_readonly_discipline() -> None:
    print("[t_readonly_discipline]")
    src = pathlib.Path("core/observability/weekly.py").read_text(encoding="utf-8")
    body = src.split('"""', 2)[2]
    for bad in ("INSERT ", "UPDATE ", "DELETE ", "commit()"):
        check(f"不写库（无 {bad.strip()}）", bad not in body)
    check("不 import 说话层", "core.speech" not in src)
    check("不 import 判", "core.routing" not in src)
    check("只读句柄来自 read_only_engine", "read_only_engine" in src)
    check("CLI 存在", pathlib.Path("scripts/weekly_report.py").exists())


def main() -> int:
    for fn in (t_metric_text, t_missing_vs_empty, t_turn_rates, t_receipt_rates, t_job_metrics,
               t_process_metrics, t_report_shape, t_meta_path_env, t_readonly_discipline):
        fn()
    print()
    if FAILS:
        print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"RESULT 0 failed / {COUNT[0]} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
