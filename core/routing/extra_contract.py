"""``AgentContext.extra`` 的键契约（WP-06 / WP-08 §8.2）。

从 ``core/routing/turn_pipeline.py`` 整块搬出（2026-10-22，R2 拆大文件）：纯字面量、
零依赖、无函数。``turn_pipeline`` 原位 re-export，历史导入面不变。

**一个静默失败点**：``agents/base.py:74`` 是在 ``try/except Exception: return None  # fail-open``
里懒取这个集合的——re-export 一断不会报错，只会**悄悄少几条 contract key**。
所以动这里必须手动跑 ``scripts/check_invariants.py`` 与 ``scripts/audit_extra_contract.py``，
并顺手确认 ``from agents.base import _extra_contract_keys as f; f() is not None``。
"""

from __future__ import annotations


EXTRA_CONTRACT: frozenset[str] = frozenset({
    #: WP-06 登记：直回 / 触发两条路径现有 ``AgentContext.extra`` 键的并集
    #: （《当断则断_执行手册》§6.2 步骤 3）。登记后只允许删除；新增必须说明谁读。
    "_last_send_log_id",
    "_pending_stickers",
    "account_id",
    #: P2：query_context 只在读取副本写规范化有效时间；BeliefMemoryProvider
    #: 和索引有效性核对读取它。空值表示当前，不是记录时间，也不改变身份授权。
    "belief_as_of",
    "chat_id",
    "continuation",
    "continuation_criteria",
    "continuation_need_clarify",
    "group_recent_context",
    #: task-51 B：她真发出回复的那回合，prompt 窗口里**入过 prompt 的入站** event_id
    #: （seq 升序、去重、≤40）。写点 core/context/assembler.py（渲染
    #: ``[群聊最近消息…]`` 块时）；读点本文件 ``router._group_mgr.note_self_message(
    #: ..., consumed_msg_ids=...)`` → ``GroupStateManager.note_self_message``，
    #: 封口时判"已答过就别再开第二回合"。
    "group_window_event_ids",
    #: Step 2d（2026-10-23）：同一批窗口行的瘦身副本（event_id/sender_name/content，
    #: ≤40 行）。N1 退役后便签由 perceive 生产，而候选区的规矩是「quote 必须落在
    #: 这回合窗口的原话上」——写点 ``turn_pipeline._stash_window_event_ids``，读点
    #: ``agents/conversational.py::_stage_perceive_notes``（只验证据，不渲染）。
    "window_rows",
    "images",
    "importance",
    "is_group",
    "media_path",
    "media_paths",
    "message_time",
    #: 入站呼唤事实（db/sync_service.py 写 ``inbound.extra["mention_self"]``）；
    #: 批B1 四门/直回读它当 addressed 证据（canonical R-A12 S1）。
    "mention_self",
    "msg_type",
    "platform",
    "pronoun_rows",
    "referent",
    "memory_summary", "retrieval_hits", "stance_hint", "position_hint",
    "memory_recall_n",
    "focus_rows", "focus_current", "focus_repo", "focus_result",
    "reply_account",
    "room_situation_block",
    "sender_id",
    "sender_name",
    "session_id",
    "speech_persona_id",
    "speech_revision",
    "summons",
    "unit_fold",
    "user_message",
    "window_turns",
    "world_state",
    #: 批D 阶段1：_apply_scene_runtime 把 Volition.to_dict() 暂存进 trace；
    #: agents/conversational.py::_synthesize_band 经 context.extra 读 delivery_hint。
    # _apply_scene_runtime 把按需召回命中暂存进 trace -> 本文件透传 extra -> 生成侧消费
    #: agents/conversational.py::_render_short_reply_seed_block 读；
    #: agents/planner.py::_mark_short_reply_signal 在工具成功后写。
    "short_reply_signal",
    # ---- WP-08 8.2 runtime guard：router 之外的真实 AgentContext.extra 写点 ----
    "_last_audit_id",
    "_last_sent_ok",
    "_output_gate",
    #: 本轮用户明确提供的正文，可支撑其中出现的记忆事实；由到点触发写入，
    #: 出站记忆一致性闸门读取。
    "memory_claim_supported_texts",
    #: 出站记忆一致性闸门留下的改写原因与状态；只供审计和追踪。
    "_memory_claim_gate",
    #: core/speech/outbound.py 审计/对账键：_addressee_guard_decision 由
        "_addressee_guard_decision",
    "affect",
    "emotion",
    "image_available",
    "link_component_id",
    "person_id",
    "relation_to_bot",
    "sticker_descriptions",
    "sticker_ids",
    "sticker_source",
    "terminal_memory_attached",
    "terminal_memory_block",
    "terminal_memory_count",
    "terminal_memory_negative",
    "terminal_memory_sources",
    "time_block_traced",
    # ---- WP-08 8.2 runtime guard 二次盘点：AgentContext(extra={...}) 构造器字面量 ----
    # scheduler/main.py 写入；agents/base.py 终局事件、core/tasks 台账锚点读。
    "turn_id",
    "parent_event_id",
    # scheduler/main.py 写入；agents/tools/analysis.py 读引用图与媒体路径。
    "quoted_media_path",
    "quoted_sticker_id",
    # agents/proactive_trigger.py 写入；agents/conversational.py 主动回合生成读。
    "force_speak",
    "proactive",
    "record_user_turn",
    # 暂无读者：登记后观察 7 天再决定删（手册 §8.2 步骤 3）。
    "proactive_source",
    "complexity",
    # core/tasks/store.py、core/tasks/slot_fill.py 台账锚点读；生产写点待任务路径接线，
    # 当前由任务台账测试夹具写入。
    "thread_id",
    "msg_id",
    "sender_account_id",
    "sender_person_id",
    # agents/conversational.py::_is_task_session 显式任务会话标记读；当前由测试置位。
    "task_session",
    # ---- WP-08 8.2 步骤 3 退役（写点已删；N1 外壳退役后这些键全仓**无写点**）----
    # reply_intent_line / reply_intent / reply_intent_need_clarify / content_plan /
    # should_send_reply / _policy_decision / turn_intent / unknown_answer /
    # trigger_reason / trigger_reasons
    #: --- 在手的事（U-10）与感官（U-19）：2026-10-21 补登记 ---
    #: 写点 core/context/job_readout.py::collect_job_readouts（on 档写读数、shadow 档写计数）；
    #: 读点 core/context/job_readout.py::build_job_readout_block → 判的第 6 样输入。
    #: 为什么必须登记：strict 档会抛 ExtraContractViolation，而 collect_job_readouts 按
    #: fail-closed 吞掉异常 → 读数一整天没进过 prompt，日志只有一条 violation。
    "job_readouts",
    "job_readout_shadow",
    #: 写点本文件 ``_job_tick``：第六态回执 / 出口消费结果 / 三级授权与撤销 / 交接单。
    #: 读点 core/speech/delivery.py::deliver_pending_approvals、agents 侧回执渲染。
    "job_approvals",
    "job_approval_receipts",
    "job_approvals_granted",
    "job_grant_revoked",
    #: 主人自己点名的步骤当场批掉（core/jobs/policy.py::is_owner_ordered）；
    #: 写点 _approve_owner_ordered()，读点无（运行期日志量具）。
    "job_ordered",
    #: 程序化前置认出的「直接下令」开出来的事（core/jobs/direct.py）
    "job_direct_order",
    "job_grant_scope",
    "job_handoff",
    #: 写点 core/senses/snapshot.py（三档感官的渲染块）；读点判的 ``[现在这一屏]`` 注入。
    "senses_block",
})
