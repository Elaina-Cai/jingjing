"""计划器测试：要不要动手由代码判，动手才问「哪几步」。"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.jobs import planner as P  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok  {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


class FakeJob:
    def __init__(self, steps):
        self.job_id = "job-test"
        self.steps = steps


def run(coro):
    return asyncio.run(coro)


def with_mode(mode):
    old = features.mode

    def fake(flag, default=features.MODE_OFF):
        if flag == features.JOB_LEDGER:
            return mode
        return old(flag, default)

    features.mode = fake
    return old


def t_looks_like_task() -> None:
    check("动词句算任务", P.looks_like_task("在鲸鲸的测试群里说一句：今天下雨记得带伞"))
    check("查资料算任务", P.looks_like_task("帮我查一下杭州下周三天气"))
    check("说一句也算任务", P.looks_like_task("在鲸鲸的测试群里说一句：今天下雨记得带伞"))
    check("纯聊天不算", not P.looks_like_task("在吗"))
    check("太短不算", not P.looks_like_task("好"))
    check("空串不算", not P.looks_like_task(""))


def t_parse() -> None:
    good = '{"act": true, "goal": "发一句话到群里", "steps": [{"action": "send:鲸鲸的测试群|今天下雨记得带伞", "effects": "external_send"}]}'
    plan = P.parse_planner_json(good)
    check("合法 JSON 可解析", plan is not None and plan["steps"][0]["effects"] == "external_send", str(plan))
    check("act=false 不开事", P.parse_planner_json('{"act": false}') is None)
    check("坏 JSON 不开事", P.parse_planner_json("我觉得应该发") is None)
    check("空 steps 不开事", P.parse_planner_json('{"act": true, "goal": "x", "steps": []}') is None)
    check("effects 非法不开事", P.parse_planner_json('{"act": true, "goal": "x", "steps": [{"action": "a", "effects": "root"}]}') is None)
    check("action 空不开事", P.parse_planner_json('{"act": true, "goal": "x", "steps": [{"action": "  "}]}') is None)
    check("围栏也能剥", P.parse_planner_json('```json\n{"act": true, "goal": "g", "steps": [{"action": "web_search:杭州"}]}\n```') is not None)
    check("n 按顺序补", (P.parse_planner_json('{"act": true, "goal": "g", "steps": [{"action": "a"}, {"action": "b"}]}') or {}).get("steps")[1]["n"] == 2)


def t_gate() -> None:
    old = with_mode(features.MODE_OFF)
    called: list[str] = []

    async def send(system, user):
        called.append(user)
        return '{"act": true, "goal": "g", "steps": [{"action": "web_search:杭州"}]}'

    try:
        job = run(P.maybe_open_job(user_text="帮我查杭州天气", chat_id="c1", send=send))
        check("off 档一次都不调", not called and job is None)
        check("off 档连动词表都不查", run(P.maybe_open_job(user_text="帮我查杭州天气", chat_id="", send=send)) is None)
    finally:
        features.mode = old


def t_open() -> None:
    old = with_mode(features.MODE_ON)
    # 2026-10-21：single_call_plan() 默认开——主路径是「一次模型输出里同时给话和
    # <plan>」，出口 _consume_plan_block 负责开事；再问一次计划器就是每回合两次
    # 模型点。这段测的是**回退档**，所以显式关掉那个开关才有对象可测。
    _old_scp = os.environ.get("YINYUE_SINGLE_CALL_PLAN")
    os.environ["YINYUE_SINGLE_CALL_PLAN"] = "0"
    opened: list[dict] = []

    class FakeStore:
        pass

    async def send_ok(system, user):
        return '{"act": true, "goal": "发到测试群", "steps": [{"action": "send:鲸鲸的测试群|今天下雨", "effects": "external_send"}]}'

    async def send_chat(system, user):
        return '{"act": false}'

    async def send_boom(system, user):
        raise RuntimeError("模型挂了")

    import core.jobs.ingest as ingest

    old_ingest = ingest.ingest_plan

    def fake_ingest(payload, **kw):
        opened.append(payload)
        return FakeJob(payload["plan"]["steps"])

    ingest.ingest_plan = fake_ingest
    try:
        job = run(P.maybe_open_job(user_text="在群里发一句下雨", chat_id="c1", send=send_ok, prime=False))
        check("问到了就开事", job is not None and opened and opened[0]["plan"]["goal"] == "发到测试群")
        check("开事走 ingest_plan", len(opened) == 1)
        job2 = run(P.maybe_open_job(user_text="在群里发一句下雨", chat_id="c1", send=send_chat, prime=False))
        check("act=false 不开事", job2 is None and len(opened) == 1)
        job3 = run(P.maybe_open_job(user_text="帮我查天气", chat_id="c1", send=send_boom, prime=False))
        check("模型炸了不开事也不抛", job3 is None and len(opened) == 1)
        called: list[str] = []

        async def send_spy(system, user):
            called.append(user)
            return "{}"

        run(P.maybe_open_job(user_text="我们随便聊聊吧", chat_id="c1", send=send_spy))
        check("不像任务：只问一次语义、不问步骤", called == [P.build_classify_prompt(user_text="我们随便聊聊吧")] or len(called) == 1)
        check("prompt 里带上了原话", "send:会话|正文" in P.build_prompt(user_text="x"))
    finally:
        features.mode = old
        ingest.ingest_plan = old_ingest
        if _old_scp is None:
            os.environ.pop("YINYUE_SINGLE_CALL_PLAN", None)
        else:
            os.environ["YINYUE_SINGLE_CALL_PLAN"] = _old_scp


def t_static() -> None:
    src = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8-sig")
    check("接线点在 _send_reply_impl 里", "await _planner.maybe_open_job(" in src)
    check("有 _had_plan 去重", "_had_plan = True" in src)
    check("计划器失败只告警", "job_planner_wiring_failed" in src)
    mod = (ROOT / "core/jobs/planner.py").read_text(encoding="utf-8-sig")
    check("计划器不 import 传输层", "wx_sender" not in mod and "send_and_verify" not in mod)
    check("计划器不自己写台账", ".create(" not in mod)


async def t_semantic_gate():
    async def reply(text):
        async def send(system, user):
            reply.seen = (system, user)
            return text
        return send

    for raw, want, label in (("yes", True, "yes"), ("YES.", True, "YES."), ("no", False, "no"), ("可能是吧", False, "垃圾→不办")):
        send = await reply(raw)
        got = await P.classify_task(user_text="随便聊聊今天天气", send=send)
        check("语义兜底 " + label, got is want, repr(raw))
    send = await reply("no")
    await P.classify_task(user_text="x", send=send)
    sys_text, user_text = reply.seen
    check("分类提示词只说 yes/no", sys_text == P.CLASSIFY_SYSTEM and "yes 或 no" in user_text)
    from pathlib import Path as _Path
    src = _Path(P.__file__).read_text(encoding="utf-8")
    check("动词表只是短路（先 looked_like 再兜底）", "by_verbs = looks_like_task" in src and "classify_task(user_text=user_text" in src)
    check("兜底只调一次模型", "raw = await caller(CLASSIFY_SYSTEM" not in src)



def main() -> int:
    print("== 计划器 ==")
    for fn in (t_looks_like_task, t_parse, t_gate, t_open, t_static):
        fn()
    run(t_semantic_gate())
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
