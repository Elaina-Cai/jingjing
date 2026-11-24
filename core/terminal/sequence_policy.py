"""``terminal_events.sequence`` 的口径常量与纯函数（B6，2026-09-30）。

**为什么需要这份常量（存量事实，2026-09-29/30 实查）**

* 2026-09-21 21:59 ``scripts/cleanup_test_memory_events.py`` 删了 676 行测试事件
  （证据 ``backups/phase5_test_event_cleanup_20260921_215943/deleted_ids.json``）：
  删除后 88 个号被后续 append 重占，余下 **571 个号永久空缺**，正好三段
  ``129–491`` / ``717–898`` / ``1490–1515``；9 天来 **0 新缺口**。
* 同一次删除还留下 **8 个重号**：``2554``、``6083–6089``（幸存行与重占行同号）。
* 主人拍板（2026-09-30）：存量空缺 **不补不重排**，写成白名单；对账器 **只报不修**；
  ``terminal_events`` **不建** ``unique(sequence)``（存量重号会让唯一索引建不起来）。

所以白名单是「已知历史事实」而不是「允许继续制造空缺」：
:data:`SEQUENCE_GAP_WHITELIST` / :data:`SEQUENCE_DUPLICATE_WHITELIST` 之外的任何
缺口或重号都会被 :func:`core.terminal.reconciler.reconcile_sequences` 报出来
（只报告，不改数据）。发号本身已改为**库内原子取号**（见
``TerminalEventStore.append``），新事件不会再制造缺口或重号。

trace 追溯用 ``terminal_event:<event_id>``，**不要用 sequence**：它有重号与空缺。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# 存量永久空缺（闭区间号段，已排序、互不相交）。
SEQUENCE_GAP_WHITELIST: tuple[tuple[int, int], ...] = (
    (129, 491),
    (717, 898),
    (1490, 1515),
)
# 同上，但用来说明「为什么允许存在」。
SEQUENCE_GAP_WHITELIST_REASON = (
    "2026-09-21 21:59 cleanup_test_memory_events.py 删除 676 行测试事件留下的永久空缺；"
    "删除后 88 个号被重占、571 个号再也没被占用，9 天 0 新缺口（2026-09-30 实查）"
)

# 存量重号（同一个 sequence 出现两次的号）。不建 unique(sequence) 的原因就是它们。
SEQUENCE_DUPLICATE_WHITELIST: tuple[int, ...] = (
    2554,
    6083,
    6084,
    6085,
    6086,
    6087,
    6088,
    6089,
)
SEQUENCE_DUPLICATE_WHITELIST_REASON = (
    "同一次测试事件清理：被删除的号段后来被 append 重占，与幸存的 8 行同号；"
    "对账器只报告不修复（修复要重排号，等于改事实层）"
)


@dataclass(frozen=True)
class GapRange:
    """一段连续空缺号（闭区间）。"""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "count": self.length}


def is_whitelisted_gap(sequence: int) -> bool:
    """该号是否落在存量空白的白名单里。"""
    return any(start <= sequence <= end for start, end in SEQUENCE_GAP_WHITELIST)


def is_whitelisted_duplicate(sequence: int) -> bool:
    """该重号是否属于存量已知的 8 个。"""
    return sequence in SEQUENCE_DUPLICATE_WHITELIST


def find_gap_ranges(
    sequences: Iterable[int],
    *,
    lower_bound: int = 1,
    upper_bound: int | None = None,
) -> list[GapRange]:
    """``[lower_bound, max(sequences)]`` 内「应该有却没有」的号段。

    ``upper_bound`` 传 None 时取实际最大值（尾部空缺无从判断，不臆造）。
    取号保证 ≥1，小于 ``lower_bound`` 的取值不参与缺口计算（坏行由调用方单独计数）。
    """
    values = sorted({int(value) for value in sequences if int(value) >= lower_bound})
    if not values:
        return []
    top = int(upper_bound) if upper_bound is not None else values[-1]
    gaps: list[GapRange] = []
    cursor = int(lower_bound)
    for value in values:
        if value > top:
            break
        if value > cursor:
            gaps.append(GapRange(cursor, value - 1))
        cursor = max(cursor, value + 1)
    if top > cursor - 1:
        gaps.append(GapRange(cursor, top))
    return gaps


def subtract_ranges(
    ranges: Sequence[GapRange], covered: Sequence[tuple[int, int]]
) -> list[GapRange]:
    """从 ``ranges`` 里挖掉 ``covered``（白名单号段），返回残余号段。"""
    remaining: list[GapRange] = []
    for item in ranges:
        pieces = [item]
        for start, end in covered:
            nxt: list[GapRange] = []
            for piece in pieces:
                if end < piece.start or start > piece.end:
                    nxt.append(piece)
                    continue
                if piece.start < start:
                    nxt.append(GapRange(piece.start, start - 1))
                if piece.end > end:
                    nxt.append(GapRange(end + 1, piece.end))
            pieces = nxt
        remaining.extend(pieces)
    return remaining


def split_gap_ranges(
    gaps: Sequence[GapRange],
    whitelist: Sequence[tuple[int, int]] | None = None,
) -> tuple[list[GapRange], list[GapRange]]:
    """把缺口号段分成 ``(白名单外的新缺口, 白名单内的存量空缺)``。

    ``whitelist=None`` 时在**调用时**读模块常量 :data:`SEQUENCE_GAP_WHITELIST`：
    写成定义期默认参数（``whitelist=SEQUENCE_GAP_WHITELIST``）会把常量在 def 那一刻
    绑死，monkeypatch / 配置覆盖都不生效（测试只能测到常量本身）。
    """
    source = SEQUENCE_GAP_WHITELIST if whitelist is None else whitelist
    known = [(int(start), int(end)) for start, end in source]
    new_gaps = subtract_ranges(gaps, known)
    known_gaps = subtract_ranges(gaps, [(item.start, item.end) for item in new_gaps])
    return new_gaps, known_gaps


def count_numbers(ranges: Sequence[GapRange]) -> int:
    """号段里一共有多少个号。"""
    return sum(item.length for item in ranges)


def find_duplicates(sequences: Iterable[int]) -> dict[int, int]:
    """``sequence -> 出现次数``，只保留出现 >1 次的号（按号升序）。"""
    counts: dict[int, int] = {}
    for value in sequences:
        key = int(value)
        counts[key] = counts.get(key, 0) + 1
    return {key: counts[key] for key in sorted(counts) if counts[key] > 1}
