"""U-28 交接单：办不下去时给人的是三样东西，不是一句「抱歉」。

钉住的边界：

- 哪些停因才算「办不下去」（`budget/turns/failed/no_executor/blocked`）；
  `done` 不交、`need_approval`/`awaiting` 不交（那有第六态回执管）；
- 文本必须回答三件事：为什么停、走到哪、还差什么 / 你要动哪里；
- **不假装办成**（不出现「已完成 / 已办」）、**不假设人会接手**（不出现「我等你确认」）；
- `handed_off` 事件只落一次（幂等），投影只换 `handoff_json`，不动推进状态；
- `off` 档一行都不算、`shadow` 只算文本不落事件。

跑法：.venv_it\\Scripts\\python.exe -X utf8 -u tests/test_job_handoff.py
"""

from __future__ import annotations

import dataclasses
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：不给 dir= 时系统 TEMP 不可写会静默回落到仓库根。
from _tempdirs import temp_base  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402

from core.contracts.plan import Job, Step  # noqa: E402
from core.jobs import handoff as H  # noqa: E402
from core.jobs import store as job_store  # noqa: E402

COUNT = [0]
FAILS: list[str] = []
BANNED = ("抱歉", "已完成", "已办", "我等你确认", "对不起")


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(f"{name} {detail}".strip())
    print(f"  FAIL {name} {detail}".strip())


def with_mode(mode: str):
    prev = H._mode
    H._mode = lambda: mode
    return prev


def _store(mode: str = "on") -> job_store.JobStore:
    engine = create_engine("sqlite://")
    job_store.ensure_schema(engine)
    return job_store.JobStore(engine, mode=mode)


def _job(*, step_n: int = 1, steps: list[Step] | None = None) -> Job:
    steps = steps if steps is not None else [
        Step(n=1, action="web_search:杭州 十一月 天气", effects="read"),
        Step(n=2, action="write:行程.md", effects="write_local"),
        Step(n=3, action="gui_click:#提交订单", effects="ui_control"),
    ]
    return Job(job_id="j1", chat_id="c1", goal="订下周去杭州的行程", owner_person_id="u1",
               step_n=step_n, steps=steps, tool_calls_total=12, turns_total=5)


def t_stops() -> None:
    print("[t_stops]")
    for stop in ("budget", "turns", "failed", "no_executor", "blocked"):
        check(f"{stop} 要交", H.should_handoff(stop) is True)
    for stop in ("done", "need_approval", "awaiting", "shadow", "state", ""):
        check(f"{stop or '空'} 不交", H.should_handoff(stop) is False)
    dead = dataclasses.replace(_job(), state="cancelled")
    check("终态的件不交", H.should_handoff("failed", dead) is False)
    check("活着的件交", H.should_handoff("failed", _job()) is True)


def t_left_and_prefilled() -> None:
    print("[t_left_and_prefilled]")
    job = _job(step_n=1)
    left = H.remaining_steps(job)
    check("只列剩下的", left == ["write:行程.md", "gui_click:#提交订单"], str(left))
    check("最多三条", len(H.remaining_steps(_job(step_n=0), limit=3)) == 3)
    pre = H.prefilled_fields(job)
    check("参数来自剩余步", pre == ("行程.md", "#提交订单"), str(pre))
    done_args = H.prefilled_fields(_job(step_n=3))
    check("做完的步不抄参数", done_args == (), str(done_args))
    long_action = _job(step_n=0, steps=[Step(n=1, action="x:" + "啊" * 200)])
    check("超长动作截断", len(H.remaining_steps(long_action)[0]) <= H.MAX_STEP_CHARS)


def t_text() -> None:
    print("[t_text]")
    job = _job(step_n=1)
    pkg = H.build_handoff(job, reason="budget")
    text = H.handoff_text(job, pkg, reason="budget")
    check("说清为什么停", "没办完（这一步的工具预算用完了）" in text, text)
    check("说清做到哪", "已经走到：第 1 步" in text, text)
    check("说清还差什么", "还差：1) write:行程.md" in text, text)
    check("给了预填字段", "我先填好：行程.md" in text, text)
    check("不超长", len(text) <= H.MAX_HANDOFF_CHARS, str(len(text)))
    for word in BANNED:
        check(f"不出现「{word}」", word not in text)
    check("不假设人接手", "等你" not in text)
    check("默认 note 是原因", pkg.note == H.HANDOFF_WHY["budget"], pkg.note)
    only = _job(steps=[Step(n=1, action="web_search:天气")], step_n=1)
    tail = H.handoff_text(only, H.build_handoff(only, reason="turns"), reason="turns")
    check("没下一步时的说法", "没有下一步了" in tail, tail)
    with_url = H.build_handoff(job, reason="failed", url="https://www.12306.cn", note="去把验证码填了")
    txt2 = H.handoff_text(job, with_url, reason="failed")
    check("链接进文本", "12306" in txt2)
    check("你要动的进文本", "去把验证码填了" in txt2)


def t_markdown_and_file() -> None:
    print("[t_markdown_and_file]")
    job = _job(step_n=1)
    pkg = H.build_handoff(job, reason="failed", url="https://x.test", screenshots=("data/evidence/a.png",))
    md = H.handoff_markdown(job, pkg, reason="failed", detail="Traceback: boom")
    check("勾选标出做完的", "- [x] 1. web_search" in md, md[:200])
    check("剩下的没勾", "- [ ] 2. write:行程.md" in md)
    check("带预算用量", "工具 12 次 / 回合 5 次" in md)
    check("带原始结果", "Traceback: boom" in md)
    check("带证据", "data/evidence/a.png" in md)
    check("写明不是失败报告", "不是失败报告" in md)
    with tempfile.TemporaryDirectory(dir=str(temp_base())) as tmp:
        path = H.write_handoff(job, md, out_dir=pathlib.Path(tmp))
        check("落盘成功", bool(path) and pathlib.Path(path).exists(), path)
        check("内容一致", pathlib.Path(path).read_text(encoding="utf-8") == md)


def t_record() -> None:
    print("[t_record]")
    job = _job(step_n=1)
    pkg = H.build_handoff(job, reason="budget")

    prev = with_mode("off")
    try:
        store = _store()
        store.create(job)
        check("off 档不落", H.record_handoff(job, pkg, store=store, reason="budget") is False)
        check("off 档没有事件", "handed_off" not in store.event_kinds("j1"))
    finally:
        H._mode = prev

    prev = with_mode("shadow")
    try:
        store = _store()
        store.create(job)
        check("shadow 只算不落", H.record_handoff(job, pkg, store=store, reason="budget") is False)
        check("shadow 没有事件", "handed_off" not in store.event_kinds("j1"))
    finally:
        H._mode = prev

    prev = with_mode("on")
    try:
        store = _store()
        store.create(job)
        check("on 档落库", H.record_handoff(job, pkg, store=store, reason="budget") is True)
        kinds = store.event_kinds("j1")
        check("事件 kind 正确", kinds[-1] == "handed_off", str(kinds))
        check("幂等：第二次 False", H.record_handoff(job, pkg, store=store, reason="budget") is False)
        loaded = store.load("j1")
        check("投影写入 handoff", loaded.handoff.note == pkg.note, loaded.handoff.note)
        check("不动状态", loaded.state == job.state and loaded.step_n == job.step_n)
        store.rebuild("c1")
        again = store.load("j1")
        check("重放后仍在", again.handoff.note == pkg.note)
        check("重放不多事件", store.event_kinds("j1").count("handed_off") == 1)
    finally:
        H._mode = prev


def t_apply() -> None:
    print("[t_apply]")
    job = _job(step_n=1)
    prev = with_mode("on")
    tmp_root = pathlib.Path(tempfile.mkdtemp(prefix="handoff_", dir=str(ROOT / ".test_tmp")))
    old_dir = H.HANDOFF_DIR
    H.HANDOFF_DIR = tmp_root
    try:
        store = _store()
        store.create(job)
        text = H.apply_handoff("budget", job, store=store)
        check("办不下去才有文本", bool(text) and "没办完" in text, text)
        check("事件落了", "handed_off" in store.event_kinds("j1"))
        check("文件也落了", (tmp_root / "j1.md").exists(), str(list(tmp_root.glob("*.md"))))
        check("done 不给交接单", H.apply_handoff("done", job, store=store) == "")
        check("need_approval 不给交接单", H.apply_handoff("need_approval", job, store=store) == "")
    finally:
        H.HANDOFF_DIR = old_dir
        H._mode = prev


def t_shape_and_wiring() -> None:
    print("[t_shape_and_wiring]")
    check("事件 kind 已注册", "handed_off" in job_store.EVENT_KINDS)
    src = pathlib.Path("core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    check("回合里接了交接", "handoff_mod.should_handoff" in src)
    check("交接文本进 extra", 'extra["job_handoff"]' in src)
    check("store 投影认这个 kind", 'elif kind == "handed_off"' in
          pathlib.Path("core/jobs/store.py").read_text(encoding="utf-8"))
    hsrc = pathlib.Path("core/jobs/handoff.py").read_text(encoding="utf-8")
    check("源码里写明不假装办成", "不假装办成" in hsrc)
    # 措辞禁令扫的是**生成出来的文本**（各种停因 × 有/无剩余步），不是源码注释：
    # 源码 docstring 里要写「不许说抱歉」，那是纪律本身。
    for stop in H.HANDOFF_STOPS:
        for job in (_job(step_n=0), _job(step_n=1), _job(step_n=3)):
            out = H.handoff_text(job, H.build_handoff(job, reason=stop), reason=stop)
            hits = [w for w in BANNED if w in out]
            check(f"{stop}/{job.step_n} 措辞干净", not hits, str(hits))
    check("终态排除写死在代码里", 'state in ("done", "cancelled", "expired")' in hsrc)


def main() -> int:
    for fn in (t_stops, t_left_and_prefilled, t_text, t_markdown_and_file, t_record,
               t_apply, t_shape_and_wiring):
        fn()
    print()
    if FAILS:
        print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"RESULT 0 failed / {COUNT[0]} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
