"""群成员名单「只列相关人」契约。

锁三件事：优先级顺序（发言人 > 被引用 > @）、去重保序、最多 4 人；
一个都没命中时兜底按活跃度列 2 人并**明确告诉模型"其余没列"**。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context.member_scope import MAX_BLOCK_CHARS, relevant_names, select_rows  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok " if ok else "FAIL ") + name + ("" if ok else f" {detail}"))
    if not ok:
        FAILS.append(name)


def t_priority_order() -> None:
    extra = {
        "sender_name": "P7",
        "quoted_speaker": "P5",
        "mentions": ["小A"],
        "referent": {"name": "小B"},
    }
    names = relevant_names(extra)
    check("发言人排第一", names[0] == "P7", str(names))
    check("被引用的人排第二", names[1] == "P5", str(names))
    check("去重保序", len(names) == len(set(names)), str(names))
    check("四类来源都在（发言人/引用/指代/提及）", names == ["P7", "P5", "小B", "小A"], str(names))


def t_dedupe() -> None:
    extra = {"sender_name": "P7", "quoted_speaker": "P7", "mentions": ["P7", "P5"]}
    names = relevant_names(extra)
    check("重复名字只留一次", names == ["P7", "P5"], str(names))


def t_empty_extra() -> None:
    check("没有 extra 不抛且为空", relevant_names(None) == [])


def t_select_rows() -> None:
    rows = [("P7", "member", "wxid_a"), ("P5", "member", "wxid_b"), ("张小三", "member", "wxid_c")]
    picked, skipped = select_rows(rows, ["P5"])
    check("只挑命中的那个人", [r[0] for r in picked] == ["P5"], str(picked))
    check("没超预算就没有漏列", skipped == 0, str(skipped))
    fallback, _ = select_rows(rows, [])
    check("没人命中时兜底列 2 人（按活跃度）", [r[0] for r in fallback] == ["P7", "P5"], str(fallback))

    # 不设人数上限：@ 了 6 个人就进 6 个
    six = [(f"人{i}", "member", f"wxid_{i}") for i in range(6)]
    picked6, skipped6 = select_rows(six, [f"人{i}" for i in range(6)])
    check("@ 了 6 个人就列 6 个（不按人数硬截）", len(picked6) == 6, str(len(picked6)))
    check("6 个人不触发漏列", skipped6 == 0, str(skipped6))

    # 超字数预算才截，且必须报告漏了几个
    many = [(f"第{i}位成员", "member", f"wxid_{i}") for i in range(60)]
    picked_many, skipped_many = select_rows(
        many, [f"第{i}位成员" for i in range(60)], detail_len=lambda row: 40
    )
    check("超预算时确实截短", len(picked_many) < 60, str(len(picked_many)))
    check("被截掉的数如实报出来", skipped_many == 60 - len(picked_many), f"{skipped_many} vs {60 - len(picked_many)}")
    check("预算本身是字数不是人数", MAX_BLOCK_CHARS > 0)


def t_block_cap_is_wired() -> None:
    import inspect

    import agents.conversational as c

    src = inspect.getsource(c.ConversationalAgent._build_system_prompt_from_snapshot)
    check("名单块走 relevant_names/select_rows", "relevant_names(" in src and "select_rows(" in src)
    check("块里明确写了「其余成员没列出」", "其余成员没列出" in src)
    check("超预算时报告漏列人数", "位相关的人没列出" in src)
    check("块里让模型去调 get_person_profile", "get_person_profile(name=" in src)


def main() -> int:
    t_priority_order()
    t_dedupe()
    t_empty_extra()
    t_select_rows()
    t_block_cap_is_wired()
    print(f"\nRESULT {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())