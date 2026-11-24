"""在手的事读数 = 判的第 6 样输入（`docs/decisions/判投记_契约.md` §1 §2）。

这条测试只盯三件事：
1. **off 档一个字符都不动**（连台账都不碰）——默认档不许有行为变化；
2. shadow 只落读数、**不注入**；on 才出 `[在手的事]` 块；
3. 块形状有上限（≤3 条 × ≤60 字），且任一环节抛异常都降级成空串。

跑法：`.venv_it\\Scripts\\python.exe -X utf8 tests\\test_job_readout_block.py`
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import job_readout as jr  # noqa: E402
from core.contracts.plan import READOUT_MAX_CHARS, READOUT_MAX_ITEMS  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(cond), detail))


class FakeStore:
    """记下被怎么用过的假台账（不碰数据库）。"""

    def __init__(self, reads: list[str] | None = None, raise_on: str = "") -> None:
        self.reads = list(reads or [])
        self.raise_on = raise_on
        self.calls: list[str] = []

    def readouts(self, chat_id: str) -> list[str]:
        self.calls.append("readouts:" + chat_id)
        if self.raise_on == "readouts":
            raise RuntimeError("db down")
        return self.reads

    def open_jobs(self, chat_id: str) -> list[str]:
        self.calls.append("open_jobs:" + chat_id)
        if self.raise_on == "open_jobs":
            raise RuntimeError("db down")
        return ["j1", "j2"]


def with_mode(mode: str):
    original = jr.job_readout_mode
    jr.job_readout_mode = lambda: mode  # type: ignore[assignment]
    return original


def run() -> None:
    # ── 形状：块头 + 每行 `- 读数` ──────────────────────────────────────────
    block = jr.render_job_readouts(["j1 · 规划杭州 · 第 1 步 · 等批准", "j2 · 订票 · 第 0 步 · 在办"])
    check("块头是 [在手的事]", block.startswith("[在手的事]\n- "), block[:20])
    check("两条读数两行", block.count("\n- ") == 2, repr(block))
    check("空列表 → 空串（不加块头）", jr.render_job_readouts([]) == "", repr(jr.render_job_readouts([])))
    check("全是空白 → 空串", jr.render_job_readouts(["", "   "]) == "", "blank")

    # ── 上限：条数 ≤3、每条 ≤60 字 ─────────────────────────────────────────
    many = [f"j{i} · " + "目标很长" * 40 for i in range(6)]
    capped = jr.render_job_readouts(many)
    lines = [ln for ln in capped.splitlines() if ln.startswith("- ")]
    check(f"条数上限 {READOUT_MAX_ITEMS}", len(lines) == READOUT_MAX_ITEMS, str(len(lines)))
    longest = max(len(ln) - 2 for ln in lines) if lines else 0
    check(f"每条上限 {READOUT_MAX_CHARS} 字", longest <= READOUT_MAX_CHARS, str(longest))

    # ── off 档：一个字符都不动，且连台账都不碰 ──────────────────────────────
    store = FakeStore(["j1 · x · 第 1 步 · 等批准"])
    original = with_mode("off")
    try:
        extra: dict = {"chat_id": "c1"}
        got = jr.collect_job_readouts(extra, store=store)
        block_off = jr.build_job_readout_block(extra)
    finally:
        jr.job_readout_mode = original  # type: ignore[assignment]
    check("off：不返回读数", got == [], repr(got))
    check("off：不碰台账", store.calls == [], repr(store.calls))
    check("off：extra 里不多任何键", set(extra) == {"chat_id"}, repr(sorted(extra)))
    check("off：块是空串", block_off == "", repr(block_off))

    # ── shadow：只落读数、不注入 ───────────────────────────────────────────
    store = FakeStore(["j1 · x · 第 1 步 · 等批准"])
    original = with_mode("shadow")
    try:
        extra = {"chat_id": "c1"}
        got = jr.collect_job_readouts(extra, store=store)
        block_shadow = jr.build_job_readout_block(extra)
    finally:
        jr.job_readout_mode = original  # type: ignore[assignment]
    check("shadow：不返回读数", got == [], repr(got))
    check("shadow：块是空串", block_shadow == "", repr(block_shadow))
    check("shadow：落了 open 读数", extra.get("job_readout_shadow") == {"open": 2}, repr(extra.get("job_readout_shadow")))
    check("shadow：查的是 open_jobs", store.calls == ["open_jobs:c1"], repr(store.calls))

    # ── on：读数进 extra，块渲染出来 ────────────────────────────────────────
    store = FakeStore(["j1 · 规划杭州 · 第 1 步 · 等批准", "j2 · 订票 · 第 0 步 · 在办"])
    original = with_mode("on")
    try:
        extra = {"chat_id": "c1"}
        got = jr.collect_job_readouts(extra, store=store)
        block_on = jr.build_job_readout_block(extra)
    finally:
        jr.job_readout_mode = original  # type: ignore[assignment]
    check("on：返回 2 条读数", len(got) == 2, repr(got))
    check("on：写进 extra['job_readouts']", extra.get("job_readouts") == got, repr(extra.keys()))
    check("on：块渲染出两条", block_on.count("\n- ") == 2, repr(block_on))

    # ── 降级：查库抛异常 → 空串，不冒泡 ────────────────────────────────────
    original = with_mode("on")
    try:
        boom = FakeStore(raise_on="readouts")
        extra = {"chat_id": "c1"}
        got = jr.collect_job_readouts(extra, store=boom)
        block_boom = jr.build_job_readout_block(extra)
    finally:
        jr.job_readout_mode = original  # type: ignore[assignment]
    check("台账炸了：返回空", got == [], repr(got))
    check("台账炸了：块是空串", block_boom == "", repr(block_boom))

    # ── 没 chat_id：不查库 ─────────────────────────────────────────────────
    original = with_mode("on")
    try:
        no_chat = FakeStore(["j1 · x · 第 1 步 · 在办"])
        got = jr.collect_job_readouts({}, store=no_chat)
    finally:
        jr.job_readout_mode = original  # type: ignore[assignment]
    check("没 chat_id：直接空", got == [] and no_chat.calls == [], repr(no_chat.calls))

    # ── 块头与常量不许改名（改名要同步契约 §1 与量具） ──────────────────────
    check("块头常量是 [在手的事]", jr.JOB_READOUT_HEADER == "[在手的事]", jr.JOB_READOUT_HEADER)

    # ── 静态接线：真回合的提示词里确实贴了这一块 ────────────────────────────
    src = io.open(ROOT / "agents" / "conversational.py", encoding="utf-8").read()
    check(
        "conversational 里贴了 _job_readout_block",
        "prompt, _job_readout_block(extra)" in src,
        "missing wiring",
    )
    check(
        "接线在 _read_block 之后（同一处追加）",
        "_read_block(extra, read_mode)" not in src and "_job_readout_block(extra)" in src,
        "order（2026-10-23：读场块退役，job 读块仍在同一处追加）",
    )
    check(
        "off 档不碰台账（助手先看开关）",
        "from core.context.job_readout import" in src and "collect_job_readouts(extra)" in src,
        "helper body",
    )


def main() -> int:
    run()
    failed = 0
    for name, cond, detail in CHECKS:
        if cond:
            print(f"PASS {name}")
        else:
            failed += 1
            print(f"FAIL {name} :: {detail}")
    print(f"RESULT {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
