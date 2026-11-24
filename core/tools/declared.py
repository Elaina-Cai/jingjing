"""模型侧工具的**声明**（只声明，不实现）——M3 第三刀。

背景：模型能调 23 个工具，执行器目录只有 10 个，交集 5 个。两份目录互不知道对方，
于是出现「工具能登记、清单能进 prompt、但没人真调」。

做法：**声明收归这里**（单一目录 = 一份数据），实现仍留在 `agents/tools/`
（layering 规则 10 禁止 core import agents，所以绑定必须从 agents 侧反向往里注册）。
`invoke()` 只对已绑定处理器的 spec 有效；本表先保证「目录里看得见」。

效果一律按最保守的可逆性登记：不确定的当 `read`，但只要涉及对外发送就是
`external_send`（宁可多问一次，也不能把「发出去」说成「看一眼」）。
"""

from __future__ import annotations

import logging

from core.tools.registry import Effect, ToolSpec

log = logging.getLogger(__name__)

# (name, effect, 一句什么时候用它（<=40 字）)
DECLARED: tuple[tuple[str, str, str], ...] = (
    ("acg_lookup", "read", "查番剧/动画/游戏/漫画角色的【静态设定】——性格、身世、能力、萌点时优先用我（"),
    ("analyze_image", "read", "看图片内容：文字、场景、物体、表情、情绪。用户刚发了一张图或一个表情包时，直接调"),
    ("complete_todo", "write_local", "完成一个待办事项。当用户说某个任务完成了时调用。"),
    ("correct_emoji_desc", "write_local", "纠正表情包描述。当发现对方发表情包后自动识别的描述不准确时，用此工具修正。"),
    ("create_reminder", "write_local", "创建一个定时提醒，到时间后鲸鲸会在来源会话里提醒。群里任何人（不只是主人）明确请"),
    ("create_todo", "write_local", "创建待办事项。当用户提到需要做的事、任务、计划时调用。"),
    ("decision_helper", "read", "决策辅助：分析用户面临的抉择，生成各选项的优缺点、打分和推荐。用户纠结/选择困难"),
    ("get_contact_info", "read", "查询联系人信息：备注名、昵称、是否群聊、最近消息摘要。可传wxid或昵称/备注名"),
    ("get_stance_cards", "read", "查鲸鲸写下的立场卡：对某群/某人的态度约定"),
    ("get_today_analysis", "read", "获取今日微信分析结果：谁找了我（私聊重要性）、群聊摘要、每日日报。用户让你整理微"),
    ("get_weather", "read", "查询天气信息：当前天气、温度、湿度、风力、未来预报。用户问天气时调用。"),
    ("list_todos", "read", "查看待办事项列表。当用户问有什么待办、任务列表时调用。"),
    ("memory_recall", "read", "按需记忆：当前窗口里没有你要的事（事实/旧事/人/梗/原话）时查一次"),
    ("notify_user", "external_send", "给用户发送系统通知弹窗，用于紧急消息提醒"),
    ("query_commands", "read", "查询命令/提问/请求台账。群成员查询本群命令时用 group_id；普通联系人只"),
    ("read_emotion", "read", "查此刻心境与对某人的态度（跨会话合并）"),
    ("read_memory", "read", "读某条记忆全文：提示词里只给索引 key（m1、m2…），要细节就按 key"),
    ("read_file", "read", "读项目里的一份文档/产出（只读，限 docs/ 与 output/）。找路径、看索引时用"),
    ("recall_thread", "read", "召回某个话题线程的完整消息内容。当你需要了解某个话题的具体讨论细节时调用。先用s"),
    ("search_messages", "read", "搜索历史消息，可按联系人、群聊、时间范围、关键词过滤。chat_id 可以传 w"),
    ("search_threads", "read", "搜索群聊中的话题线程。当你需要了解某个群在讨论什么、某个项目/事件的讨论进展时调"),
    ("send_message", "external_send", "发送消息或文件给指定联系人或群聊。需要对应平台客户端已连接。"),
    ("ui", "ui_control", "动界面：点一下 / 打字 / 跳转。只在人明确要求操作界面时用。"),
    ("ui_state", "read", "看一眼界面：读窗口控件树。当你需要知道屏幕上有什么时用。"),
    ("send_sticker", "external_send", "根据当前对话情境选择最合适的表情包发送；轻松、有情绪或气氛合适时优先主动发一张。"),
)

_EFFECT_MAP = {
    "read": Effect.READ,
    "write_local": Effect.WRITE_LOCAL,
    "ui_control": Effect.UI_CONTROL,
    "external_send": Effect.EXTERNAL_SEND,
}


def declared_specs() -> tuple[ToolSpec, ...]:
    """把声明变成 spec（不含处理器，绑定由 agents 侧完成）。"""
    out: list[ToolSpec] = []
    for name, effect, purpose in DECLARED:
        eff = _EFFECT_MAP.get(effect)
        if eff is None:
            log.info("declared_unknown_effect name=%s effect=%s", name, effect)
            eff = Effect.EXTERNAL_SEND  # fail closed
        out.append(ToolSpec(name=name, family="model", effects={eff}, args={}, purpose=purpose,
                          returns="text", owner="agents/tools"))
    return tuple(out)


def declared_names() -> frozenset[str]:
    """本表覆盖的工具名。"""
    return frozenset(name for name, _, _ in DECLARED)
