from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import ask as A

FAILS: list[str] = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name, detail)


def main() -> int:
    print("== 问批准 ==")
    job = SimpleNamespace(chat_id="10000000004@chatroom", goal="发一句")
    send_step = SimpleNamespace(action="send:今天下雨记得带伞", effects=("external_send",))
    click_step = SimpleNamespace(action="gui_click:提交", effects=("ui_control",))
    read_step = SimpleNamespace(action="gui_state:微信", effects=("read",))

    text = A.template(job, send_step, target_name="鲸鲸的测试群")
    check("模板含地点", "鲸鲸的测试群" in text)
    check("模板含后果", "收不回来" in text)
    check("模板尾巴固定", A.APPROVE_HINT in text)
    check("点击后果不同", "改不回去" in A.template(job, click_step, target_name="订单页"))
    check("只读后果不同", "只是看一眼" in A.template(job, read_step, target_name="微信"))
    check("三要素齐 = 合格", A.complete(text, target_name="鲸鲸的测试群"))
    check("缺地点不合格", not A.complete("要不要我发一句？", target_name="鲸鲸的测试群"))
    check("完成式被抓", A.looks_like_done_claim("批准收到，我去发"))
    check("普通将来式不误判", not A.looks_like_done_claim("要不要我发这一句？"))

    asked = {}
    def gen(prompt):
        asked["p"] = prompt
        return "要不要我往鲸鲸的测试群发这一句？发出去就收不回来了。"

    got = A.build_ask(job, send_step, target_name="鲸鲸的测试群", generate=gen)
    check("模型写的那句被采用", "鲸鲸的测试群" in got and "收不回来" in got)
    check("尾巴补上", A.APPROVE_HINT in got)
    check("提示词带任务", "鲸鲸的测试群" in asked.get("p", ""))

    def gen_bad(prompt):
        return "已经发好了，你放心。"

    got2 = A.build_ask(job, send_step, target_name="鲸鲸的测试群", generate=gen_bad)
    check("完成式被拒 → 退回模板", got2 == A.template(job, send_step, target_name="鲸鲸的测试群"))

    def gen_boom(prompt):
        raise RuntimeError("模型挂了")

    got3 = A.build_ask(job, send_step, target_name="鲸鲸的测试群", generate=gen_boom)
    check("模型挂了 → 模板兜底", got3 == A.template(job, send_step, target_name="鲸鲸的测试群"))

    def gen_short(prompt):
        return "要不要我来一下？"

    got4 = A.build_ask(job, send_step, target_name="鲸鲸的测试群", generate=gen_short)
    check("三要素不齐 → 退回模板", "鲸鲸的测试群" in got4 and "收不回来" in got4)
    check("长度有上限", len(A.template(job, SimpleNamespace(action="send:" + "字" * 400, effects=("external_send",)), target_name="群")) <= A.MAX_ASK_CHARS)
    class Job:
        def __init__(self, job_id, state, kind, step):
            self.job_id = job_id
            self.state = state
            self.awaiting = SimpleNamespace(kind=kind)
            self._step = step

        def next_step(self):
            return self._step

    awaiting_step = SimpleNamespace(action="send:你好", effects=("external_send",), n=3)
    store = SimpleNamespace(
        open_jobs=lambda chat: [
            Job("job-a", "awaiting", "approval", awaiting_step),
            Job("job-b", "open", "none", awaiting_step),
            Job("job-c", "awaiting", "grant", awaiting_step),
        ]
    )
    got = A.pending_asks(store, "c1", target_name="鲸鲸的测试群")
    check("只挑在等批准的", [item["job_id"] for item in got] == ["job-a"])
    check("ref 绑到第几步", got and got[0]["ref"] == "job-a#3")
    check("问的话含地点与尾巴", got and "鲸鲸的测试群" in got[0]["text"] and A.APPROVE_HINT in got[0]["text"])
    check("问过的不再问", A.pending_asks(store, "c1", target_name="群", asked=["job-a#3"]) == [])
    check("没有会话就不问", A.pending_asks(store, "") == [])
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
