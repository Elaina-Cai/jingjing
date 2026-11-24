# -*- coding: utf-8 -*-
"""对外发送接进执行器：``external_send`` 只能走那条被允许的路。

规矩（`docs/decisions/办_在手的事.md` §17.4）：
- 默认 ``allow_external=False``：这一步报 ``blocked:non_writable:external_send``，一次都不发；
- 放行但没递回调 → ``no_sender``（路上没人，不是拒绝）；
- 回调只拿 ``(收件人, 正文, turn_id)``，回账 ``sent`` / ``unconfirmed`` / ``failed``；
- ``failed`` **不重试**（重复发送率必须为 0），停在这一步交给人。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts.plan import Job, Plan, Step  # noqa: E402
from core.jobs import executor as ex  # noqa: E402
from core.jobs.ingest import build_job  # noqa: E402
from core.jobs.runner import advance  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} :: {detail}")
        FAILED.append(name)


def _job(steps: list[Step], *, state: str = "open") -> Job:
    return build_job(
        Plan(goal="试一次对外发送", steps=tuple(steps)),
        chat_id="chat-1",
        trace_id="turn-1",
        owner_person_id="owner-1",
    )


class _Sender:
    """记下每次调用的假发送器。"""

    def __init__(self, status: str = "sent", *, local_id: int = 1001, server_id: int = 9001, boom: bool = False):
        self.status = status
        self.local_id = local_id
        self.server_id = server_id
        self.boom = boom
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, to: str, text: str, *, turn_id: str = "") -> dict:
        self.calls.append((to, text, turn_id))
        if self.boom:
            raise RuntimeError("微信没连上")
        return {
            "status": self.status,
            "detail": "回读到自己那行" if self.status == "sent" else "等不到对账",
            "local_id": self.local_id,
            "server_id": self.server_id,
        }


def main() -> int:
    step = Step(n=1, action="send:明天见", effects="external_send")

    # 1) 默认：连碰都不碰
    sender = _Sender()
    out = ex.write_executor(None, turn_id="j1", target="chat-1")(step)
    check("默认拒绝对外发送", out == {"ok": False, "detail": "blocked:non_writable:external_send"}, str(out))
    check("默认不调用发送器", sender.calls == [], str(sender.calls))

    # 2) 放行但没递回调 → no_sender
    out = ex.write_executor(None, target="chat-1", allow_external=True)(step)
    check("放行但没有发送器 → no_sender", str(out.get("detail", "")).startswith(ex.NO_SENDER_REASON), str(out))
    check("no_sender 不算发出去", out.get("ok") is False, str(out))

    # 3) 收件人与正文的拆法
    sender = _Sender()
    run = ex.write_executor(None, turn_id="j1", target="chat-1", allow_external=True, external=sender)
    run(Step(n=1, action="send:张三|明天见", effects="external_send"))
    check("写了竖线 → 收件人是张三", sender.calls[-1][:2] == ("张三", "明天见"), str(sender.calls))
    run(Step(n=1, action="send:明天见", effects="external_send"))
    check("没写竖线 → 收件人是这个会话", sender.calls[-1][:2] == ("chat-1", "明天见"), str(sender.calls))
    check("turn_id 透传给发送器", sender.calls[-1][2] == "j1", str(sender.calls))

    # 4) sent：带账
    out = run(Step(n=1, action="send:明天见", effects="external_send"))
    check("sent 算发出去", out.get("ok") is True, str(out))
    check("sent 的 changed 带两个号", out.get("changed") == {"local_id": 1001, "server_id": 9001}, str(out))
    check("sent 的 state_after 指到那一行", out.get("state_after") == "wechat#1001", str(out))
    check("sent 的 detail 以状态打头", str(out.get("detail", "")).startswith("sent:"), str(out))

    # 5) unconfirmed：也算发出去了（不许重发）
    unconfirmed = _Sender("unconfirmed", local_id=0, server_id=0)
    out = ex.write_executor(None, target="chat-1", allow_external=True, external=unconfirmed)(
        Step(n=1, action="send:明天见", effects="external_send")
    )
    check("unconfirmed 也算发出去", out.get("ok") is True, str(out))
    check("unconfirmed 只调一次", len(unconfirmed.calls) == 1, str(unconfirmed.calls))
    check("unconfirmed 的 changed 有 status", out.get("changed") == {"status": "unconfirmed"}, str(out))

    # 6) failed：不重试、不推进
    failed = _Sender("failed")
    out = ex.write_executor(None, target="chat-1", allow_external=True, external=failed)(
        Step(n=1, action="send:明天见", effects="external_send")
    )
    check("failed 不算发出去", out.get("ok") is False, str(out))
    check("failed 不打头 ok", str(out.get("detail", "")).startswith("failed:"), str(out))
    check("failed 只试一次（重复发送率为 0）", len(failed.calls) == 1, str(failed.calls))

    # 7) 发送器自己炸了
    boom = _Sender(boom=True)
    out = ex.write_executor(None, target="chat-1", allow_external=True, external=boom)(
        Step(n=1, action="send:明天见", effects="external_send")
    )
    check("发送器异常算这一步失败", out.get("ok") is False, str(out))
    check("失败原因带异常类型", "RuntimeError" in str(out.get("detail", "")), str(out))

    # 8) 空正文不发
    empty = _Sender()
    out = ex.write_executor(None, target="chat-1", allow_external=True, external=empty)(
        Step(n=1, action="send:张三|", effects="external_send")
    )
    check("空正文不发", str(out.get("detail", "")).startswith("unsupported:"), str(out))
    check("空正文零调用", empty.calls == [], str(empty.calls))

    # 9) 推进器：没批准一步都不发，批了才发，发完就 done
    job = _job([Step(n=1, action="send:明天见", effects="external_send")])
    sender = _Sender()
    run = ex.write_executor(None, turn_id=job.job_id, target="chat-1", allow_external=True, external=sender)
    first = advance(job, execute=run, allow_effects=("read",))
    check("没批准停 need_approval", first.stop == "need_approval", first.stop)
    check("没批准零调用", sender.calls == [], str(sender.calls))
    second = advance(job, execute=run, allow_effects=("read",), approved=lambda s: True)
    check("批了就跑", len(sender.calls) == 1, str(sender.calls))
    check("发完就 done", second.stop == "done", second.stop)
    check("步号推进到 1", second.job.step_n == 1, str(second.job.step_n))

    # 10) 发失败：步号不动、账上留一笔
    bad_job = _job([Step(n=1, action="send:明天见", effects="external_send")])
    bad = _Sender("failed")
    run_bad = ex.write_executor(None, turn_id=bad_job.job_id, target="chat-1", allow_external=True, external=bad)
    out_bad = advance(bad_job, execute=run_bad, allow_effects=("read",), approved=lambda s: True)
    check("发失败停下", out_bad.stop == "failed", out_bad.stop)
    check("发失败步号不动", out_bad.job.step_n == 0, str(out_bad.job.step_n))

    # 11) 静态纪律
    src = (ROOT / "core/jobs/executor.py").read_text(encoding="utf-8")
    check("executor 默认不接对外发送", "allow_external: bool = False" in src, "")
    tsrc = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    check("回合里按发送器有无决定放行", "allow_external=external is not None" in tsrc, "")
    check("发送器工厂只有一个", tsrc.count("def _external_sender(") == 1, str(tsrc.count("def _external_sender(")))
    check("工厂只认 wechat_send 的实现", "from core.execution.gui.wechat_send import send_and_verify" in tsrc, "")
    check("回合不许出现别的按回车路径", "press_enter" not in tsrc, "")

    print(f"RESULT {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
