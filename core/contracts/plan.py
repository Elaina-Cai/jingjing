# -*- coding: utf-8 -*-
"""「在手的事」的契约（**只放类型与常量，不放实现**）。

出处：`docs/decisions/办_在手的事.md`（板 `t_job.png`，`(620, -380, 1000)`）。
图上的三条管子对应这里的三个入口：
    job 读数 / advance()  ← 判在每回合里读与推进（`Plan`）
    只送已批准            ← 只有带 `Approval` 的 `Step` 才进 `出口`（`Status=pending_approval` 的反面）
    任务预算              ← `TOOL_CALLS_PER_JOB`（跨回合累计）

纪律（违反任何一条都算 bug）：
- 这里**没有**第二种决策：判仍是唯一模型点，本文件只是「一件事」那一行的形状；
- 花钱 / 不可逆 / 涉隐私的步骤，没有 `Approval` 就不许调出口；
- 只有 `Step.safe_point` 为真的边界可以中断（防「半个订单」）；
- 本文件不许出现 I/O、不许 import 出口或工具包。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 一件事的状态机：open → awaiting ⇄ open → done / cancelled / abandoned / expired
#: ``cancelled`` = **主人叫停**（正常收尾，他心里有数）；``abandoned`` = **办不下去 / 没人再管**
#: （半途而废）。两者 2026-11-03 前挤在 ``cancelled`` 一个词里，所以"半途而废率"以前算不出来。
JobState = "open | awaiting | done | cancelled | abandoned | expired"
JOB_STATES: tuple[str, ...] = ("open", "awaiting", "done", "cancelled", "abandoned", "expired")

# 预算与上限（办_在手的事.md §9）
MAX_JOBS_PER_CHAT = 3          # 同一会话同时在手的 job ≤3
READOUT_MAX_ITEMS = 3          # 进判的 job 读数 ≤3 条
READOUT_MAX_CHARS = 60         # 每条读数 ≤60 字
TOOL_CALLS_PER_TURN = 2        # 回合上限（与工具包一致）
TOOL_CALLS_PER_JOB = 12        # 任务累计上限（跨回合）
MAX_TURNS_PER_JOB = 20         # 一件事最多推进 20 回合
APPROVAL_TTL_HOURS = 24        # 等批准的超时


@dataclass(frozen=True)
class Trigger:
    """槽 ①：这件事什么时候被想起来。"""
    kind: str = "once"          # once | cron | condition | event
    expr: str = ""              # cron 表达式 / 条件 / 事件名
    next_fire_at: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class Policy:
    """槽 ②：这件事的授权范围（批过一次能管多远）。"""
    scope: str = ""             # 例：「订票:杭州→深圳 2026-11-02」
    condition: str = ""
    max_amount: float | None = None
    expires_at: str | None = None
    revocable: bool = True
    approved_by: str | None = None   # 只有人能写（渠道 + 人）


@dataclass(frozen=True)
class Artifact:
    """槽 ③：交付形态（方案的呈现方式，不是文本内容）。"""
    kind: str = "text"          # text | file | card | page | link
    renderer: str = ""          # 页/卡片用哪个渲染器；空 = 出口默认
    ttl_s: int | None = None


@dataclass(frozen=True)
class Handoff:
    """槽 ④：办不下去时交给人什么（不是「抱歉失败了」）。"""
    url: str = ""
    prefilled: tuple[str, ...] = ()
    screenshots: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Step:
    """一步。`effects` 与工具包的四档同名同义；`safe_point` 决定能不能在这里中断。"""
    n: int
    action: str
    effects: str = "read"       # read | write_local | ui_control | external_send
    safe_point: bool = True
    approved: bool = False      # 只有 Approval 能把它置真


@dataclass(frozen=True)
class Awaiting:
    """在等什么：等人的批准 / 等人的补充 / 没在等。"""
    kind: str = "none"          # approval | input | none
    who: str | None = None


@dataclass(frozen=True)
class Plan:
    """判每回合能给的那一段（契约 §2 的 `plan`，≤1 次）。**不是执行**。"""
    goal: str
    # 2026-10-23：「怎么算做到了」。与 goal 分开——goal 是"要什么"，criteria 是
    # "以什么为验收"。缺了不拦（既有 plan 逐字不变），但有了就能在收口时对账。
    criteria: str = ""
    steps: tuple[Step, ...] = ()
    awaiting: Awaiting = Awaiting()
    job_id: str | None = None   # None = 新开一件事


@dataclass(frozen=True)
class Approval:
    """人给的那一行（只从人来，且只有私聊本人才算授权）。"""
    job_id: str
    step_n: int
    action_hash: str            # 批的是哪一个动作，不是哪一句「好吧」
    who: str
    channel: str                # 必须能证明「是本人」
    at: str
    expires_at: str | None = None


@dataclass
class Job:
    """一件事那一行。append-only：状态变化追加新行，不原地改历史。"""
    job_id: str
    chat_id: str
    goal: str
    criteria: str = ""                      # 验收判据（缺省空串 = 没写）
    owner_person_id: str | None = None      # 谁的委托（跨平台归一到人）
    state: str = "open"
    step_n: int = 0
    steps: list[Step] = field(default_factory=list)
    awaiting: Awaiting = Awaiting()
    trigger: Trigger = Trigger()
    policy: Policy = Policy()
    artifact: Artifact = Artifact()
    handoff: Handoff = Handoff()
    tool_calls_total: int = 0
    turns_total: int = 0
    updated_by: str = ""
    updated_at: str = ""
    #: P3：执行实例关联的 Intention 真源；空串 = 还没关联。
    goal_id: str = ""

    def readout(self) -> str:
        """进判的那一行读数（≤60 字）：`job_id · 目标 · 到第几步 · 在等什么`。

        实现纪律：**先保「在等什么」，再截目标**。反过来（先拼目标再整体截断）
        会把最要紧的那几个字截掉——`tests/test_job_contract.py` 钉住这一条。
        """
        tail = {"approval": "等批准", "input": "等补充", "none": "在办"}.get(
            self.awaiting.kind, "在办")
        core = f"第 {self.step_n} 步 · {tail}"
        budget = READOUT_MAX_CHARS - len(self.job_id) - len(core) - 2 * len(" · ")
        goal = self.goal if len(self.goal) <= budget else self.goal[:max(0, budget - 1)] + "…"
        return f"{self.job_id} · {goal} · {core}"[:READOUT_MAX_CHARS]

    def can_advance(self) -> bool:
        return self.state in ("open", "awaiting") and self.turns_total < MAX_TURNS_PER_JOB

    def next_step(self) -> Step | None:
        for st in self.steps:
            if st.n > self.step_n:
                return st
        return None
