# -*- coding: utf-8 -*-
"""计划里的 ``send:`` 一路走完：开事 → 停等批准 → 人点头 → 才发一次（第 5 件）。

这条链上每一段以前都各自有用例（executor / runner / approval / plan_block），
但**没有一条用例把整条链串起来**：计划真的从模型的载荷形状进台账，人真的用一句
「可以」落下 `Approval`，然后才真的发出去，而且只发一次。串不起来的话，任何一段
改坏了，别的用例都还是绿的。

跑法：`.venv_it\\Scripts\\python.exe -X utf8 -u tests\\test_plan_send_approval.py`
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["YINYUE_FEATURE_JOB_LEDGER"] = "on"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

from core import features  # noqa: E402
from core.jobs import approval, ingest, plan_block, runner  # noqa: E402
from core.jobs import store as job_store  # noqa: E402
from core.jobs.executor import READ_EFFECT, write_executor  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f"  [{detail}]" if detail else ""))
    return bool(cond)


class _Sender:
    """真的发出去那一步的替身：只记账，不碰微信。"""

    def __init__(self, status: str = "sent") -> None:
        self.status = status
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, to: str, text: str, *, turn_id: str = "", **_kw: object) -> dict:
        self.calls.append((to, text, turn_id))
        if self.status == "sent":
            return {"status": "sent", "local_id": 1001, "server_id": 9001,
                    "detail": "ok", "state_before": "", "state_after": "wechat#1001"}
        return {"status": self.status, "detail": "nope"}


def _env():
    engine = create_engine("sqlite://")
    job_store.ensure_schema(engine)
    return job_store.JobStore(engine, mode=features.MODE_ON)


def _plan(goal: str = "往测试群说一句晚安") -> dict:
    # 模型真实会写出来的形状：**不写 effects**，只写 action。
    return {"goal": goal, "criteria": "群里出现「晚安」",
            "steps": [{"n": 1, "action": "send:鲸鲸的测试群|晚安"}]}


def _executor(sender: _Sender, job_id: str = "") -> object:
    return write_executor(None, turn_id=job_id, target="chat-1", shadow=False,
                          allow_external=True, external=sender)


def test_effects_inferred_without_field() -> None:
    plan = ingest.parse_plan({"plan": _plan()})
    check("模型不写 effects 时按 send 推断", plan is not None and plan.steps[0].effects == "external_send",
          str(plan and plan.steps[0].effects))
    check("推断出来的效果只有四种之一", plan.steps[0].effects in ingest.EFFECTS)


def test_plan_opens_job_and_waits() -> None:
    st = _env()
    sender = _Sender()
    job_id = plan_block.consume(_plan(), chat_id="chat-1", store=st)
    check("计划开出一件事", bool(job_id), job_id)
    job = st.load(job_id) if job_id else None
    check("开完就停在等批准", job is not None and job.state == "awaiting", getattr(job, "state", None))
    check("等的是批准", job is not None and job.awaiting.kind == "approval",
          getattr(getattr(job, "awaiting", None), "kind", None))
    check("开事阶段一次都没发", sender.calls == [], str(sender.calls))
    check("判据随计划进台账", job is not None and job.criteria == "群里出现「晚安」",
          getattr(job, "criteria", None))


def test_no_send_before_approval() -> None:
    # ① 刚开出来、还没被推过的那件：推进时当场停在 need_approval。
    st = _env()
    sender = _Sender()
    job = ingest.ingest_plan({"plan": _plan()}, chat_id="chat-1", store=st)
    res = runner.advance(job, execute=_executor(sender, job.job_id), store=st,
                         allow_effects=(READ_EFFECT,))
    check("没批准就推进 → 停 need_approval", res.stop == "need_approval", res.stop)
    check("没批准时发送器零调用", sender.calls == [], str(sender.calls))

    # ② 已经被`consume`推过一次（台账里已是 awaiting/approval）的那件：
    #    再推只回 awaiting —— 不许因为「又推了一次」就把闸门当成过了。
    st2 = _env()
    sender2 = _Sender()
    job_id = plan_block.consume(_plan(), chat_id="chat-1", store=st2)
    res2 = runner.advance(st2.load(job_id), execute=_executor(sender2, job_id), store=st2,
                          allow_effects=(READ_EFFECT,))
    check("已在等人时再推只回 awaiting", res2.stop == "awaiting", res2.stop)
    check("已在等人时仍然一次都没发", sender2.calls == [], str(sender2.calls))


def test_owner_yes_writes_approval_row() -> None:
    st = _env()
    job_id = plan_block.consume(_plan(), chat_id="chat-1", store=st)
    got = approval.apply_approval("可以", chat_id="chat-1", is_private=True,
                                  speaker_id="owner", store=st)
    check("「可以」落一条批准", len(got) == 1, str(got))
    a = got[0] if got else None
    check("批准指向这件事", a is not None and a.job_id == job_id, getattr(a, "job_id", None))
    check("批准指向这一步", a is not None and a.step_n == 1, getattr(a, "step_n", None))
    check("批准带着这一步的动作指纹", a is not None and len(a.action_hash) >= 8,
          getattr(a, "action_hash", ""))
    check("批准记了是谁点的头", a is not None and a.who == "owner", getattr(a, "who", ""))
    check("批准只在私聊里收", a is not None and a.channel == "private", getattr(a, "channel", ""))
    check("批准有过期时刻", a is not None and bool(a.expires_at), getattr(a, "expires_at", ""))


def test_approved_step_really_sends_once() -> None:
    st = _env()
    sender = _Sender()
    job_id = plan_block.consume(_plan(), chat_id="chat-1", store=st)
    approval.apply_approval("可以", chat_id="chat-1", is_private=True, speaker_id="owner", store=st)
    job = st.load(job_id)
    res = runner.advance(job, execute=_executor(sender, job_id), store=st,
                         allow_effects=(READ_EFFECT,))
    check("批完真跑起来", res.stop == "done", f"{res.stop} / {[(r.n, r.status, r.detail) for r in res.ran]}")
    check("发出去恰好一次", len(sender.calls) == 1, str(sender.calls))
    check("收件人来自计划里的目标", sender.calls and sender.calls[0][0] == "鲸鲸的测试群",
          str(sender.calls))
    check("正文来自计划里的正文", sender.calls and sender.calls[0][1] == "晚安", str(sender.calls))
    check("turn_id 是 job_id（回执能对回这件事）",
          sender.calls and sender.calls[0][2] == job_id, str(sender.calls))
    check("办完状态是 done", st.load(job_id).state == "done", st.load(job_id).state)


def test_second_advance_does_not_resend() -> None:
    st = _env()
    sender = _Sender()
    job_id = plan_block.consume(_plan(), chat_id="chat-1", store=st)
    approval.apply_approval("可以", chat_id="chat-1", is_private=True, speaker_id="owner", store=st)
    runner.advance(st.load(job_id), execute=_executor(sender, job_id), store=st,
                   allow_effects=(READ_EFFECT,))
    runner.advance(st.load(job_id), execute=_executor(sender, job_id), store=st,
                   allow_effects=(READ_EFFECT,))
    check("再推一次不重发（重复发送率 0）", len(sender.calls) == 1, str(sender.calls))


def test_group_yes_is_not_approval() -> None:
    st = _env()
    plan_block.consume(_plan(), chat_id="chat-1", store=st)
    got = approval.apply_approval("可以", chat_id="chat-1", is_private=False,
                                  speaker_id="owner", store=st)
    check("群里说「可以」不算批", got == [], str(got))


def test_deny_word_is_not_approval() -> None:
    st = _env()
    plan_block.consume(_plan(), chat_id="chat-1", store=st)
    got = approval.apply_approval("不行", chat_id="chat-1", is_private=True,
                                  speaker_id="owner", store=st)
    check("「不行」不落批准", got == [], str(got))


def test_failed_send_is_not_success() -> None:
    st = _env()
    sender = _Sender(status="failed")
    job_id = plan_block.consume(_plan(), chat_id="chat-1", store=st)
    approval.apply_approval("可以", chat_id="chat-1", is_private=True, speaker_id="owner", store=st)
    res = runner.advance(st.load(job_id), execute=_executor(sender, job_id), store=st,
                         allow_effects=(READ_EFFECT,))
    check("发失败不当成办成", res.stop == "failed", res.stop)
    check("发失败只试一次", len(sender.calls) == 1, str(sender.calls))


def test_static_locks() -> None:
    src = (ROOT / "core/jobs/plan_block.py").read_text(encoding="utf-8")
    check("开事阶段只放行只读（写步必须等批准）", "allow_effects=(READ_EFFECT,)" in src)
    tsrc = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    check("回合里对外发送要有发送器才放行", "allow_external=external is not None" in tsrc)
    check("回合里写步逐次确认走 authorize", "authorize=_authorize" in tsrc)


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"RESULT {PASSED}/{PASSED + FAILED} checks passed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
