"""「在手的事」（job）——台账与推进器。

这个包回答的是图上的第 12 个实体（`t_job.png`，`(620, -300, 1000)`）：
**系统里唯一表达「进行中」的地方**。它不是第二个决策器——判仍然是唯一模型点，
`store` 只存「这件事到第几步、在等谁批」，`runner` 只按判给的步骤往下走。

分层纪律（`scripts/check_layering.py` 规则 11 看着）：
本包不许 import `core.speech` / `core.routing` / `agents` / `scheduler` / `web`；
要动手就调注入进来的可调用对象（`runner.advance(..., execute=...)`），
自己不认识「出口」和「工具包」。
"""

from __future__ import annotations

__all__ = ["store", "runner"]
