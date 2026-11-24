r"""真机驱动：把「一件事」从开到办完走一遍（不依赖模型输出判决 JSON）。

为什么需要它：判 → 一件事 这条线的总闸是
``agents/conversational.py::audit_verdict_contract``，而它在旧 pipeline 里**没有
调用点**（模型还不输出判决 JSON）。本脚本绕开模型，直接喂一份**合法载荷**，
把台账、推进器、批准闸、回执、读数、事件序列在真机环境中逐段点亮。

它只碰两样东西：
1. 台账（``jobs`` / ``job_events`` / ``job_approvals``，append-only）；
2. 只读工具（第 1 步真读一次 UIA 元素表；带副作用的步骤交给**假执行器**，
   一次点击都不做、一个字节都不发）。

用法::

    python -X utf8 -m scripts.job_drive --allow-production [--chat CHAT_ID] [--leave-awaiting] [--json]

退出码：0 = 全链路走通；1 = 有断言不成立。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

# 必须在 import core.* 之前：台账与推进器都按档位决定做不做
os.environ.setdefault("YINYUE_FEATURE_JOB_LEDGER", "on")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OWNER_CHAT = "wxid_seed01"
GOAL = "（自检驱动 {tag}）把一件事从开到办完走一遍"
GOAL_LIVE = "（自检驱动 {tag}）留一件在等你批准"


def _goal(template: str, tag: str) -> str:
    """每次都换一个目标：`job_id` 是 `hash(chat, trace, goal)`，
    同一个目标第二次跑会**命中上一次那件**（幂等），演示就变成「推一件旧事」。"""
    return template.format(tag=tag)


def _payload(goal: str, *, live: bool = False) -> dict[str, Any]:
    """一份合法的判决载荷：三小步，第二步要人批准。

    ``live=True`` 时第二步故意用**只读工具**（`gui_state`）配 `ui_control` 档：
    真机上批准后执行器会调 `act()`，工具当场拒绝（它不是写动作），
    于是一个字节都不发、一次点击都不做——用来演示「批准闸真的会放行」而不动真机。
    """
    second = "gui_state:微信" if live else "gui_click:点一下搜索结果"
    return {
        "plan": {
            "goal": goal,
            "steps": [
                {"n": 1, "action": "gui_state:微信", "effects": "read"},
                {"n": 2, "action": second, "effects": "ui_control"},
                {"n": 3, "action": "gui_state:微信", "effects": "read"},
            ],
        }
    }


class Recorder:
    """假执行器：只记账，不动真机。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, step: Any) -> dict[str, Any]:
        self.calls.append(f"{step.n}:{step.action}")
        return {
            "ok": True,
            "changed": {"simulated": step.n},
            "state_after": f"simulated#{step.n}",
            "detail": "驱动脚本未动真机",
        }


def _run(args: argparse.Namespace) -> int:
    if not args.allow_production:
        print(
            "这个脚本会往生产库写台账（jobs / job_events / job_approvals）。确认要这么做请加 --allow-production。"
        )
        return 2
    # 操作员工具（不是测试/探测）：显式声明可写生产库，理由与
    # scripts/reimport_speech_seed.py 同一类——它是**人手动跑的**真机驱动，
    # 库必须是服务正在用的那一个，否则驱动出的东西看不见。
    from db import write_guard

    write_guard.declare_writes_allowed(declared_by="scripts/job_drive.py")

    from core.jobs import approval as approval_mod
    from core.jobs import executor, ingest, runner
    from core.jobs.store import default_store
    from core.tools.builtin import build_registry

    store = default_store()
    registry = build_registry()
    problems: list[str] = []

    def check(name: str, ok: Any, detail: Any = "") -> bool:
        good = bool(ok)
        line = f"  [{'ok' if good else 'FAIL'}] {name}"
        if not good and detail != "":
            line += f" — {detail}"
        print(line)
        if not good:
            problems.append(name)
        return good

    print(f"chat={args.chat}")
    goal = _goal(GOAL, args.tag)
    job = ingest.ingest_plan(
        _payload(goal),
        chat_id=args.chat,
        store=store,
        trace_id="job_drive",
        owner_person_id=args.chat,
    )
    if not check("开出一件事", job is not None, "ingest_plan 返回 None（档位不是 on？）"):
        return 1
    assert job is not None
    print(f"  job_id={job.job_id} steps={len(job.steps)}")

    read_exec = executor.read_only_executor(registry, turn_id=job.job_id, target=args.chat)
    res1 = runner.advance(job, execute=read_exec, store=store, allow_effects=("read",))
    check("第 1 步（read）真跑完", res1.ran and res1.ran[0].status == "done", res1.ran)
    check("停在「等批准」而不是硬跑", res1.stop == "need_approval", res1.stop)
    check(
        "没批准的步一步没执行",
        [r.status for r in res1.ran].count("done") == 1,
        [(r.n, r.status) for r in res1.ran],
    )

    parked = store.load(job.job_id) or res1.job
    check(
        "台账状态 awaiting/approval",
        parked.state == "awaiting" and parked.awaiting.kind == "approval",
        (parked.state, parked.awaiting.kind),
    )
    receipts = runner.pending_approval_receipts(res1)
    check("产出 pending_approval 回执", len(receipts) == 1, receipts)
    readouts = runner.readouts_for_prompt(store, args.chat)
    # 读数会按 60 字截断，所以只判「这件在那」的短标记
    check(
        "读数取得到（下一回合会进判的 prompt）",
        any(args.tag in r and "等批准" in r for r in readouts),
        readouts,
    )
    if readouts:
        print(f"  读数：{readouts[0]}")

    ap = approval_mod.parse_approval("可以", parked, is_private=True, speaker_id=args.chat)
    check("私聊本人一句「可以」判成批准", ap is not None, "parse_approval 返回 None")
    check("群里说不算批准", approval_mod.parse_approval("可以", parked, is_private=False) is None)
    check(
        "「算了」否定优先",
        approval_mod.parse_approval("算了", parked, is_private=True, speaker_id=args.chat) is None,
    )
    check(
        "别人说不算批准",
        approval_mod.parse_approval("可以", parked, is_private=True, speaker_id="wxid_other")
        is None,
    )
    recorded = approval_mod.record_approval(parked, ap, store=store) if ap is not None else False
    check("批准落库", recorded, recorded)
    resumed = store.load(job.job_id) or parked
    check("批准后回到 open", resumed.state == "open", resumed.state)

    rec = Recorder()
    res2 = runner.advance(resumed, execute=rec, store=store, allow_effects=("read",))
    check("批了才跑第 2 步", rec.calls[:1] == ["2:gui_click:点一下搜索结果"], rec.calls)
    check("跑到停因 done", res2.stop == "done", res2.stop)
    done = store.load(job.job_id) or res2.job
    check("终态 done", done.state == "done", done.state)
    kinds = store.event_kinds(job.job_id)
    check(
        "事件序列 created→…→done",
        list(kinds[:1]) == ["created"] and "done" in kinds and "step_done" in kinds,
        kinds,
    )

    if args.leave_awaiting:
        live = ingest.ingest_plan(
            _payload(_goal(GOAL_LIVE, args.tag), live=True),
            chat_id=args.chat,
            store=store,
            trace_id="job_drive_live",
            owner_person_id=args.chat,
        )
        if live is not None:
            res3 = runner.advance(live, execute=read_exec, store=store, allow_effects=("read",))
            print(f"  留一件等你批准：{live.job_id} stop={res3.stop}")

    print(f"RESULT {len(problems)} failed / 16 checks")
    if args.json:
        print(
            json.dumps(
                {
                    "chat": args.chat,
                    "job_id": job.job_id,
                    "problems": problems,
                    "events": list(kinds),
                },
                ensure_ascii=False,
            )
        )
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真机驱动一件「在手的事」")
    parser.add_argument("--chat", default=OWNER_CHAT, help="会话 id（默认主人的私聊）")
    parser.add_argument(
        "--leave-awaiting", action="store_true", help="额外留一件在等你批准（用于看读数进 prompt）"
    )
    parser.add_argument("--json", action="store_true", help="末尾打一行 JSON")
    parser.add_argument(
        "--allow-production", action="store_true", help="允许写生产库台账（操作员工具，非测试）"
    )
    parser.add_argument(
        "--tag",
        default=time.strftime("%H%M%S"),
        help="目标里的标记（每次换一个，避免命中上一次那件事）",
    )
    return _run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
