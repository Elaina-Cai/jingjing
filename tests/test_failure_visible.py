"""失败要进她的下一轮上下文（在她看不见自己失败时，她会一直复读"发不了"）。"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.context import job_readout as jr  # noqa: E402
from core.contracts.plan import Job, Step  # noqa: E402
from core.jobs import store as job_store  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if ok:
        print("  ok  " + name)
    else:
        FAILS.append(name + " :: " + str(detail))
        print("  FAIL " + name + " :: " + str(detail))


def _engine(tmp: pathlib.Path):
    from sqlalchemy import create_engine

    eng = create_engine("sqlite:///" + (tmp / "j.db").as_posix())
    job_store.ensure_schema(eng)
    return eng


def _job(job_id: str = "j1", chat_id: str = "c1") -> Job:
    return Job(
        job_id=job_id,
        chat_id=chat_id,
        goal="往群里发一句",
        steps=[Step(n=1, action="send:鲸鲸的测试群|今天下雨记得带伞")],
    )


def t_store_failures(tmp: pathlib.Path) -> None:
    eng = _engine(tmp)
    st = job_store.JobStore(eng, mode=features.MODE_ON)
    job = _job()
    st.create(job)
    st.append(job.job_id, "step_failed", payload={"detail": "fill_failed no_target：没找到", "step_n": 1})
    rows = st.recent_failures("c1")
    check("失败读得到", len(rows) == 1, rows)
    check("detail 是原话", rows and "no_target" in rows[0]["detail"], rows)
    check("别的会话看不见", st.recent_failures("c2") == [])
    check("off 档不读", job_store.JobStore(eng, mode=features.MODE_OFF).recent_failures("c1") == [])
    eng.dispose()


def t_render_keeps_failure() -> None:
    text = jr.render_job_readouts(["事一", "事二", "事三", jr.FAIL_PREFIX + "发不出去：no_target"])
    check("块头在", text.startswith(jr.JOB_READOUT_HEADER), text)
    check("失败没被挤掉", "最近没成" in text, text)
    check("总数仍是 3", len([ln for ln in text.splitlines() if ln.startswith("- ")]) == 3, text)
    check("失败排最后", text.splitlines()[-1].startswith("- [最近没成]"), text)


class _Fake:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def open_jobs(self, chat_id: str):
        self.calls.append("open_jobs")
        return []

    def readouts(self, chat_id: str, limit: int = 3):
        self.calls.append("readouts")
        return ["[第 1 步 · 在等批准] 往群里发一句"]

    def recent_done(self, chat_id: str, **kw):
        self.calls.append("recent_done")
        return []

    def recent_failures(self, chat_id: str, **kw):
        self.calls.append("recent_failures")
        return [{"detail": "fill_failed no_target：没找到会话"}]


def t_collect_includes(tmp: pathlib.Path) -> None:
    fake = _Fake()
    extra = {"chat_id": "c1"}
    old = jr.job_readout_mode
    try:
        jr.job_readout_mode = lambda: "on"
        got = jr.collect_job_readouts(extra, store=fake)
        block = jr.build_job_readout_block(extra)
    finally:
        jr.job_readout_mode = old
    check("读到了失败", any("最近没成" in r for r in got), got)
    check("块里有失败", "最近没成" in block, block)
    check("问过 recent_failures", "recent_failures" in fake.calls, fake.calls)

    old_off = jr.job_readout_mode
    try:
        jr.job_readout_mode = lambda: "off"
        extra2 = {"chat_id": "c1"}
        got2 = jr.collect_job_readouts(extra2, store=fake)
    finally:
        jr.job_readout_mode = old_off
    check("off 档一个键都不多", got2 == [] and "job_readouts" not in extra2, extra2)


def t_assembler_policy() -> None:
    src = (ROOT / "core/context/assembler.py").read_text(encoding="utf-8")
    for header, mod in (("[现在这一屏]", "context.senses"), ("[本轮可用的工具]", "tool.prompt")):
        line = next((ln for ln in src.splitlines() if ln.strip().startswith('("' + header)), "")
        check(header + " 已登记", bool(line), line)
        check(header + " 不是 AUX", ", AUX, " not in line, line)
        check(header + " 指向 " + mod, mod in line, line)


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory(dir=str(ROOT / ".test_tmp"), ignore_cleanup_errors=True) as d:
        t_store_failures(pathlib.Path(d))
    t_render_keeps_failure()
    with tempfile.TemporaryDirectory(dir=str(ROOT / ".test_tmp"), ignore_cleanup_errors=True) as d:
        t_collect_includes(pathlib.Path(d))
    t_assembler_policy()
    print("")
    print("RESULT " + str(len(FAILS)) + " failed / " + str(COUNT) + " checks")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
