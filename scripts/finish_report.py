"""按统一词表出报表：办成 / 办砸 / 过期 / 放弃（放弃再分 主人不要 与 办不下去）。

只读。用法：``python scripts/finish_report.py``

口径见方案文档 §12 与 ``core/contracts/finish.py``：
- 五个词：pending / done / failed / expired / abandoned
- ``abandoned`` 再分 cause：owner（主人叫停）/ stuck（办不下去）/ superseded（被顶掉）
- **半途而废率只算 stuck**；把 owner 也算进去会虚高（见 ``finish.stuck_rate`` 的说明）
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.contracts import finish as F  # noqa: E402


def _db() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "yinyue_meta.db")


def _rows(sql: str, args: tuple = ()) -> list[tuple]:
    path = _db()
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    try:
        return list(conn.execute(sql, args))
    except sqlite3.Error as exc:  # 表还没建就当没有
        print("  （跳过：%s）" % exc)
        return []
    finally:
        conn.close()


def main() -> int:
    print("== jobs（在手的事）==")
    rows = [(r[0], r[1]) for r in _rows("select state, updated_by from jobs")]
    states = sorted({st for st, _ in rows})
    states = [(st, sum(1 for s2, _ in rows if s2 == st)) for st in states]
    causes = [(st, by) for st, by in rows if F.from_job_state(st) == F.ABANDONED]
    codes = []
    for state, n in states:
        codes += [F.from_job_state(state)] * int(n)
    counts = F.count_by_finish(codes)
    total = sum(counts.values())
    print("  原始状态：", states)
    print("  统一词表：", counts, "共", total)
    for code in F.FINISH_CODES:
        n = counts.get(code, 0)
        print("    %-10s %3d  %5.1f%%" % (code, n, (100.0 * n / total) if total else 0.0))
    print("  放弃的原因：", F.count_by_abandon_cause(causes))
    print("  放弃率（含主人叫停）：%.1f%%" % (100.0 * F.abandoned_rate(counts)))
    print("  半途而废率（只算办不下去）：%.1f%%" % (100.0 * F.stuck_rate(counts, causes)))

    print("\n== work_items（台账）==")
    rows = [(r[0], r[1]) for r in _rows("select status, count(*) from work_items group by status")]
    print("  原始状态：", rows)
    codes = []
    for status, n in rows:
        codes += [F.from_work_item_status(status)] * int(n)
    print("  统一词表：", F.count_by_finish(codes))

    print("\n== 意图账本（世界模型事件流）==")
    try:
        from core.context import situation_store as st

        codes = []
        for key, model in st.world_models_on_disk().items():
            opened, closed = {}, {}
            for ev in model.events:
                payload = ev.payload or {}
                k = str(payload.get("key") or "")
                if not k:
                    continue
                if ev.kind == "intention_opened":
                    opened.setdefault(k, ev)
                elif ev.kind == "intention_closed":
                    closed[k] = ev
            for k in opened:
                if k in closed:
                    codes.append(F.from_intention("closed", (closed[k].payload or {}).get("result")))
                else:
                    codes.append(F.from_intention("open"))
        print("  统一词表：", F.count_by_finish(codes), "共", len(codes))
    except Exception as exc:  # 世界模型目录没配
        print("  （跳过：%s）" % exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
