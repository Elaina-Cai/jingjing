"""槽抽取：缺哪个参数就问哪个（不靠说法，靠槽）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import slots as S

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if cond:
        print("  ok   " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name + " " + str(detail))


def t_chat():
    check("帮她发一句缺 chat", S.missing_slots("帮她发一句我晚点到") == ["chat"])
    check("群里缺 chat", "chat" in S.missing_slots("提醒一下群里的人明天带伞"))
    check("写了群名就不缺", S.missing_slots("往「鲸鲸的测试群」说一句：明天带伞") == [])
    check("群 id 也算写了", S.missing_slots("往 10000000004@chatroom 发一句：在") == [])
    check("known 名单命中", S.missing_slots("往鲸鲸的测试群说一句：在", known=("鲸鲸的测试群",)) == [])


def t_time():
    check("到点提醒我缺 time", S.missing_slots("到点提醒我") == ["time"])
    check("今晚九点不缺", S.missing_slots("今晚九点提醒我吃药") == [])
    check("明早不缺", S.missing_slots("明早提醒我带伞") == [])


def t_path():
    check("那个资料缺 path", S.missing_slots("上次说的那个资料发我一下") == ["path"])
    check("给了路径不缺", S.missing_slots("把 D:\\out\\a.pdf 发我") == [])


def t_not_actionable():
    check("闲聊不判缺", S.missing_slots("今天好累") == [])
    check("问句不判缺", S.missing_slots("我们明天几点集合") == [])


def t_ask_for():
    check("单槽问一句", "哪个群" in S.ask_for(["chat"]))
    check("多槽一次问全", len(S.ask_for(["time", "path"]).split(chr(65307))) == 2)
    check("无槽不问", S.ask_for([]) == "")


def main():
    for fn in (t_chat, t_time, t_path, t_not_actionable, t_ask_for):
        fn()
    print("RESULT %d failed / %d checks" % (len(FAILS), COUNT))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
