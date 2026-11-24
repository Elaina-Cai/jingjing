"""任务证据：事实类问句也算「该查」（真机：杭州明天会下雨吗 → 没查、编了一句、撞指纹被丢）。"""
from __future__ import annotations

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agents.conversational as C  # noqa: E402

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


POSITIVE = ["杭州明天会下雨吗", "今天几号", "现在几点了", "帮我查一下汇率", "明天限号吗", "帮我查汇率"]
NEGATIVE = ["烦死了忙了一整天", "你好呀", "别查了我记得"]


def t_positive():
    for text in POSITIVE:
        check("该查：" + text, bool(C._TOOL_ASK_RE.search(text)))


def t_negative():
    for text in NEGATIVE:
        hit = bool(C._TOOL_ASK_RE.search(text))
        check("不该查：" + text, not hit, "命中")


def t_source():
    """问句识别词表：直接看运行时编译好的那条正则，不再切源码字符串。"""
    import agents.conversational as conv_module

    block = conv_module._TOOL_ASK_RE.pattern
    check("保留发消息类词", "带句话" in block)
    check("新增事实类词", "会下雨" in block and "气温多少" in block)


def main():
    for fn in (t_positive, t_negative, t_source):
        print(fn.__name__)
        fn()
    print("RESULT %d failed / %d checks" % (len(FAILS), COUNT))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
