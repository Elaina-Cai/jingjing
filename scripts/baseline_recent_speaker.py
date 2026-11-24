"""评测用基线探针（**非生产代码**）：锚点 = 窗口内最近一条消息的说话人。

用途
----
复现「最近说话人」策略的下限分数，作为 `core/context/threads.py` 实现后的对照基线。
它刻意**不做**任何结构判定——正是 2026-09-30 21:18 串线事故的成因，用它当"最差可接受线"。

纪律
----
- 只能由 `scripts/score_threads_eval.py --predictor baseline_recent_speaker:predict` 加载；
- `core/**` 与 `agents/**` **不得** import 本模块（它不含归属正则，但语义上属"近似判定"）；
- 预测器契约：`predict(sample, hist) -> dict`，`hist` 为同会话窗口内更早的样本（含 at/speaker/text/refs）。
"""
from __future__ import annotations

from typing import Any


def predict(sample: dict, hist: list[dict]) -> dict[str, Any]:
    """锚点 = 最近说话人；无历史则判为 unresolved（不猜）。"""
    if not hist:
        return {
            "anchor": None,
            "thread_id": None,
            "addressee": None,
            "should_switch": False,
            "confidence": "unresolved",
        }
    last = hist[-1]
    return {
        "anchor": last.get("speaker"),
        "thread_id": None,
        "addressee": None,
        "should_switch": False,
        "confidence": "exact",
    }
