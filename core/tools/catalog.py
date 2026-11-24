"""工具目录：模型「看得见什么」的唯一属主。

真机教训（2026-10-21）：仓里曾有两份「工具目录」——执行器侧 `core/tools/builtin.py`
（10 个，含 `send_plan`）与模型侧 `agents/tools/base.py`（18 个只读），模型侧另有一张
手写白名单 `PLAN_LOOP_READ_ONLY_TOOLS` 决定「哪些能在计划里自动执行」。三处各说各话，
于是她答「我没有往群里发消息的工具」。

现在：**名单只在这里定义**，别处只许 import。
"""

from __future__ import annotations

# 可以在计划里「自动执行」的工具（只读 + 不发送）。
# 写/外发工具（send_message / create_reminder / notify_user / remember / send_sticker /
# create_todo / complete_todo / emit_event / correct_emoji_desc …）不在表内：
# 它们仍走显式确认路径。新工具默认不放行。
PLAN_LOOP_ALLOWED: frozenset[str] = frozenset({
    "acg_lookup", "analyze_image", "decision_helper", "fetch_url",
    "get_contact_info", "get_current_time", "get_person_profile",
    "get_persona",
    "get_today_analysis", "get_weather", "list_todos", "query_commands",
    "read_file",
    "recall_memories", "recall_thread", "search_knowledge",
    "search_messages", "search_threads", "search_vector", "web_search",
    # send_plan 不发送：它只把「往某处说一句」开成一件事、停在等主人批准。
    "send_plan",
    # gui_state 只读屏（UIA 控件树），不改任何东西。
    "gui_state",
})

# 计划循环里「只读」的那一部分（= PLAN_LOOP_ALLOWED 去掉两个非只读动词）。
PLAN_LOOP_READ_ONLY: frozenset[str] = frozenset(
    name for name in PLAN_LOOP_ALLOWED if name not in ("send_plan", "gui_state")
)


def allowed_in_plan(name: str) -> bool:
    """这个工具名能不能在计划里自动执行。"""
    return str(name or "").strip() in PLAN_LOOP_ALLOWED


def read_only_in_plan() -> frozenset[str]:
    """计划循环里的只读工具集合（副本）。"""
    return frozenset(PLAN_LOOP_READ_ONLY)
