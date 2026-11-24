# -*- coding: utf-8 -*-
"""N2 第二期③：**R-C7 两路质量对比读数**回归（快路径 vs 规划路径）。

契约硬条款（``docs/decisions/架构评估.md`` §2.4 R-C7）：
**「没有对比读数，不许宣称快路径合格」**。所以本文件钉的不是"快路径好不好"，
而是"读数的**结构**与**诚实性**"：

① 两条路径各自的分子/分母结构正确（分母 = 该路径轮数；胡编率的分母 = 有闸门观测的轮）；
② **分母为 0 → ``n/a``**（不是 ``0.0``，更不是编一个数）——这一条就是变异自检的靶子；
③ 同一批输入跑两次 → 数字一致（确定性：无随机、无时钟参与计算）；
④ 读数与结论的关系：算得出来才下结论，算不出来一律 ``undetermined``；
    快路径高于规划 → ``fail``；快路径出现无证据断言 → ``fail``（硬线）。

脚本位置：``scripts/score_two_path_quality.py``（只读；**不新建表**，读既有
``decision_trace.jsonl`` 的 ``kind="plan_path"`` 与 ``feedback_samples``）。

Run directly:
    python tests/test_two_path_quality.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
# 隔离前导（照 tests/test_plan_loop.py:39-50）。
_TEST_LOG_DIR = str(Path("data") / "_test_logs_two_path_quality")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_DIR", _TEST_LOG_DIR)
_ISO_ROOT = (ROOT / "data" / "_two_path_quality_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))
# **测试卫生**：本文件依赖的每一个开关都在 import 期钉死"关"（读脚本不依赖开关，
# 但 `plan_path` 的写者依赖 `PLAN_LOOP`；这里只用**固定输入**驱动读数，不真跑回合）。
os.environ.setdefault("YINYUE_FEATURE_PLAN_LOOP", "off")
os.environ.setdefault("YINYUE_FEATURE_DECISION_TRACE", "off")

# 与**写者**同源的常量（不许在本文件另抄一份字面量）。
from agents.planner import PLAN_PATH_KIND  # noqa: E402

_SCRIPT_PATH = ROOT / "scripts" / "score_two_path_quality.py"


def _load_script():
    """按路径加载读数脚本（``scripts/`` 不是包，import 只能这样来）。"""
    spec = importlib.util.spec_from_file_location("score_two_path_quality", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SPQ = _load_script()

CHAT = "10000000001@chatroom"


# ---------------------------------------------------------------------------
# 固定输入（**不含任何随机与时钟**：每条记录的时间戳都是写死的）
# ---------------------------------------------------------------------------
def _plan_path(ts: str, path: str, chat: str = CHAT, **over) -> dict:
    row = {
        "kind": PLAN_PATH_KIND,
        "ts": ts,
        "chat_id": chat,
        "path": path,
        "reason": "intent_simple" if path == "fast_path" else "escalated",
        "fast_eligible": path == "fast_path",
        "origin": "direct",
        "plan_steps": 0 if path == "fast_path" else 1,
        "plan_digest": "" if path == "fast_path" else "abc123def456",
    }
    row.update(over)
    return row


def _event(ts: str, kind: str, chat: str = CHAT, **over) -> dict:
    row = {"kind": kind, "ts": ts, "chat_id": chat}
    row.update(over)
    return row


def _fixture() -> tuple[list[dict], list[dict]]:
    """四条快路径轮 + 四条规划轮，外加若干质量事件。

    分母口径：快路径 4 轮、规划 4 轮；闸门观测只落在**三条**快路径轮与**两条**
    规划轮上（胡编率的分母因此是 3 / 2，不是 4 / 4——这正是"没有观测就没有分母"）。
    """
    rows = [
        _plan_path("2026-10-02T10:00:00", "fast_path"),
        _plan_path("2026-10-02T10:10:00", "fast_path"),
        _plan_path("2026-10-02T10:20:00", "fast_path"),
        _plan_path("2026-10-02T10:30:00", "fast_path"),
        _plan_path("2026-10-02T11:00:00", "planned"),
        _plan_path("2026-10-02T11:10:00", "planned"),
        _plan_path("2026-10-02T11:20:00", "planned"),
        _plan_path("2026-10-02T11:30:00", "planned"),
    ]
    events = [
        # 误解：快路径 2 条、规划 1 条（``addressee_repaired`` = 出站前真的改了）
        _event("2026-10-02T10:00:30", "addressee_repaired", rule="addressee_guard"),
        _event("2026-10-02T10:20:30", "addressee_repaired", rule="addressee_guard"),
        _event("2026-10-02T11:00:30", "addressee_repaired", rule="addressee_guard"),
        # 影子观测**不算**误解（那只是"会错"，不是"错了"）
        _event("2026-10-02T10:30:30", "addressee_shadow", rule="addressee_guard"),
        # 胡编：快路径 1 条（无证据断言），规划 0 条
        _event("2026-10-02T10:10:20", "gate_hit", rule="unevidenced_claim"),
        # 闸门观测（胡编率的分母）：快路径 3 轮、规划 2 轮
        _event("2026-10-02T10:00:10", "gate_hit", rule="group_max_chars"),
        _event("2026-10-02T10:10:10", "gate_hit", rule="group_max_chars"),
        _event("2026-10-02T10:20:10", "gate_shadow", rule="lexicon"),
        _event("2026-10-02T11:00:10", "gate_hit", rule="group_max_chars"),
        _event("2026-10-02T11:10:10", "output_gate", rule="roster_leak"),
    ]
    return rows, events


def _corrections() -> list[dict]:
    # 被纠正：快路径 1 条、规划 0 条
    return [_event("2026-10-02T10:30:10", "correction")]


# ---------------------------------------------------------------------------
# ① 两路各自的分子/分母结构
# ---------------------------------------------------------------------------
def test_each_path_has_its_own_numerator_and_denominator() -> None:
    """★ 用例①：按路径分组，分子/分母**逐项**正确（口径见脚本 docstring）。"""
    rows, events = _fixture()
    reading = SPQ.aggregate(rows, _corrections(), events)

    fast = reading["paths"]["fast_path"]
    planned = reading["paths"]["planned"]
    assert fast["turns"] == 4 and planned["turns"] == 4, "分母 = 该路径的轮数"
    assert fast["misread"] == {"numerator": 2, "denominator": 4}
    assert planned["misread"] == {"numerator": 1, "denominator": 4}
    assert fast["corrected"] == {"numerator": 1, "denominator": 4}
    assert planned["corrected"] == {"numerator": 0, "denominator": 4}
    assert fast["fabricated"] == {"numerator": 1, "denominator": 3}, (
        "胡编率的分母 = **有闸门观测的轮**（3 条快路径轮有观测），不是总轮数"
    )
    assert planned["fabricated"] == {"numerator": 0, "denominator": 2}
    assert reading["join_unmatched"] == 0, "夹具里的事件都该落进窗口"

    # 读数（浮点文本）与分子分母一致
    assert SPQ.rate(fast["misread"]) == "0.5000"
    assert SPQ.rate(planned["misread"]) == "0.2500"
    assert SPQ.rate(fast["corrected"]) == "0.2500"
    assert SPQ.rate(planned["corrected"]) == "0.0000", (
        "分母 > 0 且分子 = 0 → 就是 0.0（**不是** n/a）"
    )
    assert SPQ.rate(fast["fabricated"]) == "0.3333"


def test_verdicts_read_the_numbers_only() -> None:
    """① 附加：结论只按算得出来的数字下——快路径差 → ``fail``，好 → ``pass``。"""
    rows, events = _fixture()
    reading = SPQ.aggregate(rows, _corrections(), events)
    verdict_rows = SPQ.verdicts(reading)
    by_metric = {item["metric"]: item for item in verdict_rows}
    assert by_metric["misread"]["verdict"] == "fail", "快路径误解率 0.5 > 规划 0.25"
    assert by_metric["corrected"]["verdict"] == "fail", "快路径被纠正率 0.25 > 规划 0"
    assert by_metric["fabricated"]["verdict"] == "fail", "快路径有胡编（硬线）"
    assert SPQ.overall(verdict_rows) == "fail"

    # 反过来：快路径不比规划差 → pass（同一个判据，方向相反）
    clean_rows = [
        _plan_path("2026-10-02T12:00:00", "fast_path"),
        _plan_path("2026-10-02T12:10:00", "planned"),
    ]
    clean_events = [
        _event("2026-10-02T12:00:10", "gate_hit", rule="group_max_chars"),
        _event("2026-10-02T12:10:10", "gate_hit", rule="group_max_chars"),
        _event("2026-10-02T12:10:20", "addressee_repaired", rule="addressee_guard"),
    ]
    verdict_rows = SPQ.verdicts(SPQ.aggregate(clean_rows, [], clean_events))
    by_metric = {item["metric"]: item for item in verdict_rows}
    assert by_metric["misread"]["verdict"] == "pass"
    assert by_metric["fabricated"]["verdict"] == "pass"
    assert SPQ.overall(verdict_rows) == "pass"


# ---------------------------------------------------------------------------
# ② 分母为 0 → n/a（不编数字）
# ---------------------------------------------------------------------------
def test_zero_denominator_is_na_and_never_a_number() -> None:
    """★ 用例②：分母为 0 → ``n/a``；**不许**出现 "0" 这种编出来的读数。

    三种零分母：① 两条路径都没有轮；② 只有快路径有轮（规划那一侧 n/a）；
    ③ 有轮但**没有闸门观测**（胡编率的分母为 0）。
    """
    # ① 两个通道都空
    reading = SPQ.aggregate([], [], [])
    assert reading["paths"]["fast_path"]["turns"] == 0
    for path in SPQ.PATHS:
        for key in ("misread", "corrected", "fabricated"):
            pair = reading["paths"][path][key]
            assert pair == {"numerator": 0, "denominator": 0}
            assert SPQ.rate(pair) == "n/a", "分母为 0 必须是 n/a"
    verdict_rows = SPQ.verdicts(reading)
    assert {item["verdict"] for item in verdict_rows} == {"undetermined"}
    assert all("分母为 0" in str(item["why"]) for item in verdict_rows), (
        "结论必须写明缺哪一半分母"
    )
    assert SPQ.overall(verdict_rows) == "undetermined"

    # ② 只有快路径有轮
    only_fast = [_plan_path("2026-10-02T13:00:00", "fast_path")]
    reading = SPQ.aggregate(only_fast, [], [])
    assert SPQ.rate(reading["paths"]["fast_path"]["misread"]) == "0.0000"
    assert SPQ.rate(reading["paths"]["planned"]["misread"]) == "n/a"
    by_metric = {item["metric"]: item for item in SPQ.verdicts(reading)}
    assert by_metric["misread"]["verdict"] == "undetermined"
    assert "规划路径" in by_metric["misread"]["why"]

    # ③ 有轮但没观测 → 胡编率的分母为 0（**不许**把"没观测"读成"零胡编"）
    rows = [
        _plan_path("2026-10-02T14:00:00", "fast_path"),
        _plan_path("2026-10-02T14:10:00", "planned"),
    ]
    reading = SPQ.aggregate(rows, [], [])
    assert reading["paths"]["fast_path"]["fabricated"] == {
        "numerator": 0, "denominator": 0,
    }
    assert SPQ.rate(reading["paths"]["fast_path"]["fabricated"]) == "n/a"
    by_metric = {item["metric"]: item for item in SPQ.verdicts(reading)}
    assert by_metric["fabricated"]["verdict"] == "undetermined", (
        "没有观测通道就没有分母——不许拿'没有命中记录'冒充'零胡编'"
    )

    # 全量读数产物里也一个编出来的数字都没有
    readout = SPQ.build_readout(
        decision_trace=str(_ISO_ROOT / "missing.jsonl"), meta_db=str(_ISO_ROOT / "no.db"),
    )
    payload = json.dumps(readout, ensure_ascii=False)
    assert '"fast_path": "n/a"' in payload or "n/a" in payload
    for item in readout["verdicts"]:
        assert item["verdict"] == "undetermined"
    assert readout["overall"] == "undetermined"


def test_readout_script_missing_channel_is_an_input_error() -> None:
    """② 附加：**通道不存在**是输入问题（退出码 1），不是"零读数"。"""
    missing = str(_ISO_ROOT / "不存在.jsonl")
    assert SPQ.main(["--decision-trace", missing]) == 1
    # 通道存在但一条 plan_path 都没有 → 如实 0 轮 + n/a（退出码 0）
    empty = _ISO_ROOT / "empty.jsonl"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("", encoding="utf-8")
    assert SPQ.main(["--decision-trace", str(empty), "--json", "-"]) == 0


# ---------------------------------------------------------------------------
# ③ 同一批输入两次 → 同一批数字
# ---------------------------------------------------------------------------
def test_same_input_twice_yields_identical_numbers() -> None:
    """★ 用例③：确定性——同一批输入两次，读数**逐字节相同**。

    覆盖 ① 纯函数口径（``aggregate`` / ``verdicts``）与 ② 整条读数链
    （``build_readout``：读文件 + 读库 + 联接 + 结论）。时间戳全部写死，
    所以"两次一致"这件事本身就是"没有随机、没有时钟参与计算"的证据。
    """
    rows, events = _fixture()
    corrections = _corrections()
    first = SPQ.aggregate(rows, corrections, events)
    second = SPQ.aggregate(list(rows), list(corrections), list(events))
    assert first == second
    assert json.dumps(SPQ.verdicts(first), ensure_ascii=False, sort_keys=True) == (
        json.dumps(SPQ.verdicts(second), ensure_ascii=False, sort_keys=True)
    )

    # 乱序输入 → 同一读数（排序是稳定序，不靠调用方先排好）
    shuffled = list(reversed(rows))
    assert SPQ.aggregate(shuffled, corrections, list(reversed(events))) == first

    # 整条链：写到临时通道，跑两次
    trace = _ISO_ROOT / "determinism.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False) for item in (rows + events)
        ) + "\n",
        encoding="utf-8",
    )
    db_path = _make_meta_db(["2026-10-02T10:30:10|" + CHAT])
    a = SPQ.build_readout(decision_trace=str(trace), meta_db=str(db_path))
    b = SPQ.build_readout(decision_trace=str(trace), meta_db=str(db_path))
    assert a == b
    assert a["reading"]["paths"]["fast_path"]["turns"] == 4
    assert a["reading"]["paths"]["fast_path"]["corrected"]["numerator"] == 1, (
        "被纠正率的分母/分子要能从 feedback_samples 里读出来"
    )
    assert a["overall"] == "fail"


def _make_meta_db(corrections: list[str]) -> Path:
    """建一个**临时** meta 库，只有 ``feedback_samples``（表结构照既有迁移的最小列）。"""
    path = _ISO_ROOT / f"meta_{abs(hash(tuple(corrections))) % 10**8}.db"
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE feedback_samples (id INTEGER PRIMARY KEY, created_at TEXT,"
            " chat_id TEXT, source TEXT, status TEXT)"
        )
        for index, item in enumerate(corrections):
            created_at, chat_id = item.split("|", 1)
            conn.execute(
                "INSERT INTO feedback_samples (id, created_at, chat_id, source, status)"
                " VALUES (?, ?, ?, 'correction', 'new')",
                (index + 1, created_at, chat_id),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def test_correction_channel_missing_is_reported_not_guessed() -> None:
    """③ 附加：``feedback_samples`` 读不到 → 那一项报 ``n/a`` 并写明原因（不猜）。"""
    rows, events = _fixture()
    readout = SPQ.build_readout(
        decision_trace=str(_ISO_ROOT / "determinism.jsonl"),
        meta_db=str(_ISO_ROOT / "not_there.db"),
    )
    assert "不存在" in str(readout["inputs"]["meta_db_note"])
    assert readout["reading"]["paths"]["fast_path"]["corrected"] == {
        "numerator": 0, "denominator": 4,
    }, "通道缺失只影响被纠正率的**分子**，分母仍是该路径的轮数"

    # 表在但一行都没有 → 同样是 0（真读数：分母 > 0、分子 0）
    empty_db = _make_meta_db([])
    readout = SPQ.build_readout(
        decision_trace=str(_ISO_ROOT / "determinism.jsonl"), meta_db=str(empty_db),
    )
    assert readout["inputs"]["meta_db_note"] == ""
    assert readout["reading"]["paths"]["fast_path"]["corrected"]["numerator"] == 0


def test_unknown_path_rows_are_never_folded_into_a_group() -> None:
    """③ 附加：``path`` 取值不认识 → **不进任何一组**（不许"猜个最近似的组"）。"""
    rows = [
        _plan_path("2026-10-02T15:00:00", "fast_path"),
        _plan_path("2026-10-02T15:10:00", "not_a_path"),
        _plan_path("2026-10-02T15:20:00", "planned"),
    ]
    kept = SPQ.path_rows(rows)
    assert [row["path"] for row in kept] == ["fast_path", "planned"]
    reading = SPQ.aggregate(kept, [], [])
    assert reading["paths"]["fast_path"]["turns"] == 1
    assert reading["paths"]["planned"]["turns"] == 1


def main() -> bool:
    tests = [fn for name, fn in globals().items() if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
