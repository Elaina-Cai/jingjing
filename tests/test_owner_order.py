"""「主人自己点名的动作不再问一遍」的规则与接线。

真机 2026-10-21 业主原话：「我都在群里说了还要她说什么」。
规则：只放行 external_send / write_local，且正文必须逐字出现在主人这句话里；
ui_control 永不自动放行。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import policy  # noqa: E402

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


def step(action: str, effects: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(n=1, action=action, effects=effects)


def t_dictated() -> None:
    s = step("send:鲸鲸的测试群|今天下雨记得带伞", ("external_send",))
    check("主人说了这句 → 授权", policy.is_owner_ordered(s, "帮我在群里说一句：今天下雨记得带伞") is True)


def t_not_dictated() -> None:
    s = step("send:鲸鲸的测试群|今天下雨记得带伞", ("external_send",))
    check("主人没说过这句 → 仍要问", policy.is_owner_ordered(s, "在吗") is False)
    check("只说了目标没说正文 → 仍要问", policy.is_owner_ordered(s, "发到鲸鲸的测试群") is False)


def t_short_payload() -> None:
    s = step("send:群|好", ("external_send",))
    check("正文太短（<4 字）不认", policy.is_owner_ordered(s, "帮我说一句好") is False)


def t_ui_never() -> None:
    s = step("gui_click:点一下发送", ("ui_control",))
    check("界面步永不自动放行", policy.is_owner_ordered(s, "点一下发送") is False)


def t_read_never() -> None:
    s = step("gui_state:微信", ("read",))
    check("只读步用不着这条", policy.is_owner_ordered(s, "看看微信") is False)


def t_no_pipe() -> None:
    s = step("send:只有目标", ("external_send",))
    check("没有正文段 → 不认", policy.is_owner_ordered(s, "发到只有目标") is False)


def t_write_local() -> None:
    s = step("write:notes/今天.md|今天下雨记得带伞", ("write_local",))
    check("write_local 也算点名", policy.is_owner_ordered(s, "记一下：今天下雨记得带伞") is True)


def t_wiring() -> None:
    src = (ROOT / "core" / "routing" / "turn_pipeline.py").read_text(encoding="utf-8")
    check("有自动批准助手", "def _approve_owner_ordered(" in src)
    check("只在私聊调用", 'if mode == features.MODE_ON and approval_text and is_private:' in src)
    check("记 owner_ordered", '"owner_ordered"' in src or "who=\"owner_ordered\"" in src)
    check("留日志", "job_approved_by_order" in src)
    check("失败只警告", "job_order_approve_failed" in src)
    body = src[src.index("def _approve_owner_ordered(") : src.index("def _job_tick(")]
    check("先看是否在等批准", '"approval"' in body)
    check("用 is_owner_ordered", "is_owner_ordered(step, text)" in body)


def t_helper_runs() -> None:
    """真的走一遍：假 job 在等批准 + 主人点名 → 记一条批准。"""
    from core.routing import turn_pipeline as tp
    from core.jobs import approval as ap

    recorded: list[object] = []
    old_record = ap.record_approval
    ap.record_approval = lambda job, row, store=None: (recorded.append(row), True)[1]
    try:
        job = SimpleNamespace(
            job_id="job-x",
            awaiting=SimpleNamespace(kind="approval"),
            next_step=lambda: step("send:群|今天下雨记得带伞", ("external_send",)),
        )
        store = SimpleNamespace(open_jobs=lambda chat_id: [job])
        got = tp._approve_owner_ordered(store, chat_id="c1", text="帮我说一句：今天下雨记得带伞")
        check("记了一条批准", got == ["job-x#1"], got)
        check("批准携带 owner_ordered", bool(recorded) and getattr(recorded[0], "who", "") == "owner_ordered")
        recorded.clear()
        got2 = tp._approve_owner_ordered(store, chat_id="c1", text="在吗")
        check("没点名 → 一条都不记", got2 == [] and recorded == [], got2)
    finally:
        ap.record_approval = old_record


def main() -> int:
    for fn in (
        t_dictated,
        t_not_dictated,
        t_short_payload,
        t_ui_never,
        t_read_never,
        t_no_pipe,
        t_write_local,
        t_wiring,
        t_helper_runs,
    ):
        print("\n" + fn.__name__)
        fn()
    print(f"\nRESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
