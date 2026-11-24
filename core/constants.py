# -*- coding: utf-8 -*-
"""设计里出现的所有数字，只在这里出现一次。

出处见每段注释（`docs/decisions/*.md`）。改数字的顺序永远是
**图 → 文档 → 这里 → 测试**，不许反过来。

约定：`<区域>_<量>`，单位写在后缀（`_S` 秒 / `_CHARS` 字符 / `_ROWS` 条 / `_DAYS` 天）。
"""

# ── 世界窗口（世界窗口.md §6：三条预算同时生效，先到先逐出）──────────────
WINDOW_MAX_ROWS = 40
WINDOW_MAX_HOURS = 2
WINDOW_MAX_CHARS = 3600
WINDOW_HOT_SESSIONS = 8          # 热缓存会话数（世界窗口.md §10）
WINDOW_TTL_S = 300               # 渲染结果缓存 5 分钟

# ── 判 / 投 / 记（判投记_契约.md §2 输入预算 / §6 输出约束）──────────────
SELF_READOUT_MAX_CHARS = 200     # 自我读数上界
MEMORY_MAX_ITEMS = 2             # 查询结果 ≤2 条（带出处）
TOOL_MAX_CALLS_PER_TURN = 2      # 工具 ≤2 次（含失败）
DELIVER_MAX_PER_TURN = 2         # 每回合最多两条投递
MEMORY_WRITE_MAX = 3             # 候选纸片 0~3 条
OPEN_LOOP_MAX = 1                # 未结事 ≤1
SIDE_REMARK_MAX_CHARS = 40       # 伴随语 ≤40 字，且不带自己的 @
RATE_READOUT_MAX_CHARS = 40      # 限流读数 ≤40 字

# ── 工具包（工具包.md §6 注入上限 / §7 有界）────────────────────────────
TOOL_IN_PROMPT_MAX = 6           # 本轮进 prompt 的 schema ≤6 个
TOOL_TIMEOUT_OBSERVE_S = 8
TOOL_TIMEOUT_ACT_S = 20
TOOL_RESULT_MAX_CHARS = 600      # 工具返回进 prompt 合计 ≤600 字
TOOL_ARGS_SPLIT_OVER = 4         # 参数 >4 个就拆成两个工具

# ── 出口（出口.md §9 预算）────────────────────────────────────────────
OUTBOUND_TIMEOUT_S = 10
OUTBOUND_FILE_TIMEOUT_S = 30
RETRY_BACKOFF_S = 2
RETRY_MAX = 1                    # 只对 retryable、最多 1 次

# ── 记忆 / 情绪（记忆立方体.md / 情绪层_存储与读取.md）──────────────────
MEMORY_CANDIDATE_TTL_DAYS = 7    # 候选纸片 7 天过期
MOOD_EVENT_WINDOW_HOURS = 72     # 情绪只读最近 72 小时事件

# ── 留痕（判投记_契约.md §9.5 量具口径）────────────────────────────────
TURN_TRACE_SAMPLE = 50           # 人工抽 50 条算指回率 / reading 一致率