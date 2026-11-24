"""直接下令：句子本身就点了名 → 程序化开一件事，不等模型自觉。

真机 2026-10-21：三个回合 `step=judge note=calls=0`，她只会解释「我按不到发送键」。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import direct  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name + ((" :: " + str(detail)) if detail else ""))


def t_patterns() -> None:
    check("在…里说一句：", direct.parse_order("在鲸鲸的测试群里说一句：今天下雨记得带伞") == ("鲸鲸的测试群", "今天下雨记得带伞"))
    check("往…发一句", direct.parse_order("往幸福一家人发一句：晚上我晚点回") == ("幸福一家人", "晚上我晚点回"))
    check("跟…说", direct.parse_order("跟 Elaina 说：早点睡别熬夜") == ("Elaina", "早点睡别熬夜"))
    check("带引号也能认", direct.parse_order("在「鲸鲸的测试群」里说一句：今天下雨记得带伞") == ("鲸鲸的测试群", "今天下雨记得带伞"))


def t_reject() -> None:
    check("聊天不认", direct.parse_order("在吗") is None)
    check("空串不认", direct.parse_order("") is None)
    check("正文太短不认", direct.parse_order("在群里说一句好") is None)
    check("没有对象不认", direct.parse_order("说一句：今天下雨记得带伞") is None)


def t_open_direct() -> None:
    from core.jobs import ingest as ing

    seen: dict = {}
    old = ing.ingest_plan
    ing.ingest_plan = lambda payload, **kw: (seen.update(payload=payload, kw=kw) or SimpleNamespace(job_id="job-d1"))
    try:
        jid = direct.open_direct(
            "在鲸鲸的测试群里说一句：今天下雨记得带伞",
            chat_id="c1",
            goal_id="direct:g1",
        )
        check("开出 job", jid == "job-d1", jid)
        step = seen["payload"]["plan"]["steps"][0]
        check("动作是 send:目标|正文", step["action"] == "send:鲸鲸的测试群|今天下雨记得带伞", step)
        check("效果 external_send", step["effects"] == "external_send", step)
        check("chat_id 透传", seen["kw"].get("chat_id") == "c1", seen["kw"])
        check("goal_id 透传", seen["kw"].get("goal_id") == "direct:g1", seen["kw"])
        seen.clear()
        check("聊天不开事", direct.open_direct("在吗", chat_id="c1") == "" and not seen)
    finally:
        ing.ingest_plan = old


def t_trace_unique() -> None:
    """同一句话第二次说，必须开出**新**的事（不能撞上已 done 的旧事）。"""
    from core.jobs import ingest as ing

    traces: list[str] = []
    old = ing.ingest_plan
    ing.ingest_plan = lambda payload, **kw: (traces.append(kw.get("trace_id")) or SimpleNamespace(job_id=f"job-{len(traces)}"))
    try:
        direct.open_direct("在鲸鲸的测试群里说一句：今天下雨记得带伞", chat_id="c1")
        direct.open_direct("在鲸鲸的测试群里说一句：今天下雨记得带伞", chat_id="c1")
        check("两次的 trace_id 不同", len(set(traces)) == 2, traces)
        check("trace_id 非空", all(traces))
    finally:
        ing.ingest_plan = old


def t_fail_closed() -> None:
    from core.jobs import ingest as ing

    old = ing.ingest_plan

    def boom(*_a, **_k):
        raise RuntimeError("库不在")

    ing.ingest_plan = boom
    try:
        check("失败返回空串不抛", direct.open_direct("在群里说一句：今天下雨记得带伞", chat_id="c1") == "")
    finally:
        ing.ingest_plan = old


def t_hooked() -> None:
    src = (ROOT / "agents" / "conversational.py").read_text(encoding="utf-8")
    check("真实路径调了 direct", "from core.jobs import direct as _direct" in src)
    check("只在私聊用", 'not bool(_cx_extra.get("is_group", False)) and _cx_chat_id' in src)
    check("在 _job_tick 之前", src.index("_direct.open_direct(") < src.index("from core.routing.turn_pipeline import _job_tick"))
    check("记 job_direct_order", 'extra["job_direct_order"]' in src)
    # R2：契约定义已搬到 core/routing/extra_contract.py，改验运行时集合
    # （源码文本锁在这里本来就只是"这个字面量存在"，换成集合成员判定更硬）
    from core.routing.extra_contract import EXTRA_CONTRACT
    check("契约已登记", "job_direct_order" in EXTRA_CONTRACT)


def main() -> int:
    for fn in (t_patterns, t_reject, t_open_direct, t_trace_unique, t_fail_closed, t_hooked):
        print("\n" + fn.__name__)
        fn()
    print(f"\nRESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
