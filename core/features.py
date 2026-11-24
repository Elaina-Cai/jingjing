"""Feature flags for the incremental refactor.

The refactor is deliberately flag-driven: new infrastructure can be merged,
tested and shipped while the legacy path keeps serving production traffic.
Flags are read from the environment every time so a running process can be
toggled without a restart (useful for local diagnosis).

Convention::

    YINYUE_FEATURE_DECISION_TRACE=1

Truthy values are ``1``, ``true``, ``yes`` and ``on`` (case-insensitive).
Everything else is false.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from safelog import warn
from core.infra.envflags import text

_PREFIX = "YINYUE_FEATURE_"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Known flags.  Keep names stable: they are part of the migration contract.
DECISION_TRACE = "decision_trace"
CONVERSATION_SHADOW = "conversation_shadow"
CONVERSATION_ACTIVE = "conversation_active"
ENRICHMENT_ARTIFACTS = "enrichment_artifacts"
EVIDENCE_INDEX = "evidence_index"
OUTBOUND_RECONCILE = "outbound_reconcile"
# Outbox maintenance (REC-1): a periodic drain so a pending row still gets
# settled while no new message arrives (the sync-path drain never runs then).
# On by default -- it only finishes work the sync path already tried to do.
OUTBOX_MAINTENANCE = "outbox_maintenance"
# Terminal reconciler (REC-2): the read-only periodic scan of account.messages
# against terminal_events.  Split from the write path on purpose: the default
# run only reports gaps (and alerts), backfilling needs the apply flag below.
TERMINAL_RECONCILE = "terminal_reconcile"
# Reconciler write path: off by default so a periodic job can never backfill
# events behind the owner's back; turn on only after the dry-run numbers look
# right (YINYUE_FEATURE_TERMINAL_RECONCILE_APPLY=1).
TERMINAL_RECONCILE_APPLY = "terminal_reconcile_apply"
# WorkItem ledger (P1 记录统一): write the work_items row + work_item_created
# event next to every reminder.  Off by default -- reminders stay the only
# execution authority either way; this is a one-way projection (reminders ->
# work_items, never back).  Roll back by setting
# YINYUE_FEATURE_WORK_ITEM_LEDGER=0 and restarting: zero writes, zero overhead.
WORK_ITEM_LEDGER = "work_item_ledger"
# WorkItem runs (P1 投递观测): record work_item_run_started / _finished around
# each reminder delivery.  Off by default and **requires WORK_ITEM_LEDGER on**
# (a run without its ledger row would be an orphan).  Off = the delivery path
# behaves exactly as before.
WORK_ITEM_RUNS = "work_item_runs"
# Person memory is split into two flags so observation extraction can run in
# shadow mode long before profile retrieval is injected into prompts.
PERSON_MEMORY = "person_memory"
PERSON_PROFILE = "person_profile"
PERSON_PROFILE_AGGREGATE = "person_profile_aggregate"
PERSON_PROFILE_TOOLS = "person_profile_tools"
PERSON_LLM = "person_llm"
# Person-memory recall provider (hub kind="person"): the read-side switch of the
# same line.  Recall requires BOTH PERSON_MEMORY and PERSON_RECALL so a disabled
# write line can never serve stale observations (2026-09-29 收口).
PERSON_RECALL = "person_recall"
# Query-side vector recall over memory projections (E7 index, E2 read path).
MEMORY_VECTOR_RECALL = "memory_vector_recall"
# Correction learning: owner corrections become memory writes + invalidations.
CORRECTION_LEARNING = "correction_learning"
# Context assembler: single entry for the system prompt (Phase 1 equivalence).
CONTEXT_ASSEMBLER = "context_assembler"
# Context budget: Phase 2 priority/budget policy over the assembled blocks.
CONTEXT_BUDGET = "context_budget"
# Chat window: prompt reads the real window from terminal_events (recent
# verbatim + graded older) instead of the in-memory 30-message ring buffer.
# Scope it with YINYUE_CHAT_WINDOW_CHATS to trial on one chat first.
CHAT_WINDOW = "chat_window"
# Person notes: the [在场人·备注] block (who is here / how to treat them).
PERSON_NOTES = "person_notes"
# Outbound rhythm: 把一条回复切成最多 3 个气泡（连发），每条仍走完整网关；
# off/shadow/on，作用域 YINYUE_RHYTHM_CHATS。
OUTBOUND_RHYTHM = "outbound_rhythm"
# Retrieval memory block injection (P3): the per-turn "[事件记忆]" block is
# meant to be retired in favour of the chat window; set 0 to stop injecting
# it everywhere (the recall *tool* keeps working).
MEMORY_BLOCK_INJECT = "memory_block_inject"
# ⑤ 按需记忆注入（needs_memory）：⑤ 判缺口 → ⑤ hub 召回
# → 本 flag 掉的是“把命中渲成 [按需记忆] 块进 system prompt”这一步。
# off 逐字节不变；shadow 只落审计；on 才注入。**与 MEMORY_BLOCK_INJECT
# 不是一回事**：那个是每轮 [事件记忆]（窗口生效时被关掉），
# 这个恰恰是窗口没有信息时的补召回。
# In-session perceive (2026-10-23 Step 2b): 把判断从"另起一次 chat"改成"同一段
# transcript 的下一轮"（agents/perceive.py）。两态开关、**默认关**：先落通路，
# 真机对账（与 N1 的 route 一致率）之后再考虑开。
# YINYUE_FEATURE_PERCEIVE_TOOL=1 开。
PERCEIVE_TOOL = "perceive_tool"
#: 灰度期只让私聊走 perceive（群聊判断复杂，先不放）。两态，默认关。
# YINYUE_FEATURE_PERCEIVE_TOOL_PRIVATE=1 开。
PERCEIVE_TOOL_PRIVATE = "perceive_tool_private"

# ⑨→③ 选：召回链（FTS5-trigram + 本地向量 + 关键词兜底）→ ±6 行窗口 → 机械重排。
# off 逐字节不变且不建索引、不加载模型；shadow 只写读数；on 才进 [按需记忆] 块。
RECALL_CHAIN = "recall_chain"
# ⑨→③ 选②：结构化召回通道（附件名 / ≥6 位数字串 / 日期串 / @尾巴 × 近因先验）。
# 挂在召回链内：off 时链的行为与 +RECALL_CHAIN 逐字一致；shadow 只写读数。
RECALL_STRUCT = "recall_struct"
# 寻址边（task-43）：「谁对谁说」的事实层。三态：off 不采集不渲染（逐字旧行为）、
# shadow 只采集（写 utterance_edges + 一行 utterance_edges_recorded）、on 采集并
# 在窗口渲染 `↳回 X`。唯一读点 `core/terminal/utterance_edges.py::edges_mode`。
# 生产现状：微信 mentions 恒空、quote_reply_user 不可用，所以这张表的 @ 通道只能靠
# 正文解析（evidence='at_body'）；没有 @ 也没有引用时一律 unknown/none。
UTTERANCE_EDGES = "utterance_edges"

# ② 媒体基质：采集侧把每个媒体件写 ``message_media``（附件名/正文摘要/状态）。
# off 逐字旧行为（一行不写、零 IO）；shadow 等同 on（先攒数据给影子期看）。
MEDIA_STORE = "media_store"
# ③→⑨ 选②：媒体召回通道（附件名 + 正文摘要，题型闸门 + 通道内近因）。
# 挂在召回链内：off 时链的行为与 +RECALL_STRUCT 逐字一致；shadow 只写读数。
MEDIA_RECALL = "media_recall"
# ③→⑨ 选③：情节级（滑窗）向量——连续 30 条消息拼段做嵌入（50% 重叠），
# 补「一句话说不清、要说三句才懂」的指代；与行级向量同源，缓存键加 ep 前缀。
# 挂在召回链内：off 时链行为与 +RECALL_CHAIN 逐字一致；shadow 只写读数。
RECALL_EPISODE = "recall_episode"
# task-33：类型无关原语——词法层 char 3-gram / jieba 两路 BM25 真打分（替掉手写重叠阈值），
# 语义层轮/段粒度向量；off 时链行为与 +RECALL_CHAIN 逐字一致且零 IO。
RECALL_PRIMITIVES = "recall_primitives"
# 外部检索：DeepSeek 自研搜索/抓取（/v1/tools/search 与 /v1/tools/fetch）。
# off 逐字旧行为（Tavily → Bing → DDG 顺序不变）；shadow 只跑一次对照写读数、不影响返回；
# on 才把 DeepSeek 放第一顺位，失败仍回落到旧链。
SEARCH_DEEPSEEK = "search_deepseek"
# Affect is split the same way person memory is: the state can be maintained
# and observed long before it is allowed to change a single word she says.
AFFECT = "affect"
AFFECT_PROMPT = "affect_prompt"
# Media registry: every media message gets a tracked row so a picture whose
# file was not on disk yet at ingest time can still be resolved later.  On by
# default -- it repairs a plain defect (see core.conversation.media_registry)
# and only ever fills a blank media_path, never overwrites one.
MEDIA_REGISTRY = "media_registry"
# Memory gate (批1): auto-recall must clear a relevance bar before entering
# the prompt; an empty recall on a question produces a negative-knowledge
# note.  Off by default = legacy behaviour, roll back by flipping it off.
MEMORY_GATE = "memory_gate"
# Uncertain voice (批1): the [拿不准时] block + 「没对上号」 wording for
# names/identities that cannot be confirmed.  Off by default.
UNCERTAIN_VOICE = "uncertain_voice"
# Group coalesce (批2a): ambient room messages inside a short window are
# merged into one turn instead of one LLM call per message.  Off by default.
GROUP_COALESCE = "group_coalesce"
# Pat grace (P1-WP4 R-A9, 2026-10-01 主人口径)：**拍只是"对方呼唤鲸鲸的形式"之一**
# （与 @她 / 含她名字 / 引用她同类，不是内容）。纯拍先挂起进宽限窗口（同人窗口内的
# 后续内容并入**同一个**回合；窗口结束仍无内容 → 允许反问，态度由意图层定），
# 被禁止的只有"抢答"。**三态读法**（core.features.mode，先 shadow 后 on）：
#   off    ← 默认：完全不做事，行为与接线前**逐字节一致**（拍仍走原来的直回）
#   shadow / on → 挂起 + 合并 + 复合 trigger_reason；**shadow 与 on 同义（本门无影子档）**，
#                  要停必须 off（留 shadow 只为兼容三态读法，没有影子读数）
# 窗口：YINYUE_PAT_GRACE_SECONDS（默认 6）/ YINYUE_PAT_GRACE_MAX_WAIT（默认 12）。
PAT_GRACE = "pat_grace"
# Semantic unit (R-A12 P1a, 2026-10-02)：**一个装配器**取代 coalesce 窗口 + 拍宽限
# 单元两套计时器（规格 `docs/语义单元规格.md` §6.1 第 1/2/2b 行、§7 P1a）。
# 同一个人的信号（文字 / 拍 / @ / 引用）装进**一个**单元，**只判一次、只回一次**；
# 判定只看集合（有哪些呼唤形式 / 动作），顺序只进叙述。
# **三态读法**（`core.features.mode`，与 PAT_GRACE 同一纪律）：
#   off          ← 默认：装配器一行都不走，coalesce + pat 宽限两条老路原样
#                  （off = 与接线前**逐字节一致**，这是回滚位）
#   shadow / on  → 走装配器；**shadow 与 on 同义（本门无影子档）**，要停必须 off
#                  （留 shadow 只为兼容三态读法，没有影子读数）
# 窗口：`YINYUE_UNIT_SILENCE_SECONDS`（默认 6.0，0.05~15）——**唯一**窗口常数，
# `pat_grace_seconds()` / `_is_replied_to_sender` / `CONVERSATION_RHYTHM_SECONDS`
# 三者同值同源（G6 的 4/6/10/12 打架就此收口）。
SEMANTIC_UNIT = "semantic_unit"
# Unit media (R-A12 **P1b**, 2026-10-06)：**媒体信号（图 / 表情包 / 语音 / 文件）并入
# 同一个语义单元**（规格 `docs/语义单元规格.md` §7 P1b、§5 组合矩阵第 4/5/6/10/11 行）。
# 媒体是**内容**（规格 §3.1：内容 = 文字 / 图 / 表情 / 语音 / 文件 / 链接），与文字、拍、
# `@`、引用**同在一个单元**里，受**同一套封口四条**与**同一个窗口** ``unit_silence_seconds()``
# 约束（不许新造第二套窗口）。
# **三态读法**（`core.features.mode`，与 PAT_GRACE / SEMANTIC_UNIT 同一纪律）：
#   off          ← 默认：媒体仍按现有通道走（coalesce / 直回），`pat→media` 的
#                  ``kind="pat"`` pending 登记照旧 —— 与接线前**逐字节一致**（回滚位）
#   shadow / on  → 媒体作为内容成员进单元；同一开关同时**退役** `pat→media` 登记
#                  （规格 §6.1 第 3 行：那条登记要证的就是"他刚拍过我，图马上来"，
#                  单元路径按 `(chat, actor, 时间邻近)` 收口时天然覆盖它）；
#                  **shadow 与 on 同义（本门无影子档）**，要停必须 off
# **前置**：本开关**只有在 `SEMANTIC_UNIT` 也开着时才生效**（读法见
# ``core.group_state.unit_media_enabled()``）——没有装配器就没有"媒体进单元"这条路径，
# 那时候退役登记就是纯粹的覆盖缺口，所以那种组合下本开关一律按 off 处理。
# 不改媒体解析/完整性判据（唯一 owner 仍是 ``core.conversation.media``）。
UNIT_MEDIA = "unit_media"
# E0（U-01，2026-10-20）：判决行留痕——每回合一行「decision + basis」，写进账本旁注
# ``turn_trace``。三态：off 不写 / shadow 写行（带 mode）但不参与决策与出口 / on 进验收报表。
# 为什么是旁注不是账本正行：判决是她自己的决定，不是外部世界发生的事实；删掉可整表重建。
TURN_TRACE = "turn_trace"
# E1（U-02，2026-10-20）：**输出契约校验**（三必填 + 三条件）。模型某天真的
# 一次给出 ``decision / deliver / basis``（+ 条件项）时，由
# ``core/contracts/verdict.py::validate_verdict`` 机械校验：decision 四值、
# deliver 0~2 且 say <=1、respond/ask 必须有 deliver 与 reading、silent/abstain
# 必须 deliver 空、basis 每项非空。三态：off 不校验 / shadow 只记
# ``verdict_contract_check`` / on 不合法即拒（抛 ``VerdictContractError``）。
VERDICT_CONTRACT = "verdict_contract"
# E3（U-10，2026-10-20）：**「在手的事」台账**（图上第 12 个实体）。三态：
# off 写读全 no-op / shadow 写事件+投影但 ``JobStore.readouts()`` 一律返回 ``[]``
# （不进 prompt，写失败只 log）/ on 写读都开（写失败抛，因为读侧依赖它）。
# 唯一读点 ``core/jobs/store.py::_mode``；不是第二决策器——判照旧是唯一模型点。
JOB_LEDGER = "job_ledger"
# E2（U-11，2026-10-21）：**出口契约**（`docs/decisions/出口.md` §5 六态 / §15）。
# 三态：off 出口连回执都不造（默认，与重构前逐字节同行为）/ shadow 造回执只写日志、
# 不落任何账 / on 造回执并逐条交给 `ledger` 钩子（回执表由那一步接上）。
# 唯一读点 `core/speech/delivery.py::_delivery_mode`。
DELIVERY_CONTRACT = "delivery_contract"

# ``gui_channel``：GUI 驱动层（`core/execution/gui/`）的总闸。三态：off 连端口都不摸 /
# shadow 只读一屏写日志 / on 才把界面正文交给判。
GUI_CHANNEL = "gui_channel"

# `wechat_ui`：微信写侧（core/execution/gui/wechat.py）的总闸。三态：off 连窗口都不找 /
# shadow 只读库、不碰界面 / on 才允许切会话、聚焦输入框、填字（**仍然不按发送**）。
# 为什么要单独一个闸：这一层是唯一会真的驱动用户账号的驱动（SendInput），
# 封号风险与行为节奏绑在一起，必须能一键停。
WECHAT_UI = "wechat_ui"

# `wechat_send`：微信**发送**（按回车 + 回读对账）的闸。三态：off 不碰 / shadow 只判定（连填字都不做，
# 不可逆动作没有影子档）/ on 才允许。与 `wechat_ui` 分开：能填字不等于能发出去。
WECHAT_SEND = "wechat_send"

# `wechat_revoke`：微信**撤回**（右键菜单 → 点「撤回」→ 回读确认）的闸。
# 三态：off 不碰 / shadow 只判定（连右键都不点）/ on 才允许。
# 与 `wechat_send` 分开：能发不等于能撤，撤错人比不撤更糟。
WECHAT_REVOKE = "wechat_revoke"

# ``correction``：说错了之后的补偿（U-26）。三态：off 一行都不做（默认）/
# shadow 只算「该怎么更正」不发 / on 才真发（窗口内先撤回再更正）。
# 与 `wechat_revoke` 分开：撤不回来也必须能更正，两条路各自有闸。
CORRECTION = "correction"

# ``handoff``（U-28）：办不下去时把人要接手的东西写成交接单（原因 / 走到哪 / 还差什么 /
# 你要动哪里），而不是一句「抱歉我没办成」。三态：off 一行都不算（默认）/ shadow 只算文本写日志 /
# on 才落 `handed_off` 事件并把文本交给下游。
HANDOFF = "handoff"

# ``gui_backends``（U-21）：GUI 驱动层的多手注册表。``off`` 只放行已经在跑的两只
# （``cdp`` / ``uia``），``on`` 才允许选中新登记的手（Vision / ADB 之类）。
GUI_BACKENDS = "gui_backends"

# 设备租约（U-30）：动手前先拿「手归谁」的独占凭证；off 档一行都不执行。
DEVICE_LEASE = "device_lease"

# ``legacy_via_exit``：把旧出口（`core/speech/outbound.py`）的最后一段传输换成新出口
# （`core/speech/delivery.deliver`），于是回复第一次拿到回执与落账。三态：off 逐字节
# 不变（默认）/ shadow 只判定不发 / on 才换。**换的是传输，不是判定**：策略、输出闸、
# 社会闸、@ 裁定都还在原位跑完。
LEGACY_VIA_EXIT = "legacy_via_exit"

# ``declared_tools``：声明式工具（`configs/tools/*.yaml`）的总闸。三态：off 一个 YAML 都不读（默认）/
# shadow 只登记不启用（留痕「有这么几个声明」）/ on 才把文件里写了 ``enabled: true`` 的那些启用。
DECLARED_TOOLS = "declared_tools"

# ``mcp_tools``：外部 MCP 服务器当只读工具源的总闸。off 一台都不连 / shadow 只连上列工具但不启用 /
# on 才把「文件里 enabled: true」的那些工具登记并启用（工具仍受 registry.enable 约束）。
MCP_TOOLS = "mcp_tools"
# ``sensory``：感官（阶段 5）的总闸。off 连窗口都不读（默认）/ shadow 只读窗口标题与
# 剪贴板长度并记日志、不截图不落盘 / on 才截图并排成 ``[现在这一屏]`` 进提示词。
# **屏幕不是账本**：这些观察不进 ``terminal_events``，它是世界面的第二投影。
SENSORY = "sensory"

# ``tool_loop``：回合内工具调用闭环的总闸。off 逐字不变（默认）/ shadow 只解析
# 载荷里的 ``tools`` 并记日志，**一个工具都不调** / on 才真调 ``registry.check_call``
# 并把结果放进**下一轮**提示词与完整留痕的 ``tools`` 字段。
TOOL_LOOP = "tool_loop"


#: 工具清单进 prompt（≤6 个 schema）：E4 首片，默认 off（既有提示词逐字不变）。
TOOL_PACKAGE_PROMPT = "tool_package_prompt"

# RoomState (批2b): the [局况] block that says who is talking to whom.
ROOM_STATE = "room_state"
# Memory sinking (批3b): a lexicon/term that keeps being retrieved becomes a
# proposal (L4 → L1) so she knows it next time instead of looking it up.  Only
# ever writes pending proposals; nothing takes effect without approval.
MEMORY_SINKING = "memory_sinking"
# Memory proposals v2 (批3c): the extractor only proposes open_loop (unfinished
# business) and repeat (a pattern seen >=3 times), each carrying its evidence
# text.  Off = the legacy five-slot prompt.
MEMORY_PROPOSALS_V2 = "memory_proposals_v2"
# Speak contract (B3 回合契约): generation paths treat raw model content as an
# internal draft and render it to spoken-only text before sending.  On by
# default -- it is a contract, not an experiment.  Roll back with
STATE_ASSEMBLY = "state_assembly"
# Anti-AI-phrase stop (批C 表达层): 出稿时机械删掉命中套话的子句
# （跟我说说/我听着呢/别硬撑…，真人语料里几乎不出现）。只做减法、不调模型。
# 默认开；YINYUE_FEATURE_ANTI_AI_PHRASE=0 回滚。
ANTI_AI_PHRASE = "anti_ai_phrase"
# Residual-tail stop (2026-09-27 语气残留收口): 出稿时机械删掉高置信的
# 「反将/旧账/算账」子句（倒是你…查岗？／反正菜名没漏／咱俩扯平…）。
# 72 轮探针实测：正面示范给出后模型仍会追加一句甩回，prompt 收敛到头，
# 这里只做机械兜底。默认开；YINYUE_FEATURE_RESIDUAL_TAIL_GUARD=0 回滚。
RESIDUAL_TAIL_GUARD = "residual_tail_guard"

# Speak tone review (2026-09-27 语气残留实验): 出稿过滤器 system 追加一段语义审查——
# 把「反问对方 / 拿旧事辩护 / 赌气罢言」删掉或改成轻接，判断交给出稿模型，
# 代码不加词表。默认关（实验 A/B/C 对照用；出数据后再定默认）。
# YINYUE_FEATURE_SPEAK_TONE_REVIEW=1 开。
SPEAK_TONE_REVIEW = "speak_tone_review"

# 短句种子注入（只读）: 默认开；YINYUE_FEATURE_SHORT_REPLY_SEED=0 回滚，先私聊主人灰度。
SHORT_REPLY_SEED = "short_reply_seed"

# 消息归属（2026-09-30 19:08 事故根治）：谁是说话人、这条消息「对谁说」，由唯一
# owner（core.conversation.addressing.resolve_attribution）读时判定；窗口逐行渲染、
# [消息归属] 块、路由 @/引用判定、局况、记忆抽取共用它——不再有第二个模块自己写
# @/[引用 正则，也不许回落到"最近说话人"。默认开；关掉＝退回旧渲染路径
# （不加块、不加后缀，不半开半关）。YINYUE_FEATURE_ATTRIBUTION=0 回滚。
ATTRIBUTION = "attribution"

# P0-1 表情包链路（穷举整改，2026-10-16）：把「发哪个表情」从"必须是注册表里的一个
# 名字"（tool schema 的 enum + 精确名闸）改成**候选语义**——她可以用自己的话描述想发
# 什么，代码给出候选与命中理由；歧义/无命中时**弃权并说清楚**，不拿最像的一个顶替。
# 唯一读点两处（同一 flag，不许各写一份字面量）：
# `agents/tools/send_sticker.py::_sticker_candidates_mode()`（选择侧）与
# `core/execution/wx_sticker_sender.py::_sticker_candidates_mode()`（发送侧）。
#   * off    ← **默认**：逐字旧行为——enum 留在 schema 里、未命中报"不存在"、
#              "没找完"与"没有这张"同形（回滚位）；
#   * shadow        ：选择与发送仍走旧行为，只把候选报告/命中理由落日志（量具）；
#   * on            ：自由描述参与选择；歧义 → 弃权；"没搜完"与"不存在"分形。
# 回滚：``YINYUE_FEATURE_STICKER_CANDIDATES=off``（每次读环境，不用重启）。
# 数据面（别名跨条目碰撞、脏 label）由 registry.json 侧另行处理，本 flag 不删数据。
STICKER_CANDIDATES = "sticker_candidates"
# task-51（B）：封口排队前先判"这条消息是否已被最近一次回复答过"，是则吸收不排队。
# off=逐字旧行为；shadow=只记 group_absorb_shadow；on=吸收（不排第二个回合）。
GROUP_ABSORB_ANSWERED = "group_absorb_answered"
# ①³ 写侧随手记（穷举整改，2026-10-16）：把「这一轮值不值得记」从读侧的关键词检索 /
# 模型手记，改成**写侧确定性观察**——每轮最多 3 条候选（原话字面子串 + span + 说话人 +
# 时间），允许 0 条；shadow 只落日志（量具），不写库、不进 prompt。
# 唯一读点：``core/memory/note_writer.py::worth_remembering_mode()``（三态，默认 off）。
#   * off    ← **默认**：观察器不产生候选、不写任何日志（逐字旧行为，回滚位）；
#   * shadow        ：照算候选，只把 ``worth_remembering_shadow`` 落日志（覆盖率/保真率量具）；
#   * on            ：候选可供下游消费（本轮只留接口，尚无产品接线）。
# 回滚：``YINYUE_FEATURE_WORTH_REMEMBERING=off``（每次读环境，不用重启）。
WORTH_REMEMBERING = "worth_remembering"
# ①³ notes 双条件机械闸（cue 词 且 cov<0.35）：仅当「问句带指代线索」且「问句 token
# 在窗口里几乎不出现」才把随手记并进候选——零重叠指代题是 notes 唯一的增量区，
# 其他题集上它会挤掉会答对的 chain 条目。唯一读点：
# ``core/memory/note_recall.py::notes_gate_mode()``（三态，默认 off）。
#   * off    ：现状逐字（读侧不看 notes）；
#   * shadow ：照算，只把 ``notes_gate_would_fire`` 落日志（cue/cov/是否合并/若开会给哪 3 条）；
#   * on     ：fire 才把 notes 并进候选。回滚：``YINYUE_FEATURE_NOTES_GATE=off``。
NOTES_GATE = "notes_gate"
#: 群聊读场：off 不判定 / shadow 只审计 / on 进决策（Step 5 先做 shadow）。
GROUP_SCENE = "group_scene"
#: 意图调度 tick（群聊v2 §5 TODO 1）：off 不登记不扫不执行（与接线前逐字节一致）；
#: shadow 只扫到点意图并审计 ``intention_tick_shadow``（不投递、不改状态）；
#: on 由调度器每分钟执行 **reminder 意图**：reminders 仍是唯一执行权威，投递成功
#: → mark_done + intention_closed；可重试失败留 open 下一 tick 重试，到 3 次止损
#: → mark_failed + intention_closed；仍有 open 意图的提醒由 tick 独占，扫描器让位；
#: 到点意图超 10 分钟仍未收口（tick 停摆）→ 扫描器立即兜底投递，不静默积压。
INTENTION_TICK = "intention_tick"

#: 对话侧销账（P1-1）：off 一行不走（与接线前逐字节一致）；shadow 只记
#: ``intention_close_shadow``（打算关哪几条 + 触发句），不改状态；
#: on 才真的 close_intention + save_world（纯本地、零外发）。
OPEN_LOOP_CLOSE = "open_loop_close"
DIRECT_SEND = "direct_send"

# Plan loop (N2 规划-执行两层循环, 2026-10-06)：**外层步骤序列 + 内层对比与换方法三级**
# （`core.contracts.content_plan`，R-C1..R-C7；字段级契约见 `docs/decisions/架构评估.md` §3.2）。
# 预算口径按 §7 已定决策**写死**：单步**尝试 ≤ 3 次 = 最多换 2 次方法**
# （`StepBudget.max_method_switches_per_step` 由尝试数推导，不是第二个常数）。
# **三态**（用 :func:`mode` 读，别用 :func:`enabled`——它会把 shadow 读成 False）：
#   off    ← **默认**：一行都不走。planner 门（`_is_task_session`）、旧规划 prompt、
#            原生工具循环全部照旧，与接线前**逐字节一致**（回滚位）
#   shadow → 只**判定 + 留痕**：原生工具循环的 observation 过后跑一次内层对比并写台账
#            （`inner_compare_*`），但对外行为一个字不改（不改 prompt、不换方法、不回外层）
#   on     → N2 口径生效：规划 prompt 的 JSON schema 要求三字段（R-C2）、
#            `Planner.run_plan_loop` 按级换方法（记 Switch）与回外层重规划
# 唯一读点是 `agents/planner.py::plan_loop_mode()`（三态只在那里翻译），
# 其余地方一律读 `plan_loop_tracing()` / `plan_loop_enforcing()` 两个布尔。
#
# **第二期（2026-10-06）**：路由两处装配点（直回 + 触发）在 `on` 时跑一次 `plan_turn`，
# 把 `PlanResult` 挂到 `context.plan_result`，生成侧只**读**它（content_plan 投影）。
# **第三期（批B Step B2，2026-10-13）**：装配同时留下可变 `Plan`（`context.plan_object`），
# 生成口 `core/routing/turn_pipeline.generate_turn` 在 `on` 时用 `Planner.run_plan_loop`
# **按步骤执行「只读工具」**——白名单 `agents/planner.py::PLAN_LOOP_READ_ONLY_TOOLS`，
# 写/外发工具（send_message/create_reminder/notify_user/remember/send_sticker/…）
# 一律不自动执行，仍走 TaskSession 显式确认路径；执行结果写 `context.plan_execution`，
# 由表达层渲染成 `[步骤执行结果]` 紧随 `[本轮步骤]` 进 prompt；表达仍由生成层负责。
# 纯表达计划/含写工具/无工具/执行失败 → 一律降级为「无计划」，不得打断回合；
# off/shadow 与接线前逐字节一致（`plan_object` 不挂、执行口一行不走）。
PLAN_LOOP = "plan_loop"
#: ⑥ 经⑩读持久人设（批 B Step B3）：off 不读 / shadow 只审计 / on 才把
#: 名字/性格/语气摘要带进规划上下文。唯一读点
#: ``core/execution/persona_channel.py::persona_channel_mode``。
PERSONA_CHANNEL = "persona_channel"
#: （2026-10-23 退役）房间片段 ROOM_EPISODE 随 N1 读场整族删除：它的唯一读点
#: ``core/context/room_episode.py::episode_mode`` 与唯一消费者 ``scene_effects``
#: 一起消失。三态表 81 → 80。
#: 自我投影（批D Step 36）：她说过的话进②；off 不写、shadow 只审计、on 才写。
SELF_MODEL = "self_model"
#: 显著性读数（批D Step 36）：②事件流折算激活度；off 不算、shadow 只审计、on 进 [局面]。
SALIENCE = "salience"
#: 巩固环（批D Step 38）：②里高显著的情节 -> ⑨长期记忆（memory_projections）。
#: off 一行不走 / shadow 只审计候选（consolidation_shadow）/ on 才幂等 upsert。
#: 唯一读点 ``core/quality/consolidation.py::consolidation_mode``。
CONSOLIDATION = "consolidation"
#: 情绪分层（批D Step 35）：瞬时情绪之外派生心境慢层与对人的态度；
#: off 一行不走 / shadow 只在理解时刻与出口写读数（emotion_layers_shadow /
#: emotion_layers_outbound）/ on 才把心境行放进局面渲染。唯一读点
#: core/context/emotion.py::layers_mode。
EMOTION_LAYERS = "emotion_layers"
#: 噪音闸门：只剩「同一人完全相同文本十分钟内不重复发起」的零 token 前置闸门
#: （2026-10-23 起；模板前缀/近重复/拍人频率/速率/疲劳/静默六层已删——那些是语义）。
#: off 一行不走 / shadow 只记 noise_gate_shadow / on 才真跳过。唯一读点
#: ``core/context/noise_gate.py::noise_gate_mode``。
NOISE_GATE = "noise_gate"
#: 连续性（批D Step 49）：她说过的话作数——生成前注入 [你说过·作数] + 出口复读审计。
SELF_CONTINUITY = "self_continuity"
LLM_DEGRADATION = "llm_degradation"  # 批D 路线5：模型输出降级账本（off/shadow 只审计）
HUMAN_TONE = "human_tone"  # 批D 路线6：说话质感读数（模型腔/人味；off/shadow 只审计）
VOICE_INJECT = "voice_inject"  # 批D 步3：人话 voice 注入（群聊/私聊两套 register；shadow 先行）



# Action rules (N4 规则引擎, 2026-10-06)：**动作前置条件**的唯一判定点
# （``core/rules/engine.py``，`docs/decisions/架构评估.md` §3.4 :254 / §6 P1 :404）。
# 它把「说不说」的规则表（``policy_rules`` + ``conditions_json``）延伸到「做不做」：
# 五维 = ``condition`` / ``target`` / ``quota`` / ``action`` / ``priority``，
# 数据面**复用同一张表同一条读取通道**（key 前缀 ``action_rules`` +
# ``SpeechStore.get_revision()`` 缓存）——**改一行数据即生效、不用重启**。
# **三态**（用 :func:`mode` 读，别用 :func:`enabled`——它会把 shadow 读成 False）：
#   off    ← **默认**：一行都不走。12 处动作前置校验点各自的原判定照旧，
#            与接线前**逐字节一致**（回滚位）
#   shadow → 判定 + 留痕 + 记账，但**不拦**：``RuleDecision.allowed`` 恒为 True，
#            "本来会拦什么"只进 ``explanation`` / ``shadow``（先跑影子读数）
#   on     → 判定生效：拒绝即 ``allowed=False``，调用点照此拦下动作
# 唯一读点是 ``core/rules/engine.py::RuleEngine.mode``（三态只在那里翻译），
# 其余地方一律读 ``RuleDecision.mode`` / ``RuleDecision.shadow``。
RULES = "rules"

# Slot fill (N5 参数收集状态机, 2026-10-06)：**缺参数 → 问 → 确认 → 执行**的唯一状态机
# （``core/tasks/slot_fill.py``；`docs/decisions/架构评估.md` §3.5 :269 与条款 R-P1..R-P8 :165-172；
# 目标架构 `docs/全链路架构审计与目标架构.md` §2.2 L7 :349 + 乱点 #22 :358）。
# 它收编 ``agents/conversational.py`` 那一格一次性确认（现 :2183-2192；文档写的是旧口径
# :1708-1717「我准备这样做：…可以继续吗？」——**不记 pending 状态、无引用校验**）。
# **三态**（用 :func:`mode` 读，别用 :func:`enabled`——它会把 shadow 读成 False）：
#   off    ← **默认**：一行都不走（连 ``core.tasks.slot_fill`` 都不会被 import），
#            与接线前**逐字节一致**（回滚位）
#   shadow → 判定 + 落库 + 留痕，但**对外文本一个字不改**（仍说旧的那句）：先跑读数
#            ——缺槽位率 / 一次问齐率 / 引用命中率 / "说了好但引用对不上"的次数
#   on     → 状态机文本对外生效：缺槽位就问、补齐就确认、**引用对得上才执行**
# **前置**：落库那一侧还要 ``WORK_ITEM_LEDGER`` 同时开（台账写入只有那一把 flag 门，
# 见 ``core/tasks/store.py``）；LEDGER 关时状态机照样判定，只是不落库。
# 唯一读点是 ``core/tasks/slot_fill.py::slot_fill_mode``，其余地方一律读
# ``tracing()`` / ``enforcing()`` 两个布尔。
SLOT_FILL = "slot_fill"

# Exec receipt (A1-d 执行回执接线, 2026-10-06 第二期)：**「答应了就必须有回执」**
# 的结构约束（`docs/archive/任务清单_2026-10-06.md:43` A1-d 原始定义 + `docs/decisions/架构评估.md:96` / §5 X8 第③条前置）。
# 它把 N5 的 `EXECUTING→DONE` 接到**真实执行结果**上，并在**计划/回执层**立一条不变式：
#
#   一个「要替主人做事」的步骤（``core/tasks/slot_fill.py::PROMISE_TOOLS``，= 那三个
#   毁灭性工具），**只有在拿到执行回执之后才允许被当作已完成**；没有回执 →
#   这一步不许完成，该计划**不得视为完成**，只能走 R-C5 的上报
#   （``over_budget`` / ``asked_user`` 路径：问用户 / 明说做不到）。
#
# **明确不做**（主人早先的裁决）：不做出站关键词黑名单——那是在生成出口查承诺词再拦/改写，
# 是 prompt 级补丁；本开关一行出口过滤器都不加（守卫见 ``tests/test_exec_receipt.py`` ⑦）。
#
# **三态**（用 :func:`mode` 读，别用 :func:`enabled`——它会把 shadow 读成 False）：
#   off    ← **默认**：一行都不走（连 ``core.tasks.slot_fill`` 的回执段都不进），
#            与接线前**逐字节一致**（回滚位）
#   shadow → 收口 + 落库 + 留痕，但**不拿结论改这一轮对外说的话**（先跑读数）
#   on     → 回执缺失时**计划不得当完成**：这一轮改说上报那一句（问用户 / 明说没做到）
# **前置**（次序关系，不是同一个布尔表达式）：``YINYUE_FEATURE_SLOT_FILL`` 非 ``off``
# ——没有 N5 状态机就没有「待收口的 ``EXECUTING``」，回执无处可挂，按 ``off`` 处理。
# 落库那一侧另读 ``WORK_ITEM_LEDGER`` × ``WORK_ITEM_RUNS``（那把门在 ``core/tasks/store.py``）：
# 台账/run 关着时只能进程内收口（没有可查的回执行），本开关照旧如实上报。
# **唯一读点**是 ``core/tasks/slot_fill.py::exec_receipt_mode``。
EXEC_RECEIPT = "exec_receipt"

# Command trace (A1-d 命令链接线, 2026-10-06 第二期)：把**已经存在但零生产调用方**的
# ``core/terminal/command_service.py::CommandService.record_execution`` 接到**真实执行落点**
# （`docs/archive/任务清单_2026-10-06.md:43` 原文：「把 ``record_execution`` 接到真实执行落点，让
# 『谁下达 → 执行到哪』在真实流量上可查」），使三条判据在真实流量上同时成立：
#   ① ``authorization_records`` 有行；② ``command_records.execution_ids`` 非空；
#   ③ ``terminal_events.authorization_id`` 非空。
#
# **不新建表、不新建第二套 id 体系**：命令 id 就是入站消息事件行上 ``command_id`` 列
# 已有的那个值（``record_message_events`` 建命令时回填）；授权 id 由 ``command_id``
# 派生（``uuid5``，同一次执行重复调用得到同一个 id）——不重新发明 id。
#
# **三态**（用 :func:`mode` 读，别用 :func:`enabled`——它会把 shadow 读成 False）：
#   off    ← **默认**：一行都不走——不判定、不建授权、不写事件、不调
#            ``record_execution``（连 import 都不进），与接线前**逐字节一致**（回滚位）
#   shadow → 只判定 + 只留痕（structlog ``cmd_trace_*``），**一个字节都不落库**
#            （先跑影子读数：本可查几次、哪一步查不到）
#   on     → 落库：把这次工具执行记成命令链上的一条执行行 + 执行事件行
# 失败路径同样留痕（``ok=False`` + ``error``）：「执行到哪」= 到失败那一步 + 原因。
# **唯一读点**是 ``core/terminal/cmd_trace.py::cmd_trace_mode``。
CMD_TRACE = "cmd_trace"

# Output gate (MVP-2 输出闸门, 2026-09-30 21:40 串台事故)：出站文本的四个确定性
# 检查（禁语/群聊字数/重复指纹/无证据断言）。**三态**，不是布尔：
#   off    完全不做事（不判定、不审计）
#   shadow 只判定 + 只写审计/计数，**不改文本、不阻断发送**（默认值）
#   on     执行处置（删子句/截断/拦/最多 1 次重生成）
# 三态一律用 :func:`mode` 读；``enabled()`` 只看真值，会把 shadow 读成 False，
# 那是"影子门悄悄变成不做事"的现成通道（已踩过：position_gate/admission 各写了
# 一份读法、未知值一个回落 off 一个回落 shadow）。``=1/true/yes/on`` 等价 on，
# ``=0`` 走调用方给的 default。影子先跑满 N 小时再切 on；回滚只需改环境变量，
# 不用重启（每次读环境）。
OUTPUT_GATE = "output_gate"

#: 发送 dry-run（演练开关，2026-10-15）：on = 走完闸门/渲染/审计但**不真发**（不点微信、
#: 不调 Provider），只写一条 audit（status=dry_run）+ `dry_run_send` 日志；off = 正常发送。
#: 用途：非管理员 / 不想打扰群时，完整看她的"读-判-写"行为。唯一读点 core/speech/outbound.py。
DRY_RUN_SEND = "dry_run_send"

# ---------------------------------------------------------------------------
# N3 话题簇与锚点（焦点状态，P1；`docs/话题簇与锚点规格.md` + `docs/decisions/架构评估.md` §3.3）
# ---------------------------------------------------------------------------
# 规格已定决策 11 ★：**默认关**（先 shadow 攒分，评测达标后再决定是否默认开）。
#
# 它回答的是**现状全系统没人回答**的那个问题：这一轮在聊哪件事、主角是谁
# （`core/context/threads.py` 是 canonical owner；2026-09-30 21:18「话题主角被
# 瞬移」串线事故的成因就是缺这层，消费方只能退回"最近说话人"）。
#
# **三态读法**（用 :func:`mode` 读，别用 :func:`enabled`——它会把 shadow 读成
# ``False``，那是"影子门悄悄变成不做事"的现成通道）：
#   off    ← **默认**：本模块一行都不走——不判定、不读/不写 ``session_focus``、
#            不注入 ``[话题簇与锚点]`` 块（与接线前**逐字节一致**，回滚位）
#   shadow → 只算：判定 + 写 ``session_focus`` + 留痕，**不注入块**
#            （``.env:324-327``：未达标——锚点 0.500（门槛 0.85）、
#             换人误判 0.113（门槛 0.05），开 on 会认错人）
#   on     → 同 shadow 的判定/落库/留痕，**额外注入** ``[话题簇与锚点]`` 块
#
# **回滚面**：``YINYUE_FEATURE_THREADS=off`` → 整块不注入、消费方退回现状
# （**不半开半关**，同 ``attribution`` 口径）；``session_focus`` 行**保留但停读**
# → 行为退回「当轮判定」（R-T5 原文：丢行只损失"一句确认"）。
#
# **唯一读点**：``core/context/threads.py::threads_mode``；判定/落库读
# ``threads_enabled()``（``mode != off``），**注入块**读 ``threads_inject_enabled()``
# （只有 ``on``）。都不许各自写 ``features.enabled(...)``。
THREADS = "threads"

# ---------------------------------------------------------------------------
# M2-4 claim 台账（事实权威，2026-10-06；`docs/archive/任务清单_2026-10-06.md:71`，
# 落点 `core/context/message_facts.py`，迁移 56）
# ---------------------------------------------------------------------------
# 它把「像不像编造」（形态无穷、只能枚举词表）换成「**有没有 claim 支撑**」
# （有限：字段词表 + 三级权威 + 更正链）。
#
# **权威序边界**（与既有三份存储不重叠，见 ``core/context/message_facts.py`` 文件头）：
# ``person_notes``（迁移 48，主人手写半长期备注）/ ``person_observations``（抽取的
# 观察）/ ``memory_projections``（事件投影）各管各的"内容"；本台账只管"内容之间的
# **效力次序**"——谁在何时自报 / 转述 / 推测了什么、权威级、更正链。**不复制**它们的
# 正文，只用 ``source_chat_id`` / ``source_message_id`` 指向原始证据。
#
# **三态**（用 :func:`mode` 读，别用 :func:`enabled`——它会把 shadow 读成 False）：
#   off    ← **默认**：本模块一行都不走——不查库、不写台账、闸门的
#            ``unfounded_claim`` 仍按 M2-1 的窄口径**只观测**（与接线前**逐字节一致**，
#            回滚位）。表本身由 ``create_all`` / 迁移 56 建，与开关无关
#            （同 ``person_notes``：schema 是 schema，行为是行为）
#   shadow → 判定 + 留痕 + 写台账，但闸门**只记不动**（先跑影子读数）
#   on     → 台账结论生效：无 claim 支撑的断言 / 与权威值冲突的断言**降级为"没把握"**
#            （或按配置拦下），并且 ``unfounded_claim`` 从「只检测」升级为「可拦」
#            ——**解除的只是 GATE_SEVERITY_BLOCK 上的窄口径限制**，观测项作用域
#            （``OBSERVE_SCOPE`` 的"静态形态只记不改"）一字不动
#
# **唯一读点**是 ``core.context.message_facts.claim_ledger_mode``，其余地方一律读
# ``ledger_tracing()`` / ``ledger_enforcing()`` 两个布尔。
CLAIM_LEDGER = "claim_ledger"

# 三态取值（**唯一**定义处；``position_gate`` / ``admission`` / ``guards`` 的
# MODE_* / GATE_MODE_* 都是这里的别名，不许再各写一份字面量）。
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
MODE_CHOICES = (MODE_OFF, MODE_SHADOW, MODE_ON)

# 兼容别名：闸门模块历史上用 OUTPUT_GATE_* 这套名字。
OUTPUT_GATE_OFF = MODE_OFF
OUTPUT_GATE_SHADOW = MODE_SHADOW
OUTPUT_GATE_ON = MODE_ON

# 环境变量名（三态 flag 里有的**不**走 YINYUE_FEATURE_ 前缀，保持既有线上变量名）。
ADMISSION_MODE_ENV = "YINYUE_ADMISSION_MODE"


# ---------------------------------------------------------------------------
# task-49 事件流 actor 身份归一（tri，默认 off）
# ---------------------------------------------------------------------------
# 背景：`terminal_events.actor_person_id` 长期 95% 是裸平台号（wxid / QQ uin），
# 而 `person_entities`、群名册、记忆投影都用 meta person id；任何按 actor 关联的
# 读取都会静默漏人。写侧唯一读点：
#   `core/terminal/event_store_db.py::_event_to_values`
# off 逐字旧行为（零新日志）；shadow 只写对照读数；on 才归一写入。
# 归一后 `actor_person_id` = meta person id，`actor_account_id` 永远保留原始平台号
# （两列自 on 起才第一次有不同语义，不要拿 actor_account_id 当 person id）。
ACTOR_IDENTITY_NORMALIZE = "actor_identity_normalize"


# ---------------------------------------------------------------------------
# 三态 flag 注册表（WP-02，2026-10-06）
# ---------------------------------------------------------------------------
# 目的：让"三态 flag 被 enabled() 读成 False"从纪律变成写不出来。
#   * kind="tri"      → 只能用 :func:`mode` 读；:func:`enabled` 会直接 raise。
#   * migration=True  → 迁移期开关，必须填 kill_date（到期仍被读 = audit 红）。
#   * migration=False → owner 型开关（长期权威链），必须填 retire_trigger。
# 新增三态 flag 必须同时登记；当前 59（2026-10-24 出站语义闸退役：REPEAT_GUARD +
# ASSERTION_GUARD + REPEAT_SUPPRESS 随近重复/否定翻转判定与"重复命中改不说"一起下线；
# 判据是"代码不替模型判语义"，复读与复用词由模型在同一 transcript 里自己收。
# 前值 63 = 读场/按需记忆整族退役后的读数）
@dataclass(frozen=True)
class FlagSpec:
    kind: str            # "bool" | "tri"
    owner: str           # 判定 owner
    default: object      # bool 或 MODE_*
    retire_trigger: str  # 可跑出读数的退役条件
    kill_date: str = ""  # ISO 日期；migration=True 时必填
    migration: bool = False
    env_var: str = ""    # 非 YINYUE_FEATURE_ 前缀的历史 env 名
    doc: str = ""        # 执行手册 WP 号


FLAG_REGISTRY: dict[str, FlagSpec] = {
    # ---- migration 型（16）----
    SEMANTIC_UNIT: FlagSpec("tri", "core/group_state.py", MODE_OFF,
        "WP-07 删除 coalesce/pat grace 后固定 on 并删 flag", "2027-02-03", True, doc="WP-07"),
    UNIT_MEDIA: FlagSpec("tri", "core/group_state.py", MODE_OFF,
        "WP-07 删除 pat→media 登记后固定 on 并删 flag", "2027-02-03", True, doc="WP-07"),
    PAT_GRACE: FlagSpec("tri", "core/group_state.py", MODE_OFF,
        "WP-07 legacy_merge_path_used=0 连续 7 天后随老路删除", "2027-01-04", True, doc="WP-07"),
    TURN_TRACE: FlagSpec("tri", "core/terminal/turn_trace.py", MODE_OFF,
        "E0 连续 7 天读数非 0 且指回率可测后固定 on 并删 flag", "2027-04-30", True,
        doc="E0-留痕"),
    VERDICT_CONTRACT: FlagSpec("tri", "core/contracts/verdict.py", MODE_SHADOW,
        "E1 影子期 check/violation 读数稳定、误拒 0、载荷覆盖率达标后固定 on",
        "2027-06-30", True, doc="E1-契约"),
    JOB_LEDGER: FlagSpec("tri", "core/jobs/store.py", MODE_OFF,
        "E3 只读 job 连续 7 天读数非 0、安全点违规 0 后固定 on 并删 flag",
        "2027-09-30", True, doc="E3-在手的事"),
    TOOL_PACKAGE_PROMPT: FlagSpec("tri", "core/tools/prompt.py", MODE_OFF,
        "E4 影子期注入块稳定、判按工具名调用成功率 >80% 后固定 on",
        "2027-10-31", True, doc="E4-工具清单"),
    DELIVERY_CONTRACT: FlagSpec("tri", "core/speech/delivery.py", MODE_OFF,
        "E2 影子期六态回执稳定、重复发送率 0、回执覆盖率 100% 后固定 on",
        "2027-05-31", True, doc="E2-出口契约"),
    "admission_mode": FlagSpec("tri", "core/context/admission.py", MODE_OFF,
        "WP-08 降为 understanding 内部步骤后删", "2027-01-04", True,
        env_var=ADMISSION_MODE_ENV, doc="WP-08"),
    PLAN_LOOP: FlagSpec("tri", "agents/planner.py", MODE_OFF,
        "WP-06 并入生成内部后删", "2027-01-04", True, doc="WP-06"),
    STATE_ASSEMBLY: FlagSpec("tri", "core/context/state_assembly.py", MODE_OFF,
        "重复/追账率下降且状态块被消费后固定 on/删 flag", "2027-02-03", True, doc="WP-STATE"),
    CLAIM_LEDGER: FlagSpec("tri", "core/context/message_facts.py", MODE_OFF,
        "WP-08 台账固定 on 后删 flag", "2027-01-04", True, doc="WP-08"),
    SLOT_FILL: FlagSpec("tri", "core/tasks/slot_fill.py", MODE_OFF,
        "N5 参数收集上线后删", "2027-01-04", True, doc="独立"),
    CMD_TRACE: FlagSpec("tri", "core/terminal/cmd_trace.py", MODE_OFF,
        "命令链稳定后固定 on/删", "2027-02-03", True, doc="A1-d"),
    EXEC_RECEIPT: FlagSpec("tri", "core/tasks/slot_fill.py", MODE_OFF,
        "A1-d 执行回执闭环后固定 on/删", "2027-02-03", True, doc="A1-d"),
    # ---- owner 型（5）----
    OUTPUT_GATE: FlagSpec("tri", "core/speech/outbound.py", MODE_SHADOW,
        "出口合规 owner；WP-10 退役 prompt 散文后固定 on", doc="WP-10"),
    THREADS: FlagSpec("tri", "core/context/threads.py", MODE_OFF,
        "N3 焦点 owner；X1 退役完成后固定 on", doc="WP-10"),
    RULES: FlagSpec("tri", "core/rules/engine.py", MODE_OFF,
        "规则引擎数据面；固定 on 后删 flag", doc="独立"),
    OUTBOUND_RHYTHM: FlagSpec("tri", "core/speech/rhythm.py", MODE_OFF,
        "多气泡节奏验收后固定 on/删", doc="活人感-形态"),
    GROUP_SCENE: FlagSpec("tri", "core/context/scene_read.py", MODE_OFF,
        "群聊读场：双卷 shadow 的错换/撤回达标且 on 灰度稳定后固定 on/删", doc="群聊v2"),
    INTENTION_TICK: FlagSpec("tri", "core/context/intention_tick.py", MODE_OFF,
        "到点执行闭环稳定、reminders 迁 WorkItemRunner 后固定 on/删", doc="群聊v2"),
    PERSONA_CHANNEL: FlagSpec("tri", "core/execution/persona_channel.py", MODE_OFF,
        "批B 人设通道影子读数一致、无撤回后固定 on/删", "2027-02-03", True,
        doc="批B-人设"),
SELF_MODEL: FlagSpec("tri", "core/context/situation.py", MODE_OFF,
    "批D 自我投影：影子读数稳定、无撤回后固定 on/删", doc="批D-自我"),
SALIENCE: FlagSpec("tri", "core/context/salience.py", MODE_OFF,
    "批D 显著性：影子读数稳定、无撤回后固定 on/删", doc="批D-显著"),
CONSOLIDATION: FlagSpec("tri", "core/quality/consolidation.py", MODE_OFF,
    "批D 巩固环：影子候选稳定、无误沉淀后固定 on/删", doc="批D-巩固"),
    EMOTION_LAYERS: FlagSpec("tri", "core/context/emotion.py", MODE_OFF,
        "批D 情绪分层：影子读数稳定、无撤回后固定 on/删", doc="批D-情绪"),
    SELF_CONTINUITY: FlagSpec("tri", "core/context/self_contract.py", MODE_OFF,
        "批D 连续性：影子读数稳定、自相矛盾/复读归零后固定 on/删", doc="批D-连续"),
LLM_DEGRADATION: FlagSpec("tri", "core/context/degradation.py", MODE_OFF,
    "批D 降级账本：影子读数稳定、无漏记后固定 on/删", doc="批D-降级"),
HUMAN_TONE: FlagSpec("tri", "core/speech/human_tone.py", MODE_OFF,
    "批D 人话：影子模型腔率稳定下降后固定 on（on=触发表达重写）/删", doc="批D-人话"),
    ACTOR_IDENTITY_NORMALIZE: FlagSpec("tri", "core/terminal/event_store_db.py", MODE_OFF,
        "事件流 actor 归一：shadow 期 unique/ambiguous/none 分布稳定、off-diff 0 复核后固定 on/删",
        doc="①感知-actor 归一"),
    NOISE_GATE: FlagSpec("tri", "core/context/noise_gate.py", MODE_OFF,
        "完全相同文本去重：影子确认 0 误伤后固定 on/删", doc="批D-防刷"),
    VOICE_INJECT: FlagSpec("tri", "core/speech/voice_style.py", MODE_OFF,
        "批D 人话：影子样本人工评审通过、模型味率达标后固定 on/删", doc="批D-人话"),
    RECALL_CHAIN: FlagSpec("tri", "core/memory/recall_chain.py", MODE_OFF,
        "P3 召回链：影子期 hit@1/@3 与端到端 exact 达到决策门（≥50%）后固定 on/删", doc="⑨→③ 选"),
    RECALL_STRUCT: FlagSpec("tri", "core/memory/recall_chain.py", MODE_OFF,
        "P3 结构化召回：影子期 gold_in ≥80% 且端到端 exact ≥50% 后固定 on/删", doc="⑨→③ 选"),
    WECHAT_UI: FlagSpec("tri", "core/execution/gui/wechat.py", MODE_OFF,
        "阶段 6 微信写侧：读库渲染连续 7 天无 not_connected、填字命中率 >90%、"
        "零未批准发送后固定 on 并删 flag",
        "2027-12-31", True, doc="阶段6-微信写侧"),
    GUI_CHANNEL: FlagSpec("tri", "core/execution/gui/cdp.py", MODE_OFF,
        "阶段 6 只读快照连续 7 天无 not_connected 抖动、单屏 <4000 字、零越权点击后固定 on 并删 flag",
        "2027-12-31", True, doc="阶段6-GUI只读"),
    # 寻址边（task-43，owner 型）：退役条件在 retire_trigger；影子期读数稳定后固定 on/删。
    UTTERANCE_EDGES: FlagSpec(
        "tri",
        "core/terminal/utterance_edges.py",
        MODE_OFF,
        "寻址边：shadow 期写入条数与「↳回 X」渲染比例读数稳定后固定 on/删",
        doc="③→⑥ 事实层",
    ),
    WECHAT_SEND: FlagSpec(
        "tri",
        "core/execution/gui/wechat_send.py",
        MODE_OFF,
        "E4 真发过并回读到 server_id、重复发送率 0 之后固定 on 并删 flag",
        "2027-12-31",
        True,
        doc="E4-微信发送",
    ),
    CORRECTION: FlagSpec(
        "tri",
        "core/speech/correction.py",
        MODE_OFF,
        "E4 真更正过 3 次且每次都带 ref、连环更正 0 次之后固定 on 并删 flag",
        "2027-12-31",
        True,
        doc="E4-更正",
    ),
    GUI_BACKENDS: FlagSpec(
        "tri",
        "core/execution/gui/backends.py",
        MODE_OFF,
        "E4 第二只手（UIA 写 / ADB / Vision）真机跑通且 unsupported 拦截 0 漏放之后固定 on 并删 flag",
        "2027-12-31",
        True,
        doc="E4-多手注册表",
    ),
    HANDOFF: FlagSpec(
        "tri",
        "core/jobs/handoff.py",
        MODE_OFF,
        "E4 交接单连续 7 天：办不下去的任务 100% 都有 handed_off + 交接率报得出来之后固定 on 并删 flag",
        "2027-12-31",
        True,
        doc="E4-交接单",
    ),
    DEVICE_LEASE: FlagSpec(
        "tri",
        "core/execution/gui/wechat.py",
        MODE_OFF,
        "E3 设备租约连续 7 天真机并发 0 次互踩（input_busy 有账、抢占 0 次越权）后固定 on 并删 flag",
        kill_date="2027-12-31",
        migration=True,
        doc="E3-设备并发",
    ),
    WECHAT_REVOKE: FlagSpec(
        "tri",
        "core/execution/gui/wechat_revoke.py",
        MODE_OFF,
        "E4 撤回过 3 次且回读确认 3/3 之后固定 on 并删 flag",
        "2027-12-31",
        True,
        doc="E4-微信撤回",
    ),
    LEGACY_VIA_EXIT: FlagSpec(
        "tri",
        "core/speech/legacy_bridge.py",
        MODE_OFF,
        "E3 回复走新出口连续 7 天、回执覆盖率 100%、重复发送率 0 之后固定 on 并删 flag",
        "2027-12-31",
        True,
        doc="E3-出口归一",
    ),
    DECLARED_TOOLS: FlagSpec(
        "tri",
        "core/tools/declarative.py",
        MODE_OFF,
        "E4 声明式工具跑满 90 天无一次「声明与实现不符」后固定 on 并删 flag",
        "2027-12-31",
        True,
        doc="E4-声明式工具",
    ),
    MCP_TOOLS: FlagSpec(
        "tri",
        "core/execution/mcp.py",
        MODE_OFF,
        "E4 MCP 只读工具连续 30 天无一次「越权写」后固定 on 并删 flag",
        "2027-12-31",
        True,
        doc="E4-MCP工具源",
    ),
    SENSORY: FlagSpec(
        "tri",
        "core/senses/snapshot.py",
        MODE_OFF,
        "E4 感官：连续 30 天无一次「截到不该截的窗口」后固定 on 并删 flag",
        "2027-12-31",
        True,
        doc="E4-感官",
    ),
    TOOL_LOOP: FlagSpec(
        "tri",
        "core/tools/turnloop.py",
        MODE_OFF,
        "E4 工具调用闭环：判按工具名调用成功率 >80% 且「该查没查」≤10% 后固定 on 并删 flag",
        "2027-12-31",
        True,
        doc="E4-工具闭环",
    ),
    MEDIA_STORE: FlagSpec("tri", "core/ingestion/media_store.py", MODE_OFF,
        "P2 媒体基质：隔离根抽取成功率达标（ok/unsupported 均有归因）后固定 on/删",
        doc="②→⑨ 基质"),
    MEDIA_RECALL: FlagSpec("tri", "core/memory/recall_chain.py", MODE_OFF,
        "P3 媒体召回：file 类 hit@1 ≥80% 且整集 exact 不降后固定 on/删",
        doc="⑨→③ 选"),
    RECALL_EPISODE: FlagSpec("tri", "core/memory/recall_chain.py", MODE_OFF,
        "P2 多粒度召回：情节级滑窗向量把 7 道召回不足题拉进 top-3 且整集不降后固定 on/删",
        doc="⑨→③ 选"),
    RECALL_PRIMITIVES: FlagSpec("tri", "core/memory/recall_chain.py", MODE_OFF,
        "P2 多粒度召回：类型无关原语（BM25 词法 + 轮段向量）端到端 exact ≥ 基线 +3pp 后固定 on/删",
        doc="⑨→③ 选"),
    SEARCH_DEEPSEEK: FlagSpec("tri", "agents/tools/web.py", MODE_OFF,
        "外部检索换 DeepSeek：影子期 hit@3/延迟优于 Tavily 且抽样评审不降质后固定 on/删",
        doc="⑩→外部检索"),
    WORTH_REMEMBERING: FlagSpec("tri", "core/memory/note_writer.py", MODE_OFF,
        "①³ 写侧随手记：影子期覆盖率分布稳定、保真率 ≥ 旧写手 3/13 基线、误记率达标后固定 on/删",
        doc="①³ 写侧随手记"),
    NOTES_GATE: FlagSpec("tri", "core/memory/note_recall.py", MODE_OFF,
        "①³ notes 双条件机械闸：影子期 cue 且 cov<0.35 的触发率与真实增益对得上、零重叠题并集 ≥50% 后固定 on/删",
        doc="①³ notes 门控"),
    STICKER_CANDIDATES: FlagSpec("tri", "agents/tools/send_sticker.py", MODE_OFF,
        "P0-1 表情包候选：影子期 歧义/无命中/没搜完 三类读数稳定、误发 0、抽样评审通过后固定 on/删",
        doc="P0-1 表情包链路"),
    GROUP_ABSORB_ANSWERED: FlagSpec(
        "tri", "core/group_state.py", MODE_OFF,
        "task-51 B：影子期 吸收/漏吸收 读数稳定、二次回合数降到 0 后固定 on/删",
        doc="task-51 已答消息不再二次成回合",
    ),
    OPEN_LOOP_CLOSE: FlagSpec("tri", "core/context/intention_close.py", MODE_OFF,
        "P1-1 销账：影子期 误关 0（每条都能指回触发句）、关账率 >0 后固定 on/删",
        "2027-06-30", True, doc="P1-1 销账"),
    DIRECT_SEND: FlagSpec("tri", "loop/direct_send.py", MODE_OFF,
        "点名即发：shadow 三周内记录的指令覆盖主人全部显式点名、误发 0，才转 on 并退役本旗标",
        "2027-06-30", False,
        doc="P3-5 点名即发（只认私聊、只认主人、每条消息只发一次）"),
}

TRI_STATE_FLAGS: frozenset[str] = frozenset(
    name for name, spec in FLAG_REGISTRY.items() if spec.kind == "tri"
)

def _normalise(name: str) -> str:
    return name.strip().upper().replace("-", "_")


def env_key(name: str) -> str:
    return f"{_PREFIX}{_normalise(name)}"


def enabled(name: str, default: bool = False) -> bool:
    spec = FLAG_REGISTRY.get(name)
    if spec is not None and spec.kind == "tri":
        raise ValueError(
            f"{name} 是三态 flag，必须用 features.mode() 读（enabled() 会把 shadow 读成 False）；"
            f"owner={spec.owner} doc={spec.doc or '-'}"
        )
    raw = os.environ.get(env_key(name))
    if raw is None:
        return bool(default)
    return raw.strip().lower() in _TRUTHY


def dry_run_send(chat_id: str = "") -> bool:
    """YINYUE_FEATURE_DRY_RUN_SEND：发送 dry-run（走全链路、不投递）。

    ``chat_id`` 命中 ``YINYUE_DRY_RUN_SEND_ALLOW_CHATS``（逗号分隔）时**不拦**——
    用于「只在测试群真发、其它会话继续演练」的灰度；空 chat_id 视为不命中。
    """
    try:
        if not enabled(DRY_RUN_SEND, False):
            return False
        allow = [
            item.strip()
            for item in (text("YINYUE_DRY_RUN_SEND_ALLOW_CHATS")).split(",")
            if item.strip()
        ]
        if chat_id and chat_id in allow:
            return False
        return True
    except Exception:  # noqa: BLE001 - 读不到按关，绝不因为开关故障挡住正常发送
        warn("core/features.py:893 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def snapshot() -> dict[str, object]:
    """Return every registered flag plus unregistered YINYUE_FEATURE_* keys.

    Tri-state registry entries are read with :func:mode and reported as
    `off`/`shadow`/`on`; everything else keeps the boolean spelling.
    """
    flags: dict[str, object] = {}
    for name, spec in FLAG_REGISTRY.items():
        if spec.kind == "tri":
            flags[name] = mode(name, spec.default, env_var=spec.env_var or None)
        else:
            flags[name] = enabled(name, bool(spec.default))
    for key in os.environ:
        if not key.startswith(_PREFIX):
            continue
        name = key[len(_PREFIX):].lower()
        if name in flags:
            continue
        if name in TRI_STATE_FLAGS:
            continue  # 已按 mode 读过，不许回落 enabled()
        flags[name] = enabled(name)
    return flags


def describe(name: str, default: bool = False) -> str:
    """Small helper for diagnostics and docs."""
    spec = FLAG_REGISTRY.get(name)
    if spec is not None and spec.kind == "tri":
        state = mode(name, spec.default, env_var=spec.env_var or None)
        return f"{name}={state} ({spec.env_var or env_key(name)})"
    state = "on" if enabled(name, default) else "off"
    return f"{name}={state} ({env_key(name)})"


def mode(name: str, default: str = MODE_SHADOW, *,
         env_var: str | None = None) -> str:
    """Read a **three-state** flag (``off``/``shadow``/``on``).

    This is the **only** three-state reader in the repo.  Distinct from
    :func:`enabled` on purpose: that one is boolean and returns ``False`` for
    ``shadow``, which would silently turn a shadow-only gate into a no-op.

    Args:
        name: flag name; read from ``YINYUE_FEATURE_<NAME>``.
        default: returned when the variable is unset **or** holds an unknown
            value.  The caller states it explicitly so the call sites can no
            longer disagree: 闸门默认 ``shadow``（先影子），admission 默认
            ``off``（行为=现状）。
        env_var: explicit environment variable name for the older three-state
            switches that do not use the ``YINYUE_FEATURE_`` prefix.  The only
            live one is ``YINYUE_ADMISSION_MODE``; the former
            ``YINYUE_POSITION_GATE_MODE`` was retired with WP-04 and has no
            reader left.

    Truthy values (``1``/``true``/``yes``/``on``) normalise to ``on`` so the
    boolean convention keeps working; ``off``/``shadow`` are returned as-is;
    anything else falls back to ``default`` instead of being guessed at.
    """
    raw = os.environ.get(env_var or env_key(name))
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return MODE_ON
    if value in MODE_CHOICES:
        return value
    return default
