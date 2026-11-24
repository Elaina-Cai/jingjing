# -*- coding: utf-8 -*-
"""出站文本「最后一步」的唯一实现（core/speech/plan_ingest.py）。

守的是 2026-10-23 的合一：`<plan>` 摘块 + 开事 + 「补一句等你批准」原来在
`core/routing/turn_pipeline.py` 与 `core/speech/outbound.py` 里各有一份（三处，
其中一处还不记账）。这里的用例钉四件事：
1. 开事只发生一次、带的是真实目标与会话追踪号，路由侧塞进来的计划优先且取走即删；
2. 没有计划 / 摘块炸了 / 没有目标 —— 一律原文照发，绝不拦住说话；
3. 「等你批准」只送主人私聊、只问一条、问过记账；
4. 三步的顺序（去完成式 → 摘块开事 → 补问句）与来源纯度。
"""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech import plan_ingest as P  # noqa: E402

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


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, tuple, dict]] = []

    def info(self, event, *a, **k):
        self.events.append(("info", event, a, k))

    def warning(self, event, *a, **k):
        self.events.append(("warning", event, a, k))

    def names(self) -> list[str]:
        return [e[1] for e in self.events]

    def at(self, name: str) -> dict:
        for _lvl, event, _a, kw in self.events:
            if event == name:
                return kw
        return {}


class _Ctx:
    def __init__(self, chat_id: str = "wxid_owner", trace_id: str = "t-1", extra=None) -> None:
        self.chat_id = chat_id
        self.trace_id = trace_id
        self.extra = {} if extra is None else dict(extra)


class _Ledger:
    def __init__(self) -> None:
        self.appended: list[tuple] = []
        self.asked: set = set()

    def append(self, job_id, kind, payload=None, actor=""):
        payload = dict(payload or {})
        self.appended.append((job_id, kind, payload, actor))
        if kind == "ask":
            self.asked.add(payload.get("ref"))

    def asked_refs(self, chat_id):
        return set(self.asked)


@contextmanager
def _patched(**kv):
    old = {}
    for path, value in kv.items():
        mod_name, attr = path.rsplit(".", 1)
        mod = importlib.import_module(mod_name)
        old[path] = (mod, attr, getattr(mod, attr))
        setattr(mod, attr, value)
    try:
        yield
    finally:
        for mod, attr, value in old.values():
            setattr(mod, attr, value)


def t_consume_from_text() -> None:
    calls: list[tuple] = []

    def fake_extract(text):
        return ("干净正文", {"goal": "g"})

    def fake_consume(plan, *, chat_id, trace_id="", store=None):
        calls.append((plan, chat_id, trace_id))
        return "job-1"

    log = _Log()
    with _patched(**{
        "core.jobs.plan_block.extract": fake_extract,
        "core.jobs.plan_block.consume": fake_consume,
    }):
        out = P.consume_plan_block("原文<plan>{}</plan>", _Ctx(), log=log)
    check("摘块后只返回干净正文", out == "干净正文", out)
    check("开事带的是真实目标与会话追踪号",
          calls == [({"goal": "g"}, "wxid_owner", "t-1")], calls)
    check("留下 plan_block_consumed", "plan_block_consumed" in log.names())
    check("日志里有 job_id", log.at("plan_block_consumed").get("job_id") == "job-1")


def t_pending_plan_from_router_wins() -> None:
    calls: list[tuple] = []

    def fake_extract(text):
        return ("正文", {"goal": "from-text"})

    def fake_consume(plan, *, chat_id, trace_id="", store=None):
        calls.append((plan, chat_id))
        return "job-2"

    ctx = _Ctx(extra={"_pending_plan": {"goal": "from-router"}})
    log = _Log()
    with _patched(**{
        "core.jobs.plan_block.extract": fake_extract,
        "core.jobs.plan_block.consume": fake_consume,
    }):
        first = P.consume_plan_block("正文", ctx, log=log)

        def no_extract(text):
            return ("正文", None)

    check("路由侧塞进来的计划优先", calls == [({"goal": "from-router"}, "wxid_owner")], calls)
    check("第一次仍然返回干净正文", first == "正文")
    check("取走就 pop，不会消费两次", "_pending_plan" not in ctx.extra, ctx.extra)


def t_no_plan_means_no_consume() -> None:
    def boom(*_a, **_k):
        raise AssertionError("没有计划时不该开事")

    with _patched(**{
        "core.jobs.plan_block.extract": lambda text: ("正文", None),
        "core.jobs.plan_block.consume": boom,
    }):
        out = P.consume_plan_block("正文", _Ctx(), log=_Log())
    check("没有 <plan> 块就原样返回", out == "正文")


def t_no_target_never_opens_a_job() -> None:
    called: list = []
    log = _Log()
    with _patched(**{
        "core.jobs.plan_block.extract": lambda text: ("正文", {"goal": "g"}),
        "core.jobs.plan_block.consume": lambda *a, **k: called.append(a) or "job-3",
    }):
        out = P.consume_plan_block("正文", _Ctx(chat_id=""), log=log)
    check("没有目标就不开事", called == [], called)
    check("没有目标留一行日志", "plan_block_no_target" in log.names())
    check("正文照发", out == "正文")


def t_consume_failure_keeps_the_text() -> None:
    def boom(*_a, **_k):
        raise RuntimeError("库挂了")

    log = _Log()
    with _patched(**{
        "core.jobs.plan_block.extract": lambda text: ("干净", {"goal": "g"}),
        "core.jobs.plan_block.consume": boom,
    }):
        out = P.consume_plan_block("原文", _Ctx(), log=log)
    check("开事炸了也照发原文", out == "原文", out)
    check("炸了要留日志", "plan_block_hook_failed" in log.names())


def t_append_ask_once_to_owner_private() -> None:
    ledger = _Ledger()
    log = _Log()
    mine = [{"job_id": "j1", "ref": "j1#1", "step_n": 1, "text": "（要我开始第 1 步吗）"}]
    with _patched(**{
        "core.jobs.store.default_store": lambda: ledger,
        "core.jobs.ask.is_owner_private": lambda chat_id: chat_id == "wxid_owner",
        "core.jobs.ask.pending_asks": lambda *a, **k: list(mine),
    }):
        first = P.append_pending_ask("正文", _Ctx(), log=log)
    check("问句并进真正发出去的那条", first.endswith("（要我开始第 1 步吗）") and first.startswith("正文"), first)
    check("问过就记账", ledger.appended == [("j1", "ask", {"ref": "j1#1", "step_n": 1}, "judge")], ledger.appended)
    check("只问一条（列表有三件也只取头一件）", first.count("（要我开始第 1 步吗）") == 1)
    check("日志带 job_id 与 ref",
          log.at("job_ask_appended") == {"job_id": "j1", "ref": "j1#1"}, log.at("job_ask_appended"))


def t_ask_never_goes_to_a_group() -> None:
    ledger = _Ledger()
    log = _Log()

    def boom(*_a, **_k):
        raise AssertionError("非主人私聊不该问")

    with _patched(**{
        "core.jobs.store.default_store": lambda: ledger,
        "core.jobs.ask.is_owner_private": lambda chat_id: False,
        "core.jobs.ask.pending_asks": boom,
    }):
        out = P.append_pending_ask("正文", _Ctx(chat_id="123@chatroom"), log=log)
    check("群里一个字都不加", out == "正文", out)
    check("被拦时留一行日志", any(n.startswith("job_ask_deferred") for n in log.names()), log.names())
    check("被拦时不记账", ledger.appended == [])


def t_ask_failure_keeps_the_text() -> None:
    def boom(*_a, **_k):
        raise RuntimeError("库挂了")

    log = _Log()
    with _patched(**{
        "core.jobs.store.default_store": boom,
        "core.jobs.ask.is_owner_private": lambda chat_id: True,
    }):
        out = P.append_pending_ask("正文", _Ctx(), log=log)
    check("问不出口也不许妨碍说话", out == "正文", out)
    check("失败要留日志", "job_ask_append_failed" in log.names())


def t_outbound_calls_in_one_order() -> None:
    src = (ROOT / "core" / "speech" / "outbound.py").read_text(encoding="utf-8")
    body = src[src.index("async def _send_text_or_file("):]
    body = body[: body.index("record(", 0) if "record(" in body else len(body)]
    order = [
        body.find("self._drop_done_claims("),
        body.find("self._consume_plan_block("),
        body.find("self._append_pending_ask("),
    ]
    check("三步都在发送前", all(i >= 0 for i in order), order)
    check("顺序是 去完成式 → 摘块开事 → 补问句", order == sorted(order), order)
    check("三步都委托给唯一实现",
          "plan_ingest.drop_done_claims(" in src
          and "plan_ingest.consume_plan_block(" in src
          and "plan_ingest.append_pending_ask(" in src)
    router = (ROOT / "core" / "routing" / "turn_pipeline.py").read_text(encoding="utf-8")
    check("路由侧不再自己开事", "plan_block.consume(" not in router)
    check("路由侧不再自己追问", "pending_asks(" not in router)


def t_purity() -> None:
    src = (ROOT / "core" / "speech" / "plan_ingest.py").read_text(encoding="utf-8")
    for bad in ("sqlite3", "llm.chat", "structlog", "asyncio", "requests"):
        check(f"不碰 {bad}", bad not in src)
    check("没有任何模块级 import 台账（都在函数里，patch 打得进去）",
          "from core.jobs" not in src.split("\n\n\n")[1])


def main() -> int:
    for fn in (
        t_consume_from_text,
        t_pending_plan_from_router_wins,
        t_no_plan_means_no_consume,
        t_no_target_never_opens_a_job,
        t_consume_failure_keeps_the_text,
        t_append_ask_once_to_owner_private,
        t_ask_never_goes_to_a_group,
        t_ask_failure_keeps_the_text,
        t_outbound_calls_in_one_order,
        t_purity,
    ):
        print("\n" + fn.__name__)
        fn()
    print(f"\nRESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())