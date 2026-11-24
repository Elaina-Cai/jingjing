"""perceive 对账口径（只读报表）的契约。

锁的是口径本身：N1 五值怎么映射到 perceive 四值、路由 kind 怎么映射成三值、
对不上的样本不进分母、缺文件/坏行不许抛。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from _tempdirs import temp_base

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("pr", ROOT / "scripts" / "perceive_reconcile.py")
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok " if ok else "FAIL ") + name + ("" if ok else f" {detail}"))
    if not ok:
        FAILS.append(name)


def t_who_mapping() -> None:
    rows = [
        {"evidence": {"addressee_question": {"who": "her"}}},
        {"evidence": [{"addressee_question": {"who": "many"}}]},
        {"evidence": {"addressee_question": {"who": "other"}}},
        {"evidence": {"addressee_question": {"who": "room"}}},
    ]
    got = [pr.n1_addressee(r) for r in rows]
    check("N1 五值 → perceive 四值", got == ["her", "her", "other", "room"], str(got))
    check("取不到就是空串（不进分母）", pr.n1_addressee({"evidence": []}) == "")
    check("认不出的 who 记 unknown（不猜 other）",
          pr.n1_addressee({"evidence": {"addressee_question": {"who": "??"}}}) == "unknown")


def t_route_mapping() -> None:
    check("reply_now → speak", pr.route_of_decision({"kind": "reply_now"}) == "speak")
    check("silent → skip", pr.route_of_decision({"kind": "silent"}) == "skip")
    check("defer → defer", pr.route_of_decision({"kind": "defer"}) == "defer")
    check("只给 reason 也能读", pr.route_of_decision({"reason": "group_deferred_to_state"}) == "defer")
    check("认不出就空串", pr.route_of_decision({"kind": "???"}) == "")


def t_rates_and_unmatched() -> None:
    perceive = [
        {"trace_id": "t1", "route": "speak", "addressee": "her"},
        {"trace_id": "t2", "route": "skip", "addressee": "unknown"},
        {"trace_id": "t9", "route": "speak", "addressee": "her"},  # 三边都没有
    ]
    n1 = [
        {"trace_id": "t1", "evidence": {"addressee_question": {"who": "her"}}},
        {"trace_id": "t2", "evidence": {"addressee_question": {"who": "her"}}},
    ]
    dec = [
        {"trace_id": "t1", "kind": "reply_now"},
        {"trace_id": "t2", "kind": "reply_now"},
    ]
    rep = pr.reconcile(perceive, n1, dec, dec)
    check("addressee 分母只算对得上的", rep["totals"]["addressee"] == 2, str(rep["totals"]))
    check("addressee 一致率 1/2", rep["addressee_rate"] == 0.5, str(rep["addressee_rate"]))
    check("route 一致率 1/2", rep["route_rate"] == 0.5, str(rep["route_rate"]))
    check("不一致样本有记录", len(rep["mismatches"]) >= 2, str(rep["mismatches"]))
    check("三边都没有的样本不算不一致", all(m["trace_id"] != "t9" for m in rep["mismatches"]))


def t_files_tolerant() -> None:
    # 临时目录只许走仓库唯一入口（tests/_tempdirs.py），裸 tempfile 会被
    # tests/test_media_cache_and_mime.py 的件③守卫抓出来。
    tmp = temp_base() / "perceive_reconcile"
    tmp.mkdir(parents=True, exist_ok=True)
    missing = tmp / "nope.jsonl"
    check("缺文件返回空", pr._rows(missing) == [])
    bad = tmp / "bad.jsonl"
    bad.write_text('{"a":1}\nnot json\n\n[1,2]\n', encoding="utf-8")
    rows = pr._rows(bad)
    check("坏行与非法 JSON 被跳过", rows == [{"a": 1}], str(rows))
    check("主入口退出码恒 0（report-only）", pr.main(["--json"]) == 0)



def t_route_join_by_chat_and_time() -> None:
    """路由决策发生在回合建立之前（没有 trace_id），只能按会话 + 时间最近配对。"""
    perceive = [{"trace_id": "x", "chat_id": "c1", "at": "2026-10-23T14:15:13", "route": "speak"}]
    routes = [
        {"chat_id": "c1", "at": "2026-10-23T14:13:00", "kind": "silent"},
        {"chat_id": "c1", "at": "2026-10-23T14:15:10", "kind": "reply_now"},
        {"chat_id": "c2", "at": "2026-10-23T14:15:12", "kind": "silent"},
    ]
    rep = pr.reconcile(perceive, [], [], routes)
    check("按同会话最近一条配对（不是更早那条）", rep["route_rate"] == 1.0, str(rep))
    far = [{"chat_id": "c1", "at": "2026-10-23T10:00:00", "kind": "reply_now"}]
    rep2 = pr.reconcile(perceive, [], [], far)
    check("超出容差不硬凑（不进分母）", rep2["totals"].get("route", 0) == 0, str(rep2["totals"]))
    check("match_route 对不上返回空串", pr.match_route(perceive[0], []) == "")


def t_event_id_beats_time() -> None:
    """event_id 精确优先：哪怕时间窗里有别的消息，也认 event_id 那条。"""
    perceive = [
        {"trace_id": "x", "event_id": "e1", "chat_id": "c1",
         "at": "2026-10-23T14:15:13", "route": "speak", "addressee": "her"},
    ]
    n1 = [
        # 同会话、时间更近，但 event_id 不同 —— 不许被它抢走
        {"event_id": "e2", "chat_id": "c1", "at": "2026-10-23T14:15:12",
         "evidence": {"addressee_question": {"who": "other"}}},
        {"event_id": "e1", "chat_id": "c1", "at": "2026-10-23T14:15:00",
         "evidence": {"addressee_question": {"who": "her"}}},
    ]
    routes = [{"event_id": "e1", "chat_id": "c1", "at": "2026-10-23T14:15:05", "kind": "reply_now"}]
    rep = pr.reconcile(perceive, n1, [], routes)
    check("addressee 按 event_id 配对（不是时间最近）", rep["addressee_rate"] == 1.0, str(rep))
    check("route 也按 event_id 配对", rep["route_rate"] == 1.0, str(rep))

def main() -> int:
    t_who_mapping()
    t_route_mapping()
    t_rates_and_unmatched()
    t_files_tolerant()
    t_route_join_by_chat_and_time()
    t_event_id_beats_time()
    print(f"\nRESULT {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())