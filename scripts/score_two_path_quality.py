#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R-C7 两路质量对比读数：**快路径 vs 规划路径**（误解率 / 被纠正率 / 胡编率）。

契约硬条款（``docs/decisions/架构评估.md`` §2.4 R-C7，逐字）：
**「没有对比读数，不许宣称快路径合格」**。所以本脚本的产出不是"结论"，是**分子与分母**；
结论由 :func:`verdicts` 只按算得出来的数字下，算不出来的一律 ``n/a``。

三条纪律
--------
1. **不编数字**：分母为 0 → 该项读数输出 ``"n/a"``（**不是 0.0**）。
   ``0.0`` 的意思只有一个：分母 > 0 且分子 = 0。
2. **不新建表**：只读**既有**通道——
   ``logs/decision_trace.jsonl``（``kind="plan_path"``，每轮一条，写者唯一：
   ``agents/planner.py::Planner.attach_content_plan``）与 meta 库的
   ``feedback_samples`` 表（``source='correction'``，主人纠正的既有采集点）。
3. **可复现**：同输入同输出（按 (ts, kind, chat_id, path) 稳定排序，无随机、无时钟
   参与计算）；同一批输入跑两次数字必须一致（``tests/test_two_path_quality.py`` 用例③）。

口径（分母 / 分子**逐条写死**）
------------------------------
====================  ===============================================================
路径                  ``plan_path`` 记录里 ``path`` 字段：``fast_path`` / ``planned``
**误解率**           分子 = 该轮窗口内出现 ``addressee_repaired``（出站前确定性纠正：
                     她 @ 错了人，被 ``core/speech/outbound.py`` 改正）；分母 = 该路径
                     的轮数。**"判错到要动手改"才是误解**——``addressee_shadow``
                     只是影子观测，不算分子（那是"会不会错"，不是"错了"）。
**被纠正率**         分子 = 该轮窗口内主人发了一条 ``source='correction'`` 的反馈样本
                     （既有采集点 ``core/quality/feedback.py``，唯一写者）；分母 = 该路径
                     的轮数。它量的是"她理解错了，主人不得不纠正一次"。
**胡编率**           分子 = 该轮窗口内出现 ``kind="gate_hit"`` 且 ``rule`` 属于无证据断言
                     类的闸门命中（``unevidenced_claim`` / ``no_evidence``）；分母 =
                     **分母可评**的轮数 = 那些轮里至少有一条闸门观测记录（``gate_hit`` /
                     ``output_gate`` / ``gate_shadow``）——**观测通道没开就没有分母**，
                     此时输出 ``n/a``，绝不拿"没有命中记录"冒充"零胡编"。
====================  ===============================================================

窗口联接（为什么用时间窗而不是 turn_id）
---------------------------------------
``plan_path`` 是每轮一条的**轮级**记录，而质量事件（纠正样本 / 闸门命中）在既有通道里
**不带 turn_id**。所以这里用「同 chat + 事件时间落在该轮之后 ``--window-seconds`` 秒内」
联接，取该窗口内**最早的那一条**路径记录（同一时刻多条时按稳定序取第一条）。
这是**近似**，不是等价——所以 :func:`aggregate` 里额外报 ``join_unmatched``
（窗口内找不到路径记录的事件条数），让"分母偏小"这件事看得见，而不是被算没了。

用法
----
::

    # 只看读数（默认读 logs/decision_trace.jsonl 与 data/meta.db）
    python scripts/score_two_path_quality.py

    # 指定输入（可复现 / 用例用）
    python scripts/score_two_path_quality.py \\
        --decision-trace logs/decision_trace.jsonl --meta-db data/meta.db

    # 机器可读（**只含计数与配置，不含任何用户原文**）
    python scripts/score_two_path_quality.py --json

    # 只要某一批数据
    python scripts/score_two_path_quality.py --since 2026-10-01T00:00:00

退出码：``0`` = 读数算出来了（含"分母为 0 → n/a"这种**如实**情形）；
``1`` = 输入本身有问题（指定的通道不存在 / 读不出来）。
读数**从不**因为"数字不好看"而非零退出。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 与写者同源的常量（``agents/planner.py``）：**逐字**import，不另抄一份字面量
#: （两份字面量就是等着漂移：改名只改一处，另一处从此读不到任何记录）。
from agents.planner import PLAN_PATH_KIND  # noqa: E402

DEFAULT_DECISION_TRACE = os.path.join("logs", "decision_trace.jsonl")
DEFAULT_META_DB = os.path.join("data", "meta.db")
DEFAULT_WINDOW_SECONDS = 180.0

#: 两条路径的**唯一**取值（``plan_path`` 记录的 ``path`` 字段）。
PATH_FAST = "fast_path"
PATH_PLANNED = "planned"
PATHS: tuple[str, ...] = (PATH_FAST, PATH_PLANNED)

#: 误解率分子的记录 kind（出站前的确定性纠正）。
MISREAD_KIND = "addressee_repaired"
#: 胡编率分子：无证据断言类的闸门命中（``rule`` 命中即算）。
FABRICATION_RULES = frozenset({"unevidenced_claim", "no_evidence"})
#: 胡编率的**观测**记录（有这些才说明这一轮的闸门真的在看着）。
GATE_OBSERVATION_KINDS = frozenset({"gate_hit", "output_gate", "gate_shadow"})
#: 被纠正率的数据源（既有表，只读）。
CORRECTION_SOURCE = "correction"

#: 逐条 JSONL 记录里允许进入输出的键（**白名单**：其余一概不进 JSON，
#: 免得把用户原文/内部文本带进读数产物）。
_ROW_KEYS = ("ts", "kind", "chat_id", "path", "reason", "fast_eligible", "origin")


# ---------------------------------------------------------------------------
# 纯函数：时间与 JSONL
# ---------------------------------------------------------------------------
def parse_ts(value: Any) -> datetime | None:
    """ISO 时间戳 → ``datetime``；读不出来返回 ``None``（**不猜、不用当前时间兜底**）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_jsonl(path: str) -> tuple[list[dict[str, Any]], int]:
    """读一行一 JSON 的文件；返回 ``(rows, malformed)``。

    坏行**跳过并计数**（审计文件被截断是常态），绝不因为一行坏掉就整份读不出来。
    """
    rows: list[dict[str, Any]] = []
    malformed = 0
    if not os.path.exists(path):
        return rows, malformed
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                malformed += 1
                continue
            if isinstance(item, dict):
                rows.append(item)
            else:
                malformed += 1
    return rows, malformed


def path_rows(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """筛出 ``kind="plan_path"`` 的轮级记录（**路径分组的唯一来源**）。"""
    out: list[dict[str, Any]] = []
    for item in records:
        if str(item.get("kind") or "") != PLAN_PATH_KIND:
            continue
        path = str(item.get("path") or "").strip()
        if path not in PATHS:
            # 未知路径值不进任何一组：宁可在 coverage 里看见它，也不许塞进某一组
            # 把分子分母搅浑（"猜一个最近似的组"正是本脚本禁止的动作）。
            continue
        out.append(dict(item))
    out.sort(key=_row_sort_key)
    return out


def _row_sort_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("ts") or ""),
        str(item.get("kind") or ""),
        str(item.get("chat_id") or ""),
    )


def load_corrections(db_path: str, *, since: str = "") -> tuple[list[dict[str, Any]], str]:
    """读 ``feedback_samples`` 里 ``source='correction'`` 的行（只读）。

    返回 ``(rows, note)``：表不存在 / 库不存在 → ``([], 原因)``——**不是错误**
    （旧库里就没有这张表），调用方据此把被纠正率报成 ``n/a`` 并写明原因。
    """
    if not os.path.exists(db_path):
        return [], f"meta 库不存在：{db_path}"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [], f"meta 库打不开：{exc}"
    try:
        cur = conn.cursor()
        names = {row[0] for row in cur.execute(
            "select name from sqlite_master where type='table'"
        )}
        if "feedback_samples" not in names:
            return [], "meta 库里没有 feedback_samples 表（该库还没建过这张表）"
        sql = "select created_at, chat_id, source from feedback_samples where source = ?"
        params: list[Any] = [CORRECTION_SOURCE]
        if since:
            sql += " and created_at >= ?"
            params.append(since)
        rows = [
            {"ts": str(created_at or ""), "chat_id": str(chat_id or ""), "kind": "correction"}
            for created_at, chat_id, _source in cur.execute(sql, params)
        ]
    except sqlite3.Error as exc:
        return [], f"feedback_samples 读不出来：{exc}"
    finally:
        conn.close()
    rows.sort(key=_row_sort_key)
    return rows, ""


def join_events(
    rows: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> tuple[list[dict[str, Any]], int]:
    """把质量事件按「同 chat + 时间落在该轮之后 window 秒内」贴到路径记录上。

    返回 ``(joined, unmatched)``：``joined`` 每条形如
    ``{"row": <plan_path 行>, "event": <事件行>}``（按稳定序）；
    ``unmatched`` = 窗口内找不到任何 ``plan_path`` 记录的事件条数——
    **它要能被看见**（那说明分母偏小，而不是"这些事件不算数"）。
    """
    ordered = sorted(rows, key=_row_sort_key)
    joined: list[dict[str, Any]] = []
    unmatched = 0
    for event in sorted(events, key=_row_sort_key):
        at = parse_ts(event.get("ts"))
        if at is None:
            unmatched += 1
            continue
        chat = str(event.get("chat_id") or "")
        hit: Mapping[str, Any] | None = None
        for row in ordered:
            if str(row.get("chat_id") or "") != chat:
                continue
            started = parse_ts(row.get("ts"))
            if started is None:
                continue
            delta = (at - started).total_seconds()
            if 0.0 <= delta <= float(window_seconds):
                hit = row
                break
        if hit is None:
            unmatched += 1
            continue
        joined.append({"row": dict(hit), "event": dict(event)})
    return joined, unmatched


# ---------------------------------------------------------------------------
# 计数与读数
# ---------------------------------------------------------------------------
def count_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """每条路径的**分母基础**：轮数 + 计划可见轮数。"""
    out = {path: {"turns": 0, "with_plan": 0} for path in PATHS}
    for row in rows:
        path = str(row.get("path") or "")
        if path not in out:
            continue
        out[path]["turns"] += 1
        if str(row.get("plan_digest") or "").strip():
            out[path]["with_plan"] += 1
    return out


def aggregate(
    rows: Sequence[Mapping[str, Any]],
    corrections: Sequence[Mapping[str, Any]] = (),
    decision_records: Sequence[Mapping[str, Any]] = (),
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> dict[str, Any]:
    """核心读数：**按路径分组的分子 / 分母**（纯函数，无 IO）。

    ``rows``             ``plan_path`` 轮级记录（分母来源）
    ``corrections``      纠正样本（被纠正率分子来源）
    ``decision_records`` **全部** decision_trace 记录（误解率与胡编率的事件来源）
    """
    counts = count_rows(rows)
    # 事件分三类，**分母与分子各读各的**（一条记录只归一类）：
    #   observation —— 闸门真的看过这一轮（胡编率的**分母**；同轮多条只算一轮）
    #   fabrication —— 无证据断言命中（胡编率的**分子**）
    #   misread / correction —— 误解率与被纠正率的分子
    observations: list[dict[str, Any]] = []
    fabrication: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    for item in decision_records:
        kind = str(item.get("kind") or "")
        if kind in GATE_OBSERVATION_KINDS:
            observations.append({**dict(item), "kind": "observation"})
            if kind == "gate_hit" and str(item.get("rule") or "") in FABRICATION_RULES:
                fabrication.append({**dict(item), "kind": "fabrication"})
        elif kind == MISREAD_KIND:
            others.append({**dict(item), "kind": kind})
    others.extend(dict(item) for item in corrections)

    joined, unmatched = join_events(rows, observations + fabrication + others,
                                    window_seconds=window_seconds)

    result: dict[str, Any] = {
        "window_seconds": float(window_seconds),
        "join_unmatched": int(unmatched),
        "paths": {},
    }
    for path in PATHS:
        turns = int(counts[path]["turns"])
        misread = 0
        corrected = 0
        fabricated = 0
        observed: set[int] = set()
        for pair in joined:
            if str(pair["row"].get("path") or "") != path:
                continue
            kind = str(pair["event"].get("kind") or "")
            if kind == MISREAD_KIND:
                misread += 1
            elif kind == "correction":
                corrected += 1
            elif kind == "fabrication":
                fabricated += 1
            elif kind == "observation":
                # 同一轮的多次观测只算**一轮**（不然分母会随观测条数膨胀）。
                observed.add(_row_identity(pair["row"]))
        result["paths"][path] = {
            "turns": turns,
            "with_plan": int(counts[path]["with_plan"]),
            "misread": {"numerator": misread, "denominator": turns},
            "corrected": {"numerator": corrected, "denominator": turns},
            # 胡编率的分母**只有观测过的轮**才算（见文件头：没有观测通道就没有分母）。
            "fabricated": {"numerator": fabricated, "denominator": len(observed)},
        }
    return result


def _row_identity(row: Mapping[str, Any]) -> int:
    """一轮的稳定标识（同 ts + 同 chat 的 ``plan_path`` 记录算同一轮）。

    用它给"观测过的轮"去重：一轮里可能有多次闸门观测（禁语 + 字数各一条），
    去重后分母才是"轮数"而不是"观测条数"。
    """
    return hash((str(row.get("ts") or ""), str(row.get("chat_id") or "")))


def rate(pair: Mapping[str, Any]) -> str:
    """分子/分母 → 读数。分母为 0 → ``"n/a"``（**绝不编数字**）。"""
    try:
        numerator = int(pair.get("numerator") or 0)
        denominator = int(pair.get("denominator") or 0)
    except (TypeError, ValueError):
        return "n/a"
    if denominator <= 0:
        return "n/a"
    return f"{numerator / denominator:.4f}"


def verdicts(reading: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按算得出来的数字下结论；算不出来的一律 ``undetermined``。

    判据（R-C7 硬条款：**没有对比读数不许宣称快路径合格**）：

    * 两条路径的该指标**都必须有分母**，且至少一条有分子（否则是"都没见过"，
      不是"合格"）→ 否则 ``undetermined``，理由写清缺哪一半；
    * 有分母时：快路径的分子率 **不高于** 规划路径 → ``pass``；高于 → ``fail``。
      胡编率另加一条硬线：快路径分子必须为 0（契约 §8 的"假承诺率 = 0"同一口径）。
    """
    out: list[dict[str, Any]] = []
    metrics = (
        ("misread", "误解率"),
        ("corrected", "被纠正率"),
        ("fabricated", "胡编率"),
    )
    for key, label in metrics:
        fast = dict(reading["paths"][PATH_FAST][key])
        planned = dict(reading["paths"][PATH_PLANNED][key])
        fast_rate = rate(fast)
        planned_rate = rate(planned)
        item: dict[str, Any] = {
            "metric": key,
            "label": label,
            "fast_path": fast_rate,
            "planned": planned_rate,
            "fast_numerator": fast["numerator"],
            "fast_denominator": fast["denominator"],
            "planned_numerator": planned["numerator"],
            "planned_denominator": planned["denominator"],
        }
        if fast_rate == "n/a" or planned_rate == "n/a":
            item["verdict"] = "undetermined"
            item["why"] = (
                "分母为 0："
                + ("快路径" if fast_rate == "n/a" else "")
                + ("/" if fast_rate == "n/a" and planned_rate == "n/a" else "")
                + ("规划路径" if planned_rate == "n/a" else "")
                + " 还没有可比的轮数（没有读数就不下结论）"
            )
        elif key == "fabricated" and fast["numerator"] > 0:
            item["verdict"] = "fail"
            item["why"] = "快路径出现无证据断言（硬线：假承诺/胡编 = 0）"
        elif float(fast_rate) <= float(planned_rate):
            item["verdict"] = "pass"
            item["why"] = "快路径不高于规划路径"
        else:
            item["verdict"] = "fail"
            item["why"] = "快路径高于规划路径"
        out.append(item)
    return out


def overall(verdict_rows: Sequence[Mapping[str, Any]]) -> str:
    """三指标合成一句话：任一 ``fail`` → ``fail``；全 ``pass`` → ``pass``；否则 ``undetermined``。"""
    labels = {str(item.get("verdict") or "") for item in verdict_rows}
    if "fail" in labels:
        return "fail"
    if labels == {"pass"} and verdict_rows:
        return "pass"
    return "undetermined"


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def build_readout(
    *,
    decision_trace: str = DEFAULT_DECISION_TRACE,
    meta_db: str = DEFAULT_META_DB,
    since: str = "",
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> dict[str, Any]:
    """读通道 → 算读数 → 组装**只含计数**的产物（JSON 里没有用户原文）。"""
    records, malformed = read_jsonl(decision_trace)
    rows = path_rows(records)
    corrections, corrections_note = load_corrections(meta_db, since=since)
    if since:
        rows = [row for row in rows if str(row.get("ts") or "") >= since]
        records = [row for row in records if str(row.get("ts") or "") >= since]
    reading = aggregate(rows, corrections, records, window_seconds=window_seconds)
    verdict_rows = verdicts(reading)
    return {
        "inputs": {
            "decision_trace": decision_trace,
            "decision_trace_exists": os.path.exists(decision_trace),
            "decision_trace_records": len(records),
            "decision_trace_malformed": int(malformed),
            "meta_db": meta_db,
            "meta_db_note": corrections_note,
            "since": str(since or ""),
        },
        "reading": reading,
        "verdicts": verdict_rows,
        "overall": overall(verdict_rows),
        "note": (
            "分母为 0 的项一律 n/a（不编数字）；"
            "本脚本只给分子分母与按数字下的结论，读数不足时结论是 undetermined。"
        ),
    }


def print_report(readout: Mapping[str, Any]) -> None:
    """人读版（数字逐项带分子/分母，**不带任何用户原文**）。"""
    inputs = readout["inputs"]
    reading = readout["reading"]
    print("=" * 68)
    print("R-C7 两路质量对比读数（快路径 vs 规划路径）")
    print("=" * 68)
    print(f"  通道: {inputs['decision_trace']}"
          f"（存在={inputs['decision_trace_exists']}，"
          f"记录={inputs['decision_trace_records']}，坏行={inputs['decision_trace_malformed']}）")
    print(f"  meta: {inputs['meta_db']}"
          + (f"（{inputs['meta_db_note']}）" if inputs["meta_db_note"] else ""))
    print(f"  联接窗口: {reading['window_seconds']:.0f}s"
          f"；窗口内找不到路径记录的事件: {reading['join_unmatched']} 条")
    print()
    header = f"  {'路径':<10}{'轮数':>6}{'有计划的轮':>10}{'误解':>14}{'被纠正':>14}{'胡编':>14}"
    print(header)
    for path in PATHS:
        data = reading["paths"][path]
        cells = []
        for key in ("misread", "corrected", "fabricated"):
            pair = data[key]
            shown = rate(pair)
            cells.append(
                f"{pair['numerator']}/{pair['denominator']}"
                + (f"={shown}" if shown != "n/a" else "=n/a")
            )
        print(
            f"  {path:<10}{data['turns']:>6}{data['with_plan']:>10}"
            f"{cells[0]:>14}{cells[1]:>14}{cells[2]:>14}"
        )
    print()
    for item in readout["verdicts"]:
        print(f"  [{item['verdict']:<12}] {item['label']}："
              f"快路径 {item['fast_path']} vs 规划 {item['planned']} —— {item['why']}")
    print()
    print(f"  合成结论: {readout['overall']}")
    print(f"  {readout['note']}")
    print()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--decision-trace", default=DEFAULT_DECISION_TRACE,
                        help="decision_trace.jsonl 路径（只读）")
    parser.add_argument("--meta-db", default=DEFAULT_META_DB,
                        help="meta 库路径（只读，只查 feedback_samples）")
    parser.add_argument("--since", default="", help="只统计该 ISO 时间之后的记录")
    parser.add_argument("--window-seconds", type=float, default=DEFAULT_WINDOW_SECONDS,
                        help="质量事件贴到哪一轮的时间窗（秒）")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="把读数写到该 JSON 文件（'-' = 打到 stdout）")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.decision_trace and not os.path.exists(args.decision_trace):
        # 通道不存在是**输入问题**（不是"零读数"）：直接失败，免得把"没接通道"
        # 读成"没有问题"。
        print(f"[err] decision_trace 不存在：{args.decision_trace}", file=sys.stderr)
        return 1

    readout = build_readout(
        decision_trace=args.decision_trace,
        meta_db=args.meta_db,
        since=args.since,
        window_seconds=args.window_seconds,
    )
    if args.json_out == "-":
        print(json.dumps(readout, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_report(readout)
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as handle:
                json.dump(readout, handle, ensure_ascii=False, indent=2, sort_keys=True)
            print(f"  [json] 写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
