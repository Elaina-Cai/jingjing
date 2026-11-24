"""任务证据闸：哪些话算「她要动手」，哪些不算。

真机两次踩空（2026-10-21）：
  ① 「在鲸鲸的测试群里说一句：…」——词表一个不命中 → on=False → 连 schema 都不给；
  ② 「提醒一下群里的人明天带伞」——同样不命中（「提醒」不在表里）。
两句话都要动手，所以都必须算证据；而「别提醒我」不算。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.conversational import _TOOL_ASK_RE, _TOOL_NEGATED_RE  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool) -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}")


def t_send_request() -> None:
    for text in (
        "在鲸鲸的测试群里说一句：明天带伞",
        "帮我发一句「我晚点到」给小奇",
        "转告一下群里的人",
        "说一声明天开会",
    ):
        check(f"发消息类算证据：{text[:10]}", bool(_TOOL_ASK_RE.search(text)))


def t_remind_is_task() -> None:
    text = "提醒一下群里的人明天带伞"
    check("提醒算任务证据", bool(_TOOL_ASK_RE.search(text)))
    check("「别提醒我」被否定压住", bool(_TOOL_NEGATED_RE.search("别提醒我带伞")))
    check("「不用提醒」被否定压住", bool(_TOOL_NEGATED_RE.search("不用提醒我")))


def t_chitchat_not_task() -> None:
    check("天气闲聊不算（「今天好累」）", not _TOOL_ASK_RE.search("今天好累"))
    check("陪伴闲聊不算（「在吗」）", not _TOOL_ASK_RE.search("在吗"))


def t_fact_question_is_task() -> None:
    check("需要外部事实的问题算证据", bool(_TOOL_ASK_RE.search("杭州明天会下雨吗")))


def main() -> int:
    for fn in (t_send_request, t_remind_is_task, t_chitchat_not_task, t_fact_question_is_task):
        print(fn.__name__)
        fn()
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
