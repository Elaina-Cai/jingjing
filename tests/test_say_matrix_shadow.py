"""M2 影子：Reading → 矩阵 → 与旧判据对照（只记日志，不改行为）。"""
from __future__ import annotations

import asyncio
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.intake.observation import Observation  # noqa: E402
from core.verdict import say_shadow as S  # noqa: E402

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name + " " + str(detail))


def obs(text="在吗"):
    return Observation(where="private", chat_id="c1", by="Elaina", text=text)


def run(coro):
    return asyncio.run(coro)


GOOD = {"quotes": ["在吗"], "slots": {}, "gaps": [], "timing": "", "affect": "neutral"}


def t_off():
    os.environ.pop(S.ENV, None)
    asked = []

    async def send(messages):
        asked.append(messages)
        return GOOD

    out = run(S.shadow_reading(observation=obs(), send=send, chat_id="c1"))
    check("默认 off 不花模型费", out.get("skipped") == "mode_off" and not asked)


def t_on():
    os.environ[S.ENV] = "1"
    seen = []

    async def send(messages):
        seen.append(messages)
        return GOOD

    out = run(S.shadow_reading(observation=obs(), send=send, chat_id="c1", old_intent="owner_private_message", old_would_reply=True))
    check("on 会问一次", len(seen) == 1 and isinstance(seen[0], list))
    check("有结论", out.get("ok") is True and out.get("disposition") in ("say", "ask", "do", "abstain"), out)
    out2 = run(S.shadow_reading(observation=obs("帮我查一下杭州天气"), send=send, chat_id="c1", old_intent="owner_private_message", old_would_reply=True))
    check("第二句也有结论", out2.get("ok") is True, out2)


def t_fail():
    os.environ[S.ENV] = "on"

    async def boom(messages):
        raise RuntimeError("boom")

    out = run(S.shadow_reading(observation=obs(), send=boom, chat_id="c1"))
    check("send 炸了不拖垮回合", out.get("skipped") == "send_failed")

    async def bad(messages):
        return "不是 JSON"

    out2 = run(S.shadow_reading(observation=obs(), send=bad, chat_id="c1"))
    check("解析失败只记日志", out2.get("skipped") == "unparsed")
    out3 = run(S.shadow_reading(observation=obs(), send=None, chat_id="c1"))
    check("没有 send 就跳过", out3.get("skipped") == "no_input")


def t_static():
    src = pathlib.Path("core/verdict/say_shadow.py").read_text(encoding="utf-8")
    check("不写账本", "terminal_events" not in src and "deliver(" not in src)
    check("不改行为（无 return 给主干的副作用）", "outbound" not in src and "send_and_verify" not in src)


def main():
    for fn in (t_off, t_on, t_fail, t_static):
        print(fn.__name__)
        fn()
    os.environ.pop(S.ENV, None)
    print("RESULT %d failed / %d checks" % (len(FAILS), COUNT))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
