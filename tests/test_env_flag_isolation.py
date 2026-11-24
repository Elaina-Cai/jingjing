"""测试进程 × 生产 ``.env`` 开关隔离守卫（2026-10-02）。

背景：同一个坑咬了三次
----------------------
``core.bootstrap.init_dirs()`` 会调 ``core.env.load_yinyue_env_defaults()``，
它在 key **缺失**时把 ``.env`` 写进 ``os.environ``——于是生产配置成了测试的
**隐式输入**：

1. ``.env`` 打开 ``YINYUE_FEATURE_PAT_GRACE=on`` → ``test_trigger_grace`` /
   ``test_pat_sync`` 的"flag 关=旧行为"断言失真（先靠 import 期钉死兜住）；
2. ``.env`` 打开 ``YINYUE_FEATURE_SEMANTIC_UNIT=on`` → **同样两个文件又红**：
   ``should_defer_pure_pat()`` = ``PAT_GRACE`` **or** ``SEMANTIC_UNIT``，只钉一个不够；
3. 机制始终相同：生产 ``.env`` 成了测试的隐式输入。

本文件是那条机制的守卫，四件事：

* **① 机制**：测试进程里 ``.env`` 的开关类键**不注入**（资料类键照旧），
  直跑 ``tests/test_*.py`` 也认——用子进程实测，不靠读代码；
* **② 分类**：``core/features.py`` 里**被读过**的每个开关都要在下面的兄弟表里
  登记（新开关不加表 → 红）；同一布尔表达式里一起读的两个开关必须是同一组
  （新开关接在 ``PAT_GRACE or X`` 旁边 → 红）；
* **③ 纪律**：``tests/test_*.py`` 里凡是显式钉了兄弟组某个开关的，必须把**整组**
  都显式钉死（"flag 关 = 旧行为"的断言不许被兄弟开关改写）；
* **④ 变异自检**：把机制改回"照抄 ``.env``"（``core/env.py`` 的
  ``skip_switch_injection`` 恒返回 ``False``）→ 本文件必须红；
  还原 → 复绿。实测记录写在下面 §变异自检。

Run directly:
    python tests/test_env_flag_isolation.py
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _tempdirs import make_db_dir  # noqa: E402

from core.env import (  # noqa: E402
    SWITCH_KEYS,
    TEST_NO_ENV_FLAGS,
    is_switch_key,
    load_yinyue_env_defaults,
    skip_switch_injection,
)

# ---------------------------------------------------------------------------
# 兄弟表（**唯一**声明处：加开关 / 加连线时先改这里）
# ---------------------------------------------------------------------------
# 一组 = "其中任意一个都能把同一段行为换成另一套"的开关。判据两种，取并集：
#
# * **代码证据**：同一个布尔表达式里一起读（如 ``PAT_GRACE or SEMANTIC_UNIT``，
#   见 ``legacy_brain/group_state.py::should_defer_pure_pat``）——:func:`boolean_sibling_pairs`
#   每次跑测试现场推导，推导出来的对**必须**落在同一组里，推导不出来的组是
#   保守的超集（多钉一个开关不会错，少钉一个就是这次的坑）；
# * **历史证据**：用例一直以来成对钉死的（如 ``MEMORY_GATE`` ↔ ``UNCERTAIN_VOICE``，
#   ``REPLY_INTENT`` ↔ ``ADMISSION_MODE``——意图层与准入合并成同一次调用）。
# 2026-11-17 清理（stale 断言实测）：下面 8 个开关在生产代码里已经没有读点，只有
# tests/、scripts/ 和 core/features.py 的注释里还提到它们，故不在本表里分类：
#   ANTI_AI_PHRASE / CONTEXT_ASSEMBLER / GROUP_COALESCE / MEMORY_BLOCK_INJECT /
#   MEMORY_VECTOR_RECALL / RESIDUAL_TAIL_GUARD / ROOM_STATE / SHORT_REPLY_SEED
# core/features.py 的声明还在；哪天它们被重新读，unknown 断言会立刻红并要求重新分类。
SIBLING_GROUPS: tuple[tuple[str, ...], ...] = (
    # 纯拍挂起 / 群聊合并触发：route 要不要直回这一拍 = PAT_GRACE or SEMANTIC_UNIT
    # （legacy_brain/group_state.py::should_defer_pure_pat / process_message）。
    ("YINYUE_FEATURE_PAT_GRACE", "YINYUE_FEATURE_SEMANTIC_UNIT"),
    # 召回闸门与不确定表达：同一段 [拿不准时] / 负知识块，历史上总是成对钉死。
    ("YINYUE_FEATURE_MEMORY_GATE", "YINYUE_FEATURE_UNCERTAIN_VOICE"),
    # 人物记忆链：写侧 / 读侧 / 抽取 / 聚合（scheduler/person_memory.py、
    # core/memory/providers.py：读侧要求写侧同时开）。
    ("YINYUE_FEATURE_PERSON_MEMORY", "YINYUE_FEATURE_PERSON_RECALL",
     "YINYUE_FEATURE_PERSON_LLM", "YINYUE_FEATURE_PERSON_PROFILE_AGGREGATE"),
    # WorkItem 台账：run 没有 ledger 行就是孤儿（core/tasks/store.py::runs_enabled）。
    ("YINYUE_FEATURE_WORK_ITEM_LEDGER", "YINYUE_FEATURE_WORK_ITEM_RUNS"),
    # 对话影子 / 主动灰度（core/routing/router.py::route）。
    ("YINYUE_FEATURE_CONVERSATION_SHADOW", "YINYUE_FEATURE_CONVERSATION_ACTIVE"),
)

#: 明确声明"没有兄弟"的开关：单独钉它一个就够（读它的人只有一处、不与他开关共判定）。
#: **新开关不许默认落这里**——要么进上面的组，要么在这里写一行并给理由。
STANDALONE_SWITCHES = frozenset({
    "YINYUE_FEATURE_CORRECTION",
    "YINYUE_FEATURE_HANDOFF",
    "YINYUE_FEATURE_GUI_BACKENDS",           # 更正：真发一条改口消息（U-26）
    "YINYUE_FEATURE_CONTEXT_BUDGET",
    "YINYUE_FEATURE_CHAT_WINDOW",           # 轮次窗口；白名单 YINYUE_CHAT_WINDOW_CHATS 是资料
    "YINYUE_FEATURE_SEARCH_DEEPSEEK",       # ⑩ 外部检索：DeepSeek Search/Fetch（三态，默认 off）
    # E1（U-02）输出契约校验：唯一读点 ``agents/conversational.py::audit_verdict_contract``；
    # off 连载荷都不看，shadow 只记 verdict_contract_check，on 才抛。
    "YINYUE_FEATURE_VERDICT_CONTRACT",
   # 阶段 6 微信写侧（三态，默认 off）：唯一读点 `core/execution/gui/wechat.py::_mode`；
   # off 连窗口都不找，shadow 只读库，on 才填字（仍然不按发送）。
   "YINYUE_FEATURE_WECHAT_UI",
    "YINYUE_FEATURE_DECLARED_TOOLS",
    "YINYUE_FEATURE_MCP_TOOLS",
    "YINYUE_FEATURE_LEGACY_VIA_EXIT",
    "YINYUE_FEATURE_WECHAT_SEND",
    "YINYUE_FEATURE_WECHAT_REVOKE",
    "YINYUE_FEATURE_DEVICE_LEASE",
    # P1-1 销账（三态，默认 off）：唯一读点 ``core/context/intention_close.py::close_mode``；
    # off 不算销账、shadow 只记、on 才关开放环。
    "YINYUE_FEATURE_OPEN_LOOP_CLOSE",
    # P3-5 点名即发（三态，默认 off）：唯一读点 ``loop/direct_send.py::mode``；
    # off 不解析、shadow 只记、on 才真投递（只认私聊、只认主人、每条一次）。
    "YINYUE_FEATURE_DIRECT_SEND",
    # E3（U-10）「在手的事」台账：唯一读点 ``core/jobs/store.py::_mode``；
    # off 写读全 no-op，shadow 只写不读（readouts 返回空），on 才进 prompt。
    "YINYUE_FEATURE_JOB_LEDGER",
    "YINYUE_FEATURE_TOOL_PACKAGE_PROMPT",
    "YINYUE_FEATURE_TOOL_LOOP",
    "YINYUE_FEATURE_SENSORY",
    "YINYUE_FEATURE_DELIVERY_CONTRACT",
    "YINYUE_FEATURE_GUI_CHANNEL",
    # ⑥ 按需记忆（⑨→③→⑥，三态，默认 off）：唯一读点
    # ``agents/conversational.py::_needs_memory_block``，只回答“⑥ 要不要把
    # ④ 落进 extra 的命中渲成块进 system prompt”，不改路由、不改
    # 判定，与 [事件记忆] 那个 MEMORY_BLOCK_INJECT 门是两个独立门（后者锁
    # 窗口回合），单独钉它一个就够。
    # P3 召回链（三态，默认 off）：唯一读点 ``core/memory/recall_chain.py::recall_chain_mode``；
    # 只决定「⑨ 之前要不要多路召回 + ±6 行窗口 + 机械重排」，off 不建索引、不加载模型。
    "YINYUE_FEATURE_RECALL_CHAIN",
    # 2026-10-18（task-43）：寻址边三态；off 不采集不渲染（逐字旧行为），
    # shadow 只写 utterance_edges，on 才在窗口渲染 `↳回 X`。
    # 唯一读点 ``core/terminal/utterance_edges.py::edges_mode``。
    "YINYUE_FEATURE_UTTERANCE_EDGES",
    # E0 判决行留痕：off 不写 / shadow 写行带 mode / on 进报表（唯一读点
    # ``core/terminal/turn_trace.py::_mode``）。
    "YINYUE_FEATURE_TURN_TRACE",
    # task-45 沉默判据接组门（三态，默认 off）：唯一读点
    # ``core/routing/relevance.py::silence_mode``；off 不读判据、零新日志，
    # shadow 只写 silence_would_skip，on 才在 MODEL 出口把两处 silence 换成 SKIP。
    # task-49 事件流 actor 归一（三态，默认 off）：唯一读点
    # ``core/terminal/event_store_db.py::_event_to_values``；off 逐字旧行为、
    # 零新日志，shadow 只记 actor_identity_shadow，on 才把 person id 写进
    # ``actor_person_id``（``actor_account_id`` 永远保留平台号）。
    "YINYUE_FEATURE_ACTOR_IDENTITY_NORMALIZE",
    # task-26 媒体基质：采集侧落库（off 默认）与召回通道；两侧都只读 env，
    # 由 core.features.mode 统一读，因此归入 STANDALONE_SWITCHES。
    "YINYUE_FEATURE_MEDIA_STORE",
    "YINYUE_FEATURE_MEDIA_RECALL",
    # P2 多粒度召回：情节级滑窗向量（三态，默认 off）；唯一读点
    # ``core/memory/recall_chain.py::episode_tracing``。
    "YINYUE_FEATURE_RECALL_EPISODE",
    # task-33 类型无关原语（三态，默认 off）：唯一读点
    # ``core/memory/recall_chain.py::recall_primitives_mode``；只决定召回链里
    # 「词法层用不用两路 BM25 替掉 keyword/fts」以及「语义层加不加轮/段向量」，
    # off 时逐字等于 +RECALL_CHAIN（不建 token、不建索引、不载模型）。
    "YINYUE_FEATURE_RECALL_PRIMITIVES",
    # P3 结构化召回通道（三态，默认 off）：唯一读点
    # ``core/memory/recall_chain.py::recall_struct_mode``；只决定召回链里要不要加第 4 路
    # （附件名 / 数字串 / 日期串 / @尾巴 × 近因先验），off 逐字等于 +RECALL_CHAIN。
    "YINYUE_FEATURE_RECALL_STRUCT",
    # ①³ 写侧随手记（三态，默认 off）：唯一读点
    # ``core/memory/note_writer.py::worth_remembering_mode``；只决定「写侧观察器要不要
    # 产出候选 / 落量具日志」，off 不产生候选也不写日志，不碰召回与生成。
    "YINYUE_FEATURE_WORTH_REMEMBERING",
    # ①³ notes 双条件机械闸（三态，默认 off）：唯一读点
    # ``core/memory/note_recall.py::notes_gate_mode``；只决定「cue 词命中且问句 token
    # 在窗口里覆盖率 <0.35 时，要不要把随手记并进候选」，off 逐字等于不看 notes。
    "YINYUE_FEATURE_NOTES_GATE",
    # P0-1 表情候选（三态，默认 off）：唯一读点
    # ``core/execution/wx_sticker_sender.py::sticker_candidates_mode``；只决定
    # 「表情选择是按 enum 精确匹配还是按自由描述取候选」，off 逐字旧行为。
    "YINYUE_FEATURE_STICKER_CANDIDATES",
    "YINYUE_FEATURE_GROUP_ABSORB_ANSWERED",
    "YINYUE_FEATURE_MEMORY_PROPOSALS_V2",
    "YINYUE_FEATURE_MEMORY_SINKING",
    "YINYUE_FEATURE_PERSON_NOTES",
    "YINYUE_FEATURE_PERSON_PROFILE_TOOLS",
    "YINYUE_FEATURE_ATTRIBUTION",
    # 会话内感知（Step 2b）：唯一读点 agents/conversational.py::_perceive_tool_on；
    # 默认关，两态开关（不进三态表）。
    "YINYUE_FEATURE_PERCEIVE_TOOL",
    # 灰度期只让私聊走 perceive（作用域白名单 YINYUE_PERCEIVE_TOOL_CHATS 是
    # 会话名单、不是开关，不进这张表）。
    "YINYUE_FEATURE_PERCEIVE_TOOL_PRIVATE",
    # WP-STATE 状态装配 v1：唯一读点 core/context/state_assembly.py；默认 off。
    "YINYUE_FEATURE_STATE_ASSEMBLY",
    # 出站节奏：owner core/speech/rhythm.py，调用点在 agents/tools/messaging.py；
    # 默认 off，作用域名单显式才开。
    "YINYUE_FEATURE_OUTBOUND_RHYTHM",
    # 出站文本改写：只剩 OUTPUT_GATE 一条线（ADDRESSEE_GUARD 随 R0 删门移除），无兄弟。
    "YINYUE_FEATURE_OUTPUT_GATE",
    "YINYUE_FEATURE_DECISION_TRACE",
    "YINYUE_FEATURE_AFFECT",
    "YINYUE_FEATURE_AFFECT_PROMPT",
    "YINYUE_FEATURE_EVIDENCE_INDEX",
    "YINYUE_FEATURE_OUTBOUND_RECONCILE",
    "YINYUE_TURNS_MODE",
    "YINYUE_TURNS_TIMESTAMP",
    "YINYUE_ANSWER_LINKS",
    "YINYUE_FEATURE_CORRECTION_LEARNING",
    "YINYUE_FEATURE_MEDIA_REGISTRY",
    # 人设通道（批B Step B3）：唯一读点
    # ``core/execution/persona_channel.py::persona_channel_mode``（三态 off|shadow|on，
    # 默认 off）。**不与他开关共布尔判定**：它只决定"⑥ 要不要经⑩读人设摘要进规划
    # 上下文"，独立开关、单独钉它一个就够。
    "YINYUE_FEATURE_PERSONA_CHANNEL",
    # 媒体进单元（R-A12 P1b，2026-10-06）：唯一读点
    # ``legacy_brain/group_state.py::unit_media_enabled``（三态 off|shadow|on，默认 off）。
    # **不与他开关共布尔判定**：它自己先把 ``SEMANTIC_UNIT`` 当**前置条件**读
    # （``if not semantic_unit_enabled(): return False``，不是 ``or``/``and`` 表达式），
    # 所以"钉了 SEMANTIC_UNIT=off 的文件"天然免疫本开关——正因如此它不进
    # PAT_GRACE×SEMANTIC_UNIT×GROUP_COALESCE 那一组（进组会要求 17 个只钉三个的
    # 测试文件再补一钉，收益为零）；而钉了本开关的文件（tests/test_unit_media.py）
    # 把整组四个都显式钉死了。
    "YINYUE_FEATURE_UNIT_MEDIA",
    # 群内回合归属 / 续写识别（R-A12 **P1c**，R-A11，2026-10-06；**S4 改名/收口**）：
    # 唯一读点是 ``core/context/intent.py::turn_ownership_enabled``（三态
    # off|shadow|on，**默认 off**）。
    # **旧名退役**：本开关首登时叫 ``YINYUE_FEATURE_UNIT_CONTINUATION``（当时的读点在
    # ``legacy_brain/group_state.py::unit_continuation_enabled``），S4 起那个名字在
    # ``core/features.py`` 与 ``.env`` 都已移除、全仓无读点；**读点也早已搬到
    # ``core.context.intent``**（S1 搬家：判据、阈值、取字口径、四判据先后与短路
    # 一并搬去，``legacy_brain.group_state`` 只剩"喂事实 + 封口时调一次"）。
    # **不与他开关共布尔判定**，理由与上面 ``UNIT_MEDIA`` 逐字相同：它把
    # ``SEMANTIC_UNIT`` 当**前置条件**先读（``if not semantic_unit_enabled(): return False``，
    # 不是 ``or``/``and`` 表达式），所以钉了 ``SEMANTIC_UNIT=off`` 的文件天然免疫；
    # 而钉了本开关的文件（``tests/test_unit_continuation.py``）把整组四个
    # （``PAT_GRACE × SEMANTIC_UNIT × GROUP_COALESCE × TURN_OWNERSHIP``）都显式钉死。
    # 后台周期任务：各自有自己的开关断言（test_scheduler_jobs 三个一起钉死）。
    "YINYUE_FEATURE_OUTBOX_MAINTENANCE",
    "YINYUE_FEATURE_TERMINAL_RECONCILE",
    "YINYUE_FEATURE_TERMINAL_RECONCILE_APPLY",
    # N2 规划-执行两层循环（R-C1..R-C7，2026-10-06）：唯一读点
    # ``agents/planner.py::plan_loop_mode``（三态 off|shadow|on，默认 off）。
    # **不与他开关共布尔判定**——它是「这一层要不要接管」的总闸，
    # 不与 routing / 出站 / 群状态那些开关同一次判定（按本文件 §② 的口径属"没有兄弟"）。
    "YINYUE_FEATURE_PLAN_LOOP",
    # 动作规则引擎（N4，``core/rules/engine.py``，2026-10-06）：唯一读点
    # ``RuleEngine.mode()``（三态 off|shadow|on，**默认 off**）。
    # **不与他开关共布尔判定**——它是「动作前置条件要不要接管」的总闸，
    # 不与 routing / 出站 / 群状态那些开关同一次判定（按本文件 §② 的口径属"没有兄弟"）。
    "YINYUE_FEATURE_RULES",
    # N3 话题簇与锚点（焦点状态，2026-10-06 第一期）：唯一读点是
    # ``core/context/threads.py::threads_mode``（三态 off|shadow|on，**默认 off**；
    # 其余地方只读 ``threads_enabled()`` 这个布尔）。
    # **不与他开关共布尔判定**——它是「跨轮的这一轮在聊谁的事」这一层要不要接管，
    # 不与 routing / 出站 / 群状态 / 装配那些开关同一次判定（本文件 §② 的口径）。
    # 理由照 ``PLAN_LOOP`` / ``RULES`` 逐字同：总闸单独成组，钉一个就够；
    # 钉了它的文件（``tests/test_threads_focus.py``）在 import 期显式钉死
    # ``YINYUE_FEATURE_THREADS=off``（"env 未设"≠"关"，本文件的第①条纪律）。
    "YINYUE_FEATURE_THREADS",
    # N5 参数收集状态机（``core/tasks/slot_fill.py``，2026-10-06）：唯一读点是
    # ``slot_fill_mode``（三态 off|shadow|on，**默认 off**）。**不与他开关共布尔判定**
    # ——它是「缺参数→问→确认→执行」这一层要不要接管，不与 routing / 出站 / 群状态 /
    # 装配那些开关同一次判定（本文件 §② 的口径）。落库那一侧另读 ``WORK_ITEM_LEDGER``
    # （那把门在 ``core/tasks/store.py``，是**调用次序**不是同一个表达式），所以这里
    # 单独成组；钉了它的文件（``tests/test_slot_fill.py``）在 import 期显式钉死它自己，
    # 需要落库的用例另外把 LEDGER × RUNS 两个一起钉死。
    "YINYUE_FEATURE_SLOT_FILL",
    # A1-d 执行回执接线（`core/tasks/slot_fill.py`，2026-10-06 第二期）：唯一读点是
    # ``exec_receipt_mode``（三态 off|shadow|on，**默认 off**）。**不与他开关共布尔判定**
    # ——它是「答应了就必须有回执」这条计划层不变式要不要接管出口，不与 routing / 出站 /
    # 群状态 / 装配那些开关同一次判定（本文件 §② 的口径）。前置（``SLOT_FILL`` 非 off）
    # 是**调用次序**、不是同一个 ``or``/``and`` 表达式里的兄弟，所以单独成组；
    # 钉了它的文件（``tests/test_exec_receipt.py``）在 import 期显式钉死它自己，
    # 落库用例另外把 ``LEDGER × RUNS`` 两个一起钉死（同 ``SLOT_FILL`` 那一行）。
    "YINYUE_FEATURE_EXEC_RECEIPT",
    # A1-d 命令链接线（``core/terminal/cmd_trace.py``，2026-10-06 第二期）：唯一读点是
    # ``cmd_trace_mode``（三态 off|shadow|on，**默认 off**）。**不与他开关共布尔判定**
    # ——它是「真实执行落点要不要往命令链上记一笔执行」这一条，不与 routing / 出站 /
    # 群状态 / 装配那些开关同一次判定（本文件 §② 的口径）。**落库那一侧**复用既有的
    # ``CommandService.record_authorization`` / ``record_execution``（前提是命令投影
    # 已在采集侧建行，那是**调用次序**、不是同一个 ``or``/``and`` 表达式里的兄弟），
    # 所以单独成组；钉了它的文件（``tests/test_cmd_trace.py``）在 import 期显式钉死
    # ``YINYUE_FEATURE_CMD_TRACE=off``（"env 未设"≠"关"，本文件第①条纪律）。
    "YINYUE_FEATURE_CMD_TRACE",
    # M2-4 claim 台账（事实权威序 + 更正链，`core/context/message_facts.py`，
    # 2026-10-06）：唯一读点是 ``message_facts.claim_ledger_mode``（三态
    # off|shadow|on，**默认 off**）。**不与他开关共布尔判定**——它是「这条事实该信
    # 谁」这一层要不要接管出口，不与 routing / 出站 / 群状态 / 装配那些开关同一次
    # 判定（本文件 §② 的口径）。闸门侧**不读它**：台账结论经
    # ``guards.claim_ledger_inputs`` 折进 ``GateConfig.unsupported_claims``
    # （**调用次序**，不是同一个 ``or``/``and`` 表达式），所以单独成组；
    # 钉了它的文件（``tests/test_claim_ledger.py``）在 import 期显式钉死
    # ``YINYUE_FEATURE_CLAIM_LEDGER=off``（"env 未设"≠"关"，本文件第①条纪律），
    # 并按第③条纪律把 OUTPUT_GATE 兄弟组一并钉死。
    "YINYUE_FEATURE_CLAIM_LEDGER",
    # 群聊v2 读场总闸（`core/context/scene_read.py::scene_mode`，三态 off|shadow|on，
    # 默认 off）：唯一档位读点，position / act_view / N1 问句门控都经它的布尔壳。
    # **不与他开关共布尔判定**——它自成一条链，不与任何开关共 ``or``/``and``
    # 表达式，单独成组。
    # 钉了它的文件（tests/test_group_scene_shadow.py 等）显式钉死它自己。
    "YINYUE_FEATURE_GROUP_SCENE",
    # 意图调度 tick（`core/context/intention_tick.py::tick_mode`，三态，默认 off）：
    # 「到点执行要不要接管 reminders 投递」的总闸，与 GROUP_SCENE 是独立的两条链，
    # 不与他开关共布尔判定，单独成组。
    "YINYUE_FEATURE_INTENTION_TICK",
        "YINYUE_FEATURE_NOISE_GATE",
    "YINYUE_FEATURE_VOICE_INJECT",
    "YINYUE_FEATURE_DRY_RUN_SEND",
    # 批D Step 35 情绪分层（core/context/emotion.py::layers_mode，三态，默认 off）：
    "YINYUE_FEATURE_EMOTION_LAYERS",
    "YINYUE_FEATURE_SELF_MODEL",
    "YINYUE_FEATURE_SALIENCE",
    "YINYUE_FEATURE_CONSOLIDATION",
    "YINYUE_FEATURE_SELF_CONTINUITY",
    "YINYUE_FEATURE_LLM_DEGRADATION",
    "YINYUE_FEATURE_HUMAN_TONE",
    # 准入规则窗口（`core/context/admission.py::mode`，三态，默认 off）：N1 退役后
    # 它不再与 REPLY_INTENT 合并成一次调用（那个开关已删），只剩自己的规则窗口闸；
    # **没有兄弟**，单独成组。钉了它的文件（tests/test_judges_retired.py 等）显式钉死它自己。
    "YINYUE_ADMISSION_MODE",
})

#: 读开关的代码范围（排除 tests/ 与 scripts/：它们不是产品判定点）。
SOURCE_DIRS = ("core", "agents", "scheduler", "db", "llm", "web")

#: 非开关的字符串常量（``core/features.py`` 里与开关同住，但不是开关）。
_NOT_FLAGS = frozenset({"MODE_OFF", "MODE_ON", "MODE_SHADOW", "_PREFIX"})

#: 用字面量读的三态开关（``features.mode("admission_mode", env_var=...)``）。
_LITERAL_SWITCHES = {
    "admission_mode": "YINYUE_ADMISSION_MODE",
}


# ---------------------------------------------------------------------------
# ① 机制：测试进程不许继承 .env 的开关
# ---------------------------------------------------------------------------
_FIXTURE_ENV = """\
# 守卫用的假 .env（**不是**生产 .env）：开关类 + 资料类各几条
PROVIDER=deepseek
YINYUE_FEATURE_PAT_GRACE=on
YINYUE_FEATURE_SEMANTIC_UNIT=on
YINYUE_TURNS_MODE=on
YINYUE_ANSWER_LINKS=on
YINYUE_TOOLS_DISABLED=create_reminder,recall_memories
YINYUE_CHAT_WINDOW_CHATS=10000000002@chatroom
YINYUE_MEMORY_GATE_MIN_SCORE=0.9
"""

_FIXTURE_SWITCHES = (
    "YINYUE_FEATURE_PAT_GRACE",
    "YINYUE_FEATURE_SEMANTIC_UNIT",
    "YINYUE_TURNS_MODE",
    "YINYUE_ANSWER_LINKS",
)
_FIXTURE_DATA = (
    "YINYUE_TOOLS_DISABLED",
    "YINYUE_CHAT_WINDOW_CHATS",
    "YINYUE_MEMORY_GATE_MIN_SCORE",
)

_PROBE_SRC = '''\
import json
import os
import sys

sys.path.insert(0, {root!r})
from core.env import load_yinyue_env_defaults

load_yinyue_env_defaults({env_path!r})
print("PROBE " + json.dumps({{k: os.environ.get(k) for k in {keys!r}}}))
'''


def _child_env(marker: str | None) -> dict:
    """子进程环境：**不带**任何开关类键（模拟"干净的测试进程"）。"""
    env = {k: v for k, v in os.environ.items() if not is_switch_key(k)}
    env.pop("PYTEST_CURRENT_TEST", None)          # 别把 pytest 身份漏给子进程
    env.pop(TEST_NO_ENV_FLAGS, None)
    if marker is not None:
        env[TEST_NO_ENV_FLAGS] = marker
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_probe(script: Path, marker: str | None) -> dict:
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT),
        env=_child_env(marker),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, f"探针子进程失败：\n{proc.stdout}"
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("PROBE ")]
    assert lines, f"探针没有输出：\n{proc.stdout}"
    return json.loads(lines[-1][len("PROBE "):])


def _write_probe(directory: Path, name: str, env_path: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(
        _PROBE_SRC.format(
            root=str(ROOT),
            env_path=str(env_path),
            keys=list(_FIXTURE_SWITCHES) + list(_FIXTURE_DATA),
        ),
        encoding="utf-8",
    )
    return script


def test_switch_classification() -> None:
    """开关类判据本身：前缀 + 不带前缀的那几条；资料类**不算**开关。"""
    for key in ("YINYUE_FEATURE_PAT_GRACE", "YINYUE_FEATURE_ANY_NEW_FLAG"):
        assert is_switch_key(key), key
    for key in SWITCH_KEYS:
        assert is_switch_key(key), key
    for key in ("YINYUE_CHAT_WINDOW_CHATS", "YINYUE_MEMORY_GATE_MIN_SCORE",
                "YINYUE_TOOLS_DISABLED", "YINYUE_PERSON_NOTES_CHATS", "DATA_DIR",
                "META_DB_PATH", "DEEPSEEK_API_KEY"):
        assert not is_switch_key(key), f"{key} 是资料类，不该被跳过"
    expected = {
        "YINYUE_ADMISSION_MODE", "YINYUE_TURNS_MODE",
        "YINYUE_TURNS_TIMESTAMP", "YINYUE_ANSWER_LINKS",
    }
    assert expected <= set(SWITCH_KEYS), sorted(expected - set(SWITCH_KEYS))


def test_test_process_skips_env_switches() -> None:
    """标记开着（= 测试进程）：开关类不注入，资料类照旧注入。"""
    tmp = make_db_dir("flagiso_")
    try:
        env_path = tmp / "fixture.env"
        env_path.write_text(_FIXTURE_ENV, encoding="utf-8")
        script = _write_probe(tmp, "probe_marked.py", env_path)
        got = _run_probe(script, marker="1")
        leaked = {k: got.get(k) for k in _FIXTURE_SWITCHES if got.get(k) is not None}
        assert not leaked, f"测试进程继承了 .env 的开关：{leaked}"
        missing = [k for k in _FIXTURE_DATA if got.get(k) is None]
        assert not missing, f"资料类键被误跳过（测试会读不到配置）：{missing}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_control_mode_still_injects_switches() -> None:
    """对照位（``YINYUE_TEST_NO_ENV_FLAGS=0``）：照旧全注入——证明挡的是标记，不是删了加载器。"""
    tmp = make_db_dir("flagiso_")
    try:
        env_path = tmp / "fixture.env"
        env_path.write_text(_FIXTURE_ENV, encoding="utf-8")
        script = _write_probe(tmp, "probe_control.py", env_path)
        got = _run_probe(script, marker="0")
        missing = [k for k in _FIXTURE_SWITCHES if got.get(k) is None]
        assert not missing, f"对照位没注入开关（机制被整体关掉了？）：{missing}"
        assert got["YINYUE_FEATURE_PAT_GRACE"] == "on", got
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_direct_test_run_is_detected() -> None:
    """``python tests/test_x.py`` 直跑（**没有**标记）也必须隔离。"""
    tmp = make_db_dir("flagiso_")
    try:
        env_path = tmp / "fixture.env"
        env_path.write_text(_FIXTURE_ENV, encoding="utf-8")
        script = _write_probe(tmp / "tests", "test_probe_direct.py", env_path)
        got = _run_probe(script, marker=None)
        leaked = {k: got.get(k) for k in _FIXTURE_SWITCHES if got.get(k) is not None}
        assert not leaked, f"直跑 tests/test_*.py 时继承了 .env 的开关：{leaked}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_direct_non_test_run_keeps_env() -> None:
    """同一个探针放在非 ``tests/test_*`` 位置：照旧继承（生产/工具脚本不受影响）。"""
    tmp = make_db_dir("flagiso_")
    try:
        env_path = tmp / "fixture.env"
        env_path.write_text(_FIXTURE_ENV, encoding="utf-8")
        script = _write_probe(tmp / "scripts", "probe_tool.py", env_path)
        got = _run_probe(script, marker=None)
        assert got.get("YINYUE_FEATURE_PAT_GRACE") == "on", (
            f"普通脚本被误判成测试进程，.env 开关没生效：{got}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_current_process_is_not_polluted_by_env() -> None:
    """本进程（正统测试进程）：**再加载一次** ``.env`` 也不许注入任何开关类键。

    这条是变异自检的主判据——机制被改回"照抄 .env"时，缺失的开关会被写进来。
    """
    assert skip_switch_injection(), "本进程没被认成测试进程（守卫本身失去意义）"
    tmp = make_db_dir("flagiso_")
    try:
        env_path = tmp / "fixture.env"
        env_path.write_text(_FIXTURE_ENV, encoding="utf-8")
        keys = list(_FIXTURE_SWITCHES) + list(_FIXTURE_DATA)
        before = {k: os.environ.get(k) for k in keys}
        load_yinyue_env_defaults(env_path)
        injected = [
            k for k in _FIXTURE_SWITCHES
            if before[k] is None and os.environ.get(k) is not None
        ]
        assert not injected, f"加载 .env 时把开关注入了测试进程：{injected}"
        # 生产 .env 的开关同样不许被"补"进来（缺失的那些）。
        prod = ROOT / ".env"
        if prod.exists():
            prod_switches = _env_switch_keys(prod)
            missing = [k for k in prod_switches if os.environ.get(k) is None]
            load_yinyue_env_defaults(prod)
            injected = [k for k in missing if os.environ.get(k) is not None]
            assert not injected, f"生产 .env 的开关进了测试进程：{injected}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_runner_marks_children() -> None:
    """正统 runner 必须给子进程带上标记（core/env.py 是名字的唯一来源）。"""
    runner = _load_module(ROOT / "scripts" / "run_all_tests.py", "run_all_tests_guard")
    assert runner.TEST_NO_ENV_FLAGS_ENV == TEST_NO_ENV_FLAGS, (
        "runner 与 core/env.py 的标记名不一致"
    )
    isolation = {
        "env": {k: f"<{k}>" for k in (
            "META_DB_PATH", "DATA_DIR", "PLATFORMS_DIR", "LOG_DIR",
            "YINYUE_DECISION_TRACE_PATH", "YINYUE_INTENT_TRACE_PATH",
            # 2026-11-22：流水账本也开始随隔离根改道
            "YINYUE_LEDGER_DIR",
        )},
        "tmp": "<tmp>",
    }
    saved = os.environ.pop(TEST_NO_ENV_FLAGS, None)
    try:
        env = runner.child_env(isolation, ROOT / "output")
        assert env.get(TEST_NO_ENV_FLAGS) == "1", (
            f"runner 没给子进程带开关隔离标记：{env.get(TEST_NO_ENV_FLAGS)!r}"
        )
        os.environ[TEST_NO_ENV_FLAGS] = "0"
        env = runner.child_env(isolation, ROOT / "output")
        assert env.get(TEST_NO_ENV_FLAGS) == "0", "操作者显式给的对照位被 runner 覆盖"
    finally:
        os.environ.pop(TEST_NO_ENV_FLAGS, None)
        if saved is not None:
            os.environ[TEST_NO_ENV_FLAGS] = saved


# ---------------------------------------------------------------------------
# ② 分类：被读过的开关都要登记，同一布尔表达式的开关必须同组
# ---------------------------------------------------------------------------
def _load_module(path: Path, name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _env_switch_keys(env_path: Path) -> list[str]:
    """``.env`` 里的开关类键（只读，绝不改）。"""
    keys: list[str] = []
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if key.startswith("YINYUE_") and is_switch_key(key) and key not in keys:
            keys.append(key)
    return keys


def _feature_constants() -> dict[str, str]:
    """``core/features.py`` 里的 ``NAME = "value"`` 常量表。"""
    tree = ast.parse((ROOT / "core" / "features.py").read_text(encoding="utf-8-sig"))
    constants: dict[str, str] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            constants[node.targets[0].id] = node.value.value
    return constants


def _aliases_in(tree: ast.AST, constants: dict[str, str]) -> dict[str, str]:
    """模块级 ``MODE_X = features.FLAG`` 别名 → 环境变量名。"""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id in ("features", "_features")
                and node.value.attr in constants
                and node.value.attr not in _NOT_FLAGS):
            aliases[node.targets[0].id] = _const_env(constants[node.value.attr])
    return aliases


def _const_env(value: str) -> str:
    if value in _LITERAL_SWITCHES:
        return _LITERAL_SWITCHES[value]
    if value.startswith("YINYUE_"):
        return value
    return f"YINYUE_FEATURE_{value.upper()}"


#: 读环境开关的 helper 名：``from core.infra.envflags import flag`` 之后
#: ``flag("YINYUE_...")`` 是一等读法（person_notes / chat_window 就这么读）。
_ENV_READERS = frozenset({
    "flag", "_flag", "text", "_text", "env_text", "_env_text",
    "env_bool", "_env_bool", "bool_env", "getenv",
})


def _raw_env_at(node: ast.AST) -> str | None:
    """裸读开关：``os.environ.get("YINYUE_...")`` / ``os.environ["YINYUE_..."]``。

    不带前缀的三态开关（``YINYUE_TURNS_MODE`` 等）就是**这么**读的，不走
    ``features.enabled``——分类时不能漏掉它们。``flag("YINYUE_...")`` 这类
    走 :mod:`core.infra.envflags` 的 helper 读法同样算读（2026-11-17 补）。
    """
    key: str | None = None
    if isinstance(node, ast.Call) and node.args:
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name in _ENV_READERS or name == "get":
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                key = arg.value
    elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            key = node.slice.value
    if key and is_switch_key(key):
        return key
    return None

def _flag_at(node: ast.AST, constants: dict[str, str], aliases: dict[str, str]) -> str | None:
    """这一个节点是不是"读开关"（``features.X`` / ``features.enabled("x")`` / 裸读 / 别名）。"""
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in ("features", "_features")
            and node.attr in constants and node.attr not in _NOT_FLAGS):
        return _const_env(constants[node.attr])
    if isinstance(node, ast.Call):
        fn = node.func
        if (isinstance(fn, ast.Attribute) and fn.attr in ("enabled", "mode")
                and isinstance(fn.value, ast.Name)
                and fn.value.id in ("features", "_features") and node.args):
            arg = node.args[0]
            if isinstance(arg, ast.Name) and arg.id in aliases:
                return aliases[arg.id]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return _const_env(arg.value)
    if isinstance(node, ast.Name) and node.id in aliases:
        return aliases[node.id]
    return _raw_env_at(node)


def _direct_flags(node: ast.AST, constants: dict[str, str], aliases: dict[str, str]) -> set[str]:
    """节点子树里**直接**读到的开关。"""
    found: set[str] = set()
    for sub in ast.walk(node):
        flag = _flag_at(sub, constants, aliases)
        if flag:
            found.add(flag)
    return found


def _source_files() -> list[Path]:
    files: list[Path] = []
    for src in SOURCE_DIRS:
        base = ROOT / src
        if base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
    return files


def declared_in_features() -> dict[str, int]:
    """``core/features.py`` 里**声明过且真被引用**的开关 -> 引用行。

    为什么需要它：``core/features.py`` 是开关的定义处，也常常是唯一读点
    （``enabled(X, ...)`` 就在同文件里）。只看 ``SOURCE_DIRS`` 会把这种开关
    当成死开关，所以必须补上这一半。

    只收"常量名在别处被引用过"的：定义处自己那行不算——
    ``ENRICHMENT_ARTIFACTS`` / ``PERSON_PROFILE`` 这种真的没人读的开关
    必须继续被 ``stale`` 抓出来。
    """
    tree = ast.parse((ROOT / "core" / "features.py").read_text(encoding="utf-8-sig"))

    def line_of(node: ast.AST) -> int:
        return int(getattr(node, "lineno", 0) or 0)

    # 定义：常量名 -> (环境变量, 定义行)
    defined: dict[str, tuple[str, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            names = [node.target.id] if isinstance(node.target, ast.Name) else []
            value = node.value
        else:
            continue
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for name in names:
            if name in _NOT_FLAGS:
                continue
            env = _const_env(value.value)
            # ``YINYUE_FEATURE_`` 这种只是前缀常量，不是开关名
            if is_switch_key(env) and env != "YINYUE_FEATURE_":
                defined[name] = (env, line_of(node))

    # 引用：常量名在别处被读
    refs: dict[str, int] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in defined
        ):
            refs.setdefault(node.id, line_of(node))

    out: dict[str, int] = {}
    for name, (env, _decl) in defined.items():
        if name in refs:
            out.setdefault(env, refs[name])
    return out


def read_switches() -> dict[str, str]:
    """全仓库被**读过**的开关 → 第一处读点（``file:line``）。"""
    constants = _feature_constants()
    reads: dict[str, str] = {}
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        aliases = _aliases_in(tree, constants)
        rel = path.relative_to(ROOT)
        for node in ast.walk(tree):
            lineno = getattr(node, "lineno", None)
            if lineno is None:
                continue
            flag = _flag_at(node, constants, aliases)
            if flag:
                reads.setdefault(flag, f"{rel}:{lineno}")
    # core/features.py 是定义处，常常也是唯一读点：声明即算被读过，
    # 否则同文件里 ``enabled(X, ...)`` 的开关会被误判成死开关。
    rel_features = (ROOT / "core" / "features.py").relative_to(ROOT)
    for flag, lineno in declared_in_features().items():
        reads.setdefault(flag, f"{rel_features}:{lineno}")
    return reads


def boolean_sibling_pairs() -> dict[tuple[str, str], str]:
    """同一布尔表达式里一起读的开关对 → 证据（``file:function``）。

    只跟 **1 层同模块 helper**（``f() or g()`` 里的 ``f``/``g`` 各自读一个开关），
    这正是 ``should_defer_pure_pat`` 的形状；不跟更深的调用链——那会把
    "一个大函数里先后判了十个开关"也算成兄弟，兄弟表就失去意义。
    """
    constants = _feature_constants()
    pairs: dict[tuple[str, str], str] = {}
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        aliases = _aliases_in(tree, constants)
        helpers: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                helpers[node.name] = _direct_flags(node, constants, aliases)
        rel = path.relative_to(ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.BoolOp):
                    continue
                flags: set[str] = set()
                for value in sub.values:
                    flags |= _direct_flags(value, constants, aliases)
                    for call in ast.walk(value):
                        if isinstance(call, ast.Call):
                            fn = call.func
                            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                            flags |= helpers.get(name, set())
                flags = {f for f in flags if f.startswith("YINYUE_")}
                for left in sorted(flags):
                    for right in sorted(flags):
                        if left < right:
                            pairs[(left, right)] = f"{rel}::{node.name}"
    return pairs


def test_every_read_switch_is_classified() -> None:
    """新开关：只要被读过就必须进兄弟表（组或"无兄弟"清单），否则红。"""
    reads = read_switches()
    assert reads, "没扫到任何开关读点？扫描坏了（守卫必须能真的红）"
    known = set(STANDALONE_SWITCHES)
    for group in SIBLING_GROUPS:
        known |= set(group)
    unknown = {k: v for k, v in reads.items() if k not in known}
    assert not unknown, (
        "这些开关被读了但没登记进本文件的兄弟表（SIBLING_GROUPS / STANDALONE_SWITCHES）：\n  "
        + "\n  ".join(f"{k}  <- {v}" for k, v in sorted(unknown.items()))
    )
    stale = {k for k in known if k not in reads}
    assert not stale, f"兄弟表里登记了但全仓库没人读的开关（死开关/改名了）：{sorted(stale)}"
    # 反向自检：定义处扫描必须真的收得到东西
    # （否则本守卫会静默失明）。
    declared = declared_in_features()
    assert len(declared) >= 50, f"core/features.py 声明扫描坏了：只收到 {len(declared)} 个"
    assert all(is_switch_key(k) for k in declared), sorted(declared)[:5]


def test_boolean_siblings_share_a_group() -> None:
    """同一布尔表达式里一起读的开关必须同组（新开关接在 ``PAT_GRACE or X`` 旁边 → 红）。"""
    reads = read_switches()
    declared = {frozenset(group) for group in SIBLING_GROUPS}
    bad: list[str] = []
    for (left, right), evidence in sorted(boolean_sibling_pairs().items()):
        if left not in reads or right not in reads:
            continue
        if not any({left, right} <= group for group in declared):
            bad.append(f"{left} & {right}  <- {evidence}")
    assert not bad, (
        "这些开关在同一个判定里一起读，却不在同一组兄弟里：\n  " + "\n  ".join(bad)
    )


# ---------------------------------------------------------------------------
# ③ 纪律：钉了兄弟组一个开关的测试文件，必须把整组钉死
# ---------------------------------------------------------------------------
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})


class _Pins:
    def __init__(self) -> None:
        self.on: set[str] = set()
        self.off: set[str] = set()
        self.explicit: set[str] = set()   # 字面量既非真值也非假值（如三态的 shadow）

    @property
    def touched(self) -> set[str]:
        return self.on | self.off | self.explicit


def _pins_of(path: Path) -> _Pins:
    """测试文件显式钉死的开关（``os.environ[...] = v`` / ``pop`` / 上下文管理器 kwargs）。

    只看"写"，不猜语义：值是真值 → on，假值 → off，``pop`` → off（= 缺省行为）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    pins = _Pins()
    consts: dict[str, str] = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and node.value.value.startswith("YINYUE_")):
            consts.setdefault(node.targets[0].id, node.value.value)

    def key_of(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value if is_switch_key(node.value) else None
        if isinstance(node, ast.Name):
            value = consts.get(node.id)
            return value if value and is_switch_key(value) else None
        return None

    def literal(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def add(key: str | None, value: str | None) -> None:
        if key is None:
            return
        if value is None:
            pins.off.add(key)
        elif value.strip().lower() in _TRUTHY:
            pins.on.add(key)
        elif value.strip().lower() in _FALSEY:
            pins.off.add(key)
        else:
            pins.explicit.add(key)     # ``= "shadow"`` 也是显式表态

    def add_kwargs(call: ast.Call) -> None:
        for arg in call.args:
            if isinstance(arg, ast.Dict):
                add_dict(arg)
        for kw in call.keywords:
            if kw.arg is None and isinstance(kw.value, ast.Dict):
                add_dict(kw.value)          # ``_env(**{KEY: "on"})``
            else:
                add(key_of(ast.Constant(kw.arg)) if kw.arg else None, literal(kw.value))

    def add_dict(node: ast.Dict) -> None:
        for k, v in zip(node.keys, node.values):
            add(key_of(k) if k is not None else None, literal(v))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Subscript) and isinstance(target.ctx, ast.Store):
                add(key_of(target.slice), literal(node.value))
        # 模块级 / 函数内的钉表（``_BASE = {...}``、``_LEAKY = (...)``）也算显式钉死：
        # 只要表里是字面量，这份"本文件需要什么开关"就是写在文件里的。
        if isinstance(node, ast.Dict):
            add_dict(node)
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else ""
            )
            if name == "pop" and node.args:
                add(key_of(node.args[0]), None)
            elif name == "setdefault" and len(node.args) >= 2:
                add(key_of(node.args[0]), literal(node.args[1]))
            elif name in ("update", "_env", "_Env", "_with_env", "_base_env"):
                add_kwargs(node)
    return pins


def test_flag_pins_cover_sibling_group() -> None:
    """一个文件钉了兄弟组里的开关 → 整组都得显式钉死（"flag 关=旧行为"不许被改写）。"""
    violations: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        pins = _pins_of(path)
        for group in SIBLING_GROUPS:
            touched = set(group) & pins.touched
            if not touched:
                continue
            missing = set(group) - pins.touched
            if missing:
                violations.append(
                    f"{path.name}: 钉了 {sorted(touched)}，但没钉 {sorted(missing)}"
                )
    assert not violations, (
        "这些测试文件只钉了兄弟组的一部分（生产 .env / 同进程其它文件的开关会改写断言）：\n  "
        + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------------
# 变异自检（人工做过一次，记录在此；机制被改回去时①会红）
# ---------------------------------------------------------------------------
# 2026-10-02 实测（`.venv`，脚本 `.test_tmp/mutation_check.py`，跑完自动还原）：
#   1) `core/env.py::skip_switch_injection` 改成 `return False`（= 照抄 .env）
#      → 红，第一条报错就是子进程实测（fail-fast）：
#        ``直跑 tests/test_*.py 时继承了 .env 的开关：{'YINYUE_FEATURE_PAT_GRACE': 'on', …}``；
#   2) `scripts/run_all_tests.py::child_env` 不传标记
#      → 红，``runner 没给子进程带开关隔离标记：None``；
#   3) 还原 → 全绿（baseline=0 mutA=1 mutB=1 restored=0）。
def main() -> int:
    tests = [
        test_switch_classification,
        test_test_process_skips_env_switches,
        test_control_mode_still_injects_switches,
        test_direct_test_run_is_detected,
        test_direct_non_test_run_keeps_env,
        test_current_process_is_not_polluted_by_env,
        test_runner_marks_children,
        test_every_read_switch_is_classified,
        test_boolean_siblings_share_a_group,
        test_flag_pins_cover_sibling_group,
    ]
    for test in tests:
        test()
        print(f"OK {test.__name__}")
    print(f"ALL PASSED ({len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
