from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import plan_block
from core.tools import builtin

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool) -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}")


def t_spec() -> None:
    specs = {s.name: s for s in builtin.all_specs()}
    check("send_plan 在清单里", "send_plan" in specs)
    spec = specs["send_plan"]
    check("effects 是 external_send", spec.effects == frozenset({builtin.Effect.EXTERNAL_SEND}))
    check("有 target/text 两个参数", set(spec.args) == {"target", "text"})


def t_bound() -> None:
    reg = builtin.build_registry()
    check("注册表里有 send_plan", "send_plan" in reg._specs)
    check("绑的是真 handler", reg._specs["send_plan"] is not None)


def t_missing_args() -> None:
    out = builtin.send_plan_handler(target="", what="", params={})
    check("缺参数 → blocked", out.status == "blocked")
    check("缺参数不写台账", "不能发" in (out.content or ""))


def t_opens_job_only() -> None:
    seen: dict[str, object] = {}

    def fake_consume(plan, *, chat_id, store=None, trace_id=""):
        seen["plan"] = plan
        seen["chat_id"] = chat_id
        return "job-test123"

    real = plan_block.consume
    plan_block.consume = fake_consume
    try:
        out = builtin.send_plan_handler(target="鲸鲸的测试群", what="今天下雨记得带伞", params={"chat_id": "c1"})
    finally:
        plan_block.consume = real
    check("status=ok", out.status == "ok")
    check("说的是开成一件", "已开成一件" in (out.content or ""))
    check("明说一个字都没发", "一个字都没发" in (out.content or ""))
    plan = seen.get("plan") or {}
    check("动作是 send:目标|正文", plan["steps"][0]["action"] == "send:鲸鲸的测试群|今天下雨记得带伞")
    check("goal 不带外壳", "goal" in plan and "plan" not in plan)
    check("chat_id 传了进来", seen.get("chat_id") == "c1")


def t_no_direct_delivery() -> None:
    src = (ROOT / "core/tools/builtin.py").read_text(encoding="utf-8")
    i = src.find("def send_plan_handler")
    body = src[i : i + 2000]
    for bad in ("send_and_verify", "dispatchMouseEvent", "press_enter", "gateway.send"):
        check(f"handler 里没有 {bad}", bad not in body)


def t_slot_ask() -> None:
    """缺参数时先问一句，不开事（槽决定问句，不由说法决定）。"""
    r = builtin.send_plan_handler(target="鲸鲸的测试群", what="到点提醒我")
    check("缺时间就问什么时候", "什么时候" in r.content, r.content)
    r2 = builtin.send_plan_handler(target="鲸鲸的测试群", what="今晚九点提醒我吃药")
    check("时间齐了就不问", "什么时候" not in r2.content, r2.content)
    check("槽抽取坏了自己不拦路", "我还得知道" not in builtin.send_plan_handler(target="群", what="在").content)


def main() -> int:
    for fn in (t_spec, t_bound, t_missing_args, t_opens_job_only, t_no_direct_delivery):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
