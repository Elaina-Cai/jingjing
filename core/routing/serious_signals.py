"""时间压力词表（唯一 owner）。

2026-10-23 主人定案「语义交回模型」：本模块原来还提供 ``serious_signals_for``
（把「提问 / 转达 / 承诺 / 撤回 / 有截止时间」这些**语义**判成信号，再当作
回合要不要回的否决票）。那一族已删——信号不该由词表决定。

留下的只有 ``URGENT_MARKERS``：它是**字面词表**，不是判断，
消费者是 ``core/routing/router.py::_is_urgent``（进审计 evidence）与
``core/routing/pipeline.py`` 的老词表口径。
"""

__all__ = ["URGENT_MARKERS"]

URGENT_MARKERS = frozenset({
    "紧急", "urgent", "ASAP", "马上", "立刻", "赶紧",
    "救命", "急", "尽快", "速回",
})
