"""阶段 3 第一刀：判能看到「在手的事」（只读侧，影子）。

这条测试守两件事：
1. **默认档零行为变化**——`JOB_LEDGER=off` 时 `_job_tick` 连台账都不打开；
2. **影子不发消息、不改 prompt**——`shadow` 只有日志与一个 `job_readout_shadow`
   读数，`extra["job_readouts"]` 不许出现；`on` 才进 prompt。

跑法：``python -X utf8 -u tests/test_job_tick_wiring.py``
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILED = 0
CHECKED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED, CHECKED
    CHECKED += 1
    if cond:
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f" — {detail}" if detail else ""))


class FakeStore:
    def __init__(self, jobs: list | None = None, reads: list | None = None) -> None:
        self.jobs = jobs if jobs is not None else []
        self.reads = reads if reads is not None else []
        self.opened: list[str] = []
        self.read_calls: list[str] = []

    def enabled(self) -> bool:
        return True

    def open_jobs(self, chat_id: str) -> list:
        self.opened.append(chat_id)
        return list(self.jobs)

    def readouts(self, chat_id: str, limit: int = 3) -> list:
        self.read_calls.append(chat_id)
        return list(self.reads)[:limit]


class FakeJob:
    """推进器只用到 `job_id`；授权判定还要 `policy`（空 policy = 没人批过）。"""

    job_id = "j1"
    policy = None

    def __init__(self, job_id: str = "j1", policy=None) -> None:
        self.job_id = job_id
        self.policy = policy


class FakeRegistry:
    """能记事的工具注册表：`get()` 给参数声明，`act()` / `observe()` 只记事不碰真工具。"""

    def __init__(self, args: tuple = ()) -> None:
        self.declared = {name: {"type": "string"} for name in args}
        self.acted: list = []
        self.observed: list = []

    def get(self, tool: str):
        return SimpleNamespace(name=tool, args=dict(self.declared))

    def act(self, target, action, params=None):
        self.acted.append({"target": target, "action": action, "params": dict(params or {})})
        return SimpleNamespace(
            status="ok", content="写了 1 个文件", truncated=False,
            state_before="h0", state_after="h1", changed={"path": "notes/x.md"},
        )

    def observe(self, target, what, params=None):
        self.observed.append({"target": target, "what": what, "params": dict(params or {})})
        return SimpleNamespace(
            status="ok", content="三条搜索结果", truncated=False,
            state_before="", state_after="", changed={},
        )


def _patch(monkey: dict) -> dict:
    """把 features.mode / default_store / advance / 工具注册表换掉，返回旧值以便还原。"""
    from core import features
    from core.jobs import executor, runner, store

    old = {
        "mode": features.mode,
        "default_store": store.default_store,
        "advance": runner.advance,
        "default_registry": executor.default_registry,
    }
    features.mode = lambda name, default=None: monkey.get("mode", old["mode"](name, default))
    store.default_store = monkey.get("store_factory") or (lambda: FakeStore())
    calls: list = []
    runner.advance = lambda job, **kw: calls.append((job, kw)) or _FakeAdvance()
    # 没给 registry 就跟原来一样走真实工厂（测试环境里通常没绑定 → execute 为 None）
    executor.default_registry = (
        monkey["registry"] if "registry" in monkey else old["default_registry"]
    )
    monkey["calls"] = calls
    return old


def _restore(old: dict) -> None:
    from core import features
    from core.jobs import executor, runner, store

    features.mode = old["mode"]
    store.default_store = old["default_store"]
    runner.advance = old["advance"]
    executor.default_registry = old["default_registry"]


class _FakeAdvance:
    stop = "no_executor"
    ran: tuple = ()
    job = FakeJob()


def test_off_touches_nothing() -> None:
    from core.routing import turn_pipeline as tp

    touched: list[str] = []

    def boom():
        touched.append("store")
        raise AssertionError("off 档不该打开台账")

    old = _patch({"mode": "off", "store_factory": boom})
    try:
        extra: dict = {}
        tp._job_tick(chat_id="c1", extra=extra)
        check("off：不打开台账", touched == [], f"touched={touched}")
        check("off：extra 一个键都不多", extra == {}, f"extra={extra}")
    finally:
        _restore(old)


def test_shadow_computes_counts_but_no_prompt() -> None:
    from core.routing import turn_pipeline as tp

    store = FakeStore(jobs=[FakeJob()], reads=["j1 · 目标A · 第1步 · 等批准"])
    monkey = {"mode": "shadow", "store_factory": lambda: store}
    old = _patch(monkey)
    try:
        extra: dict = {"chat_id": "c1"}
        tp._job_tick(chat_id="c1", extra=extra)
        check("shadow：推进器被调用（有调用者）", len(monkey["calls"]) == 1, f"{monkey['calls']}")
        check(
            "shadow：execute 没给（只读推进器空转）",
            monkey["calls"] and monkey["calls"][0][1].get("execute") is None,
            f"{monkey['calls']}",
        )
        check(
            "shadow：extra 里没有 job_readouts",
            "job_readouts" not in extra,
            f"extra={extra}",
        )
        check(
            "shadow：只有一个 open 计数",
            extra.get("job_readout_shadow") == {"open": 1},
            f"extra={extra}",
        )
        check("shadow：不读 readouts", store.read_calls == [], f"{store.read_calls}")
    finally:
        _restore(old)


def test_on_enters_prompt() -> None:
    from core.context.job_readout import build_job_readout_block
    from core.routing import turn_pipeline as tp

    store = FakeStore(jobs=[FakeJob()], reads=["j1 · 目标A · 第1步 · 等批准"])
    old = _patch({"mode": "on", "store_factory": lambda: store})
    try:
        extra: dict = {"chat_id": "c1"}
        tp._job_tick(chat_id="c1", extra=extra)
        check("on：读数进 extra", extra.get("job_readouts") == store.reads, f"extra={extra}")
        block = build_job_readout_block(extra)
        check("on：渲染成块", block.startswith("[在手的事]"), repr(block))
        check("on：块里有目标", "目标A" in block, repr(block))
    finally:
        _restore(old)


def test_failure_never_breaks_turn() -> None:
    from core.routing import turn_pipeline as tp

    def boom():
        raise RuntimeError("db 挂了")

    old = _patch({"mode": "shadow", "store_factory": boom})
    try:
        extra: dict = {"chat_id": "c1"}
        try:
            tp._job_tick(chat_id="c1", extra=extra)
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            print("  raised", exc)
        check("台账炸了也不许抛", ok)
        check("炸了以后 extra 只有 chat_id", set(extra) == {"chat_id"}, f"extra={extra}")
    finally:
        _restore(old)


def test_empty_chat_id_noop() -> None:
    from core.routing import turn_pipeline as tp

    touched: list[str] = []
    old = _patch(
        {
            "mode": "on",
            "store_factory": lambda: touched.append("store") or FakeStore(),
        }
    )
    try:
        tp._job_tick(chat_id="", extra={})
        check("没有 chat_id 就不碰台账", touched == [], f"{touched}")
    finally:
        _restore(old)


def test_static_wiring() -> None:
    """下面这几条是**静态纪律**，行为层测不出来，所以保留并各自写明理由。

    本文件其余用例（`test_off_*` / `test_on_*`）已经把「off 不碰台账」「on 交出去的执行器
    能真写」「推进器拿到的 allow_effects 只有读」等**行为**测掉了；这里只守那些
    「行为测到也不等于接线还在」的形状约束。
    """
    src = io.open(ROOT / "core/routing/turn_pipeline.py", encoding="utf-8").read()

    # 理由 1/5：_job_tick 是「每回合的写入口」，全局只能有一个实现——多一个同名函数
    #   就是两条写路径，行为测试只会命中其中一条（patch 哪个都说得通），分不出重复实现。
    check("静态：定义了 _job_tick", src.count("def _job_tick(") == 1, str(src.count("def _job_tick(")))

    # 理由 2/5：调用点必须落在主回合函数 generate_turn 里。行为层能测「一次回合写了什么」，
    #   测不到「它是不是被别的入口（例如定时器/补发）也调了一次」；这条同时把调用位置钉住。
    call = re.search(r"^async def generate_turn\(ctx: TurnContext\).*?(?=^async def |^def )", src, re.S | re.M)
    body = call.group(0) if call else ""
    check("静态：调用点在 generate_turn 里", "_job_tick(" in body, str(len(body)))

    # 理由 3/5：全文件只允许「定义 1 处 + 调用 1 处」。这是理由 1+2 的合计口径：
    #   行为测试无法发现第三个调用点（多调一次通常幂等/被 store 吞掉，行为上看不出）。
    check("静态：只有一个调用点", src.count("_job_tick(") == 2, str(src.count("_job_tick(")))

    # 理由 4/5：`_job_tick` 的前置判据是三态旗标 JOB_LEDGER。行为层测的是"off 时什么都没发生"，
    #   而"读的是不是这个旗标"测不出来——把旗标换成别的常量，off 用例照样绿。
    check("静态：读的是 JOB_LEDGER", "features.JOB_LEDGER" in src)

    # 理由 5/5：三处 import 是三个模块级接缝（读数 / 推进器 / 台账）。它们确实由行为用例
    #   间接盯着（`monkey["calls"]` 能拿到 advance 的参数），但"从哪个模块 import"本身
    #   是模块边界纪律：换成另一条同义路径（例如 `core.jobs.store.JobStore(...)` 自建）
    #   行为能过、边界却破了。三处并成一条，避免为一个 import 写三条断言。
    seams = ("from core.context.job_readout import",
             "from core.jobs.runner import advance",
             "from core.jobs.store import default_store")
    check("静态：三处接缝都还是那几个模块", all(seam in src for seam in seams),
          str([seam for seam in seams if seam not in src]))


def test_on_wires_a_writable_executor() -> None:
    """`on` 档交给推进器的执行器必须「能真写、不是影子档」。

    这一条替掉 `tests/test_job_write_executor.py` 里 4 条读源码的锁：
    它们断言 `core/routing/turn_pipeline.py` 出现过 `write_executor(` /
    `shadow=False` / `allow_effects=(READ_EFFECT,)` / 不出现 `read_only_executor(`。
    源码一改结构就红，而它们想保的行为（写步真写、读步只 observe、默认只放读）
    在这里全都能直接测出来。
    """
    from core.contracts.plan import Step
    from core.jobs.executor import READ_EFFECT
    from core.routing import turn_pipeline as tp

    reg = FakeRegistry(args=("content",))
    store = FakeStore(jobs=[FakeJob()])
    monkey = {"mode": "on", "store_factory": lambda: store, "registry": lambda: reg}
    old = _patch(monkey)
    real_external, real_correction = tp._external_sender, tp._correction_sender
    tp._external_sender = lambda: None  # 别为了一个执行器把 GUI 出口的模块拉进来
    tp._correction_sender = lambda: None
    try:
        tp._job_tick(chat_id="c1", extra={"chat_id": "c1"})
        kw = monkey["calls"][0][1] if monkey["calls"] else {}
        execute = kw.get("execute")
        check("on：给了执行器", callable(execute), f"{execute}")
        check(
            "on：免批准名单只有 read",
            tuple(kw.get("allow_effects") or ()) == (READ_EFFECT,),
            f"{kw.get('allow_effects')}",
        )

        wrote = execute(Step(n=1, action="write_note:旅行计划", effects="write_local"))
        check("写步真的写进去（不是影子档）", wrote.get("ok") is True, f"{wrote}")
        check("写步调到 act", [c["action"] for c in reg.acted] == ["write_note"], f"{reg.acted}")
        check("写步带回状态差异回执", bool(wrote.get("changed")), f"{wrote}")
        check(
            "半句话猜进工具声明的参数名",
            bool(reg.acted) and reg.acted[0]["params"].get("content") == "旅行计划",
            f"{reg.acted[:1]}",
        )

        read = execute(Step(n=2, action="web_search:杭州", effects="read"))
        check("读步走 observe", read.get("ok") is True and len(reg.observed) == 1, f"{read}")
        check("读步不碰 act", len(reg.acted) == 1, f"{reg.acted}")
    finally:
        tp._external_sender, tp._correction_sender = real_external, real_correction
        _restore(old)


def test_on_authorize_is_policy_authorize_step() -> None:
    """推进器拿到的 `authorize` 钩子必须是三级授权，不是常真的 lambda。

    替掉 `tests/test_job_authorization.py:158-159` 的两条源码锁
    （`policy.authorize_step(` + `authorize=_authorize` / `allow_effects=(READ_EFFECT,)`）。
    """
    from core.contracts.plan import Policy, Step
    from core.jobs import policy as pol
    from core.jobs.executor import READ_EFFECT
    from core.routing import turn_pipeline as tp

    monkey = {"mode": "on", "store_factory": lambda: FakeStore(jobs=[FakeJob()]),
              "registry": lambda: FakeRegistry()}
    old = _patch(monkey)
    try:
        tp._job_tick(chat_id="c1", extra={"chat_id": "c1"})
        kw = monkey["calls"][0][1] if monkey["calls"] else {}
        auth = kw.get("authorize")
        check("on：给了授权判定钩子", callable(auth), f"{auth}")
        check("on：免批准名单只有 read",
              tuple(kw.get("allow_effects") or ()) == (READ_EFFECT,),
              f"{kw.get('allow_effects')}")
        check(
            "读步自动放行",
            auth(Step(n=1, action="web_search:杭州", effects="read")) == (True, pol.TIER_AUTO),
            "",
        )
        check(
            "写步没人批过就拦",
            auth(Step(n=2, action="write_note:x", effects="write_local"))[0] is False,
            f"{auth(Step(n=3, action='write_note:y', effects='write_local'))}",
        )
    finally:
        _restore(old)

    granted = FakeJob(policy=Policy(scope="write_local", approved_by="私聊:p1"))
    monkey = {"mode": "on", "store_factory": lambda: FakeStore(jobs=[granted]),
              "registry": lambda: FakeRegistry()}
    old = _patch(monkey)
    try:
        tp._job_tick(chat_id="c1", extra={"chat_id": "c1"})
        auth = monkey["calls"][0][1]["authorize"] if monkey["calls"] else None
        check(
            "有长期授权就自己办",
            auth is not None
            and auth(Step(n=1, action="write_note:x", effects="write_local"))
            == (True, pol.TIER_GRANT),
            "",
        )
    finally:
        _restore(old)


def main() -> int:
    test_off_touches_nothing()
    test_shadow_computes_counts_but_no_prompt()
    test_on_enters_prompt()
    test_failure_never_breaks_turn()
    test_empty_chat_id_noop()
    test_on_wires_a_writable_executor()
    test_on_authorize_is_policy_authorize_step()
    test_static_wiring()
    print(f"RESULT {FAILED} failed / {CHECKED} checked")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
