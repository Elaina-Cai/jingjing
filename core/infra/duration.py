# -*- coding: utf-8 -*-
"""时长与时刻的唯一单位：**毫秒（int）**。

为什么要有这个文件（UI-066）
----------------------------
2026-10-28 之前，`90.0` / `0.4` / `300.0` / `120.0` 这些秒数散在 loop/run.py、
scheduler/main.py、loop/ledger.py、监管器里，各自 `* 1000` 或 `/ 1000` 换算。
后果不是"不好看"，而是三类真事故：
  1. 90 秒被渲染成"2 分钟"（每个控制台各自 round）；
  2. 发车间隔两处各自算（PIT-111，同一个洞）；
  3. 判死阈值写死 120 秒，节拍一改就与现实脱钩（第 1 项/PIT-155）。
`_S` / `_MS` 命名约定约束不住任何人——命名不可执行。所以规矩落到代码上：

本文件的规矩
------------
1. **存储与传递只用毫秒整数**：`at_ms` / `beat_ms` / `interval_ms` / `timeout_ms`。
2. `to_ms()` 是**唯一入口**：只接受 `int`（就是毫秒）或带单位的字符串 `"90s"`。
   传 float 直接抛错——浮点字面量本身就意味着"单位不明"，不给它入口就是设计。
3. `human()` 是**唯一渲染出口**：面板 / 日志 / 给她看的话术都从这里出。
4. `sleep_s()` 是**唯一**给 `time.sleep` / `asyncio.sleep` 的出口（它们只吃秒）。
5. 例外只有一处：`time.time()` 给的是 epoch **秒**（标准库定死），统一走
   `now_ms()` 换算；别处不许再乘 1000（`scripts/check.py` 的 `unit` 规则盯着）。
"""

from __future__ import annotations

import re
import time

# 宽限倍数：允许连续几次心跳丢失再判死。量词只许写在这里。
GRACE = 3.0

_UNIT_MS = {"ms": 1, "s": 1000, "m": 60 * 1000, "h": 60 * 60 * 1000}
_PAT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)\s*$")


def to_ms(value: object, *, where: str = "") -> int:
    """把"时长"变成毫秒整数。int 原样收，字符串必须带单位，float 一律拒绝。"""
    tail = ("（%s）" % where) if where else ""
    if isinstance(value, bool):
        raise TypeError("时长不要用 bool：收到 %r%s" % (value, tail))
    if isinstance(value, int):
        if value < 0:
            raise ValueError("时长不能是负数：%r%s" % (value, tail))
        return int(value)
    if isinstance(value, float):
        raise TypeError(
            "时长不要用浮点秒（单位不明）：收到 %r%s。写成 '90s' 或 90000（毫秒）"
            % (value, tail))
    if isinstance(value, str):
        hit = _PAT.match(value.strip().lower())
        if not hit:
            raise ValueError("时长字符串必须带单位（ms/s/m/h）：%r%s" % (value, tail))
        return int(round(float(hit.group(1)) * _UNIT_MS[hit.group(2)]))
    raise TypeError("不认识的时长类型 %s：%r%s" % (type(value).__name__, value, tail))


def human(ms: int) -> str:
    """毫秒 → 人话。**这是唯一允许做单位换算的渲染出口。**"""
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return "未知"
    sign = "-" if ms < 0 else ""
    ms = abs(ms)
    if ms < 1000:
        return "%s%d 毫秒" % (sign, ms)
    if ms < 60 * 1000:
        if ms % 1000:
            return "%s%.1f 秒" % (sign, ms / 1000.0)
        return "%s%d 秒" % (sign, ms // 1000)
    if ms < 60 * 60 * 1000:
        body = "%d 分" % (ms // (60 * 1000))
        sec = (ms % (60 * 1000)) // 1000
        return sign + (body if not sec else "%s %d 秒" % (body, sec))
    if ms < 24 * 60 * 60 * 1000:
        body = "%d 小时" % (ms // (60 * 60 * 1000))
        minute = (ms % (60 * 60 * 1000)) // (60 * 1000)
        return sign + (body if not minute else "%s %d 分" % (body, minute))
    body = "%d 天" % (ms // (24 * 60 * 60 * 1000))
    hour = (ms % (24 * 60 * 60 * 1000)) // (60 * 60 * 1000)
    return sign + (body if not hour else "%s %d 小时" % (body, hour))


def now_ms() -> int:
    """此刻（epoch 毫秒）。`time.time()` 的秒只在这一次被换算。"""
    return time.time_ns() // 1000000


def sleep_s(ms: int) -> float:
    """给 `time.sleep` / `asyncio.sleep` 的秒数。单位换算只许发生在这里。"""
    return int(ms) / 1000.0


def deadline_ms(at_ms: int, beat_ms: int, grace: float = GRACE) -> int:
    """判死的唯一公式：最后一次心跳 + 承诺节拍 × 宽限。"""
    return int(at_ms) + int(int(beat_ms) * grace)


def epoch_s(ms: int) -> float:
    """毫秒 → epoch 秒。**只给只吃秒的标准库用**（`datetime.fromtimestamp`、
    `%0.1f` 这类日志格式）；业务判断不许用这个函数。"""
    return int(ms) / 1000.0


def from_epoch_s(seconds: float) -> int:
    """epoch 秒 → 毫秒。**只用于接收边界**（标准库/旧数据给的秒、DB 里的秒级时间戳）。
    业务判断里不许出现它——出现了就说明你把一个秒级时刻带进了毫秒世界。"""
    try:
        return int(round(float(seconds) * 1000))
    except (TypeError, ValueError):
        return 0


def human_s(seconds: float) -> str:
    """浮点秒（来自标准库 / 旧数据）→ 人话。**先换算成毫秒，再交给 human()**，
    所以格式化规则仍然只有一份。"""
    return human(from_epoch_s(seconds))
