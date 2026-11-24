"""perceive 与 N1 / 路由决策的对账（只读报表，不进门禁，退出码恒 0）。

为什么要这张表：打开 `YINYUE_FEATURE_PERCEIVE_TOOL` 之前，必须知道两件事——
1. perceive 判的 `addressee` 与 N1 判的是不是同一个结论；
2. perceive 判的 `route` 与代码实际走的分支（回/不回/等）是不是同一个结论。
两条都对得上，才轮到"把 [本轮理解]/[读场] 删掉"这一步。

口径：
- N1 侧读 `logs/intent_trace.jsonl`（`evidence.addressee_question.who` / `need_clarify`）；
- 路由侧读 `logs/decision_trace.jsonl`（`kind` / `reason`）；
- perceive 侧读 `logs/perceive_trace.jsonl`（`agents.perceive.append_trace` 写的）；
- 三边按 `trace_id` 对齐；对不上的计入 unmatched，不当成不一致。

用法：
    python scripts/perceive_reconcile.py [--limit 2000] [--json]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"

#: 路由侧 kind → perceive 的三值
_KIND_TO_ROUTE: dict[str, str] = {
    "reply_now": "speak",
    "speak": "speak",
    "defer": "defer",
    "silent": "skip",
    "skip": "skip",
}

#: N1 的 addressee 读数是五值，perceive 是四值：room 之外没有 room/many 的差别，
#: 显式给出映射，**不靠猜**。
_WHO_TO_ADDRESSEE: dict[str, str] = {
    "her": "her",
    "many": "her",
    "other": "other",
    "room": "room",
    "unknown": "unknown",
}


def _rows(path: Path, *, limit: int = 2000) -> list[dict[str, Any]]:
    """读 JSONL 的尾巴（只读、坏行跳过、永不抛）。"""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001 - 坏行不该弄挂报表
                    continue
                # decision_trace 里有一行一个 **list** 的形态（一批 readout 打一行），
                # 不拍平就会整批被当成"坏行"跳过 —— 报表会永远显示 0/0。
                if isinstance(obj, list):
                    out.extend(item for item in obj if isinstance(item, dict))
                elif isinstance(obj, dict):
                    out.append(obj)
    except Exception:  # noqa: BLE001
        return []
    return out[-limit:]


def n1_addressee(row: dict[str, Any]) -> str:
    """从 N1 的一条 trace 里取 addressee 读数（取不到就空串）。"""
    evidence = row.get("evidence")
    candidates: list[Any] = []
    if isinstance(evidence, dict):
        candidates.append(evidence)
    elif isinstance(evidence, list):
        candidates.extend(item for item in evidence if isinstance(item, dict))
    for item in candidates:
        q = item.get("addressee_question")
        if isinstance(q, dict):
            who = str(q.get("who") or "").strip().lower()
            return _WHO_TO_ADDRESSEE.get(who, "unknown")
    who = str(row.get("addressee") or "").strip().lower()
    return _WHO_TO_ADDRESSEE.get(who, "") if who else ""


def route_of_decision(row: dict[str, Any]) -> str:
    """从决定台账里取这一轮实际走的分支。"""
    for key in ("kind", "action", "route"):
        value = str(row.get(key) or "").strip().lower()
        if value in _KIND_TO_ROUTE:
            return _KIND_TO_ROUTE[value]
    reason = str(row.get("reason") or "").strip().lower()
    for token, route in (("defer", "defer"), ("silent", "skip"), ("no_reply", "skip")):
        if token in reason:
            return route
    return ""


def _ts(value: Any) -> float:
    """ISO 时间 → epoch 秒（认不出返回 -1，不进配对）。"""
    raw = str(value or "").strip()
    if not raw:
        return -1.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return -1.0


def match_route(row: dict[str, Any], route_rows: list[dict[str, Any]], *, tolerance_s: float = 180.0) -> str:
    """按「同一会话、时间最近、容差内」给一条 perceive 样本配路由决策。

    为什么不用 trace_id：路由决策发生在回合建立**之前**（判成不回就没有回合、
    也没有 trace_id）。所以只能按 chat_id + 时间配对；配不上就空串（不进分母）。
    """
    # 配对顺序：event_id 精确 → trace_id → 同会话 + 时间最近。
    # event_id 是三条台账唯一共有的键（路由决策发生在回合建立之前，没有 trace_id）。
    event_id = str(row.get("event_id") or "")
    if event_id:
        for item in route_rows:
            if str(item.get("event_id") or "") == event_id:
                route = _KIND_TO_ROUTE.get(str(item.get("kind") or ""), "")
                if route:
                    return route
    # 再试 trace_id（有的台账会带）。
    tid = str(row.get("trace_id") or "")
    if tid:
        for item in route_rows:
            if tid in (str(item.get("trace_id") or ""), str(item.get("decision_trace_id") or "")):
                route = _KIND_TO_ROUTE.get(str(item.get("kind") or ""), "")
                if route:
                    return route
    chat_id = str(row.get("chat_id") or "")
    at = _ts(row.get("at"))
    if not chat_id or at < 0:
        return ""
    best: tuple[float, str] = (tolerance_s + 1.0, "")
    for item in route_rows:
        if str(item.get("chat_id") or "") != chat_id:
            continue
        delta = abs(_ts(item.get("at")) - at)
        if delta <= tolerance_s and delta < best[0]:
            best = (delta, _KIND_TO_ROUTE.get(str(item.get("kind") or ""), ""))
    return best[1]


def match_addressee(row: dict[str, Any], n1_by_event: dict[str, dict[str, Any]],
                    n1_by_id: dict[str, dict[str, Any]], *, tolerance_s: float = 180.0) -> str:
    """给一条 perceive 样本配 N1 的 addressee 读数：event_id → trace_id → 会话+时间。"""
    event_id = str(row.get("event_id") or "")
    if event_id and event_id in n1_by_event:
        who = n1_addressee(n1_by_event[event_id])
        if who:
            return who
    tid = str(row.get("trace_id") or "")
    if tid and tid in n1_by_id:
        who = n1_addressee(n1_by_id[tid])
        if who:
            return who
    chat_id = str(row.get("chat_id") or "")
    at = _ts(row.get("at"))
    if not chat_id or at < 0:
        return ""
    best: tuple[float, str] = (tolerance_s + 1.0, "")
    for item in n1_by_event.values():
        if str(item.get("chat_id") or "") != chat_id:
            continue
        delta = abs(_ts(item.get("at")) - at)
        if delta <= tolerance_s and delta < best[0]:
            best = (delta, n1_addressee(item))
    return best[1]


def reconcile(
    perceive_rows: Iterable[dict[str, Any]],
    n1_rows: Iterable[dict[str, Any]],
    decision_rows: Iterable[dict[str, Any]],
    route_rows: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """三边按 trace_id 对齐，算两项一致率与不一致样本。"""
    perceive_rows = list(perceive_rows)
    route_list = list(route_rows)
    n1_by_event: dict[str, dict[str, Any]] = {}
    for row in n1_rows:
        value = str(row.get("event_id") or "")
        if value:
            n1_by_event.setdefault(value, row)
    n1_by_id: dict[str, dict[str, Any]] = {}
    for row in n1_rows:
        for key in ("decision_trace_id", "trace_id"):
            value = str(row.get(key) or "")
            if value:
                n1_by_id.setdefault(value, row)
    # 路由台账里同一行有两个 id：``trace_id``（这一轮）与 ``decision_trace_id``
    # （decision 自己那条）。perceive 落的是后者（核过 2026-10-23 真机样本），
    # 所以两个键都要建索引，否则永远对不上、报表恒 0/0。
    dec_by_id: dict[str, dict[str, Any]] = {}
    for row in decision_rows:
        for key in ("decision_trace_id", "trace_id"):
            value = str(row.get(key) or "")
            if value:
                dec_by_id.setdefault(value, row)
    totals = Counter()
    agree = Counter()
    samples: list[dict[str, Any]] = []
    for row in perceive_rows:
        tid = str(row.get("trace_id") or "")
        if not tid:
            totals["unmatched_trace_id"] += 1
            continue
        want = match_addressee(row, n1_by_event, n1_by_id)
        got = str(row.get("addressee") or "")
        if True:
            if want:
                totals["addressee"] += 1
                if want == got:
                    agree["addressee"] += 1
                elif len(samples) < 20:
                    samples.append({"trace_id": tid, "field": "addressee", "n1": want, "perceive": got})
        want_route = match_route(row, route_list)
        got_route = str(row.get("route") or "")
        if True:
            if want_route:
                totals["route"] += 1
                if want_route == got_route:
                    agree["route"] += 1
                elif len(samples) < 20:
                    samples.append({"trace_id": tid, "field": "route", "code": want_route, "perceive": got_route})
    report: dict[str, Any] = {"samples": len(list(perceive_rows)) if isinstance(perceive_rows, list) else None,
                              "totals": dict(totals), "agree": dict(agree), "mismatches": samples}
    for field in ("addressee", "route"):
        total = totals.get(field, 0)
        report[f"{field}_rate"] = round(agree.get(field, 0) / total, 4) if total else None
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="perceive vs N1 / 路由 对账（只读）")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = reconcile(
        _rows(LOGS / "perceive_trace.jsonl", limit=args.limit),
        _rows(LOGS / "intent_trace.jsonl", limit=args.limit),
        _rows(LOGS / "decision_trace.jsonl", limit=args.limit),
        _rows(LOGS / "route_trace.jsonl", limit=args.limit),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print("perceive 对账（只读）")
    print(f"  addressee 一致率：{report['addressee_rate']}（{report['agree'].get('addressee', 0)}/{report['totals'].get('addressee', 0)}）")
    print(f"  route 一致率    ：{report['route_rate']}（{report['agree'].get('route', 0)}/{report['totals'].get('route', 0)}）")
    if not report["totals"]:
        print("  （还没有 perceive 样本：把 YINYUE_FEATURE_PERCEIVE_TOOL=1 跑几轮再回来看）")
    for row in report["mismatches"]:
        print(f"  - {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())