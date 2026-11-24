"""确定性抽样：同一个输入永远给同一个答案。

为什么不用 ``random``：主动触达、复盘抽样这类“要不要做”的判断，一旦用了未播种
的随机，同一个输入重放（回放日志、跑 benchmark、复现线上事故）永远得不到同一个
结论，问题就变成不可复现的。这里把“随机”收紧成“键控抽样”：随机性照旧（不同
key 分布均匀），但**给定 key 就确定**。

反例形状（PIT-044 之前）：
- ``if random.random() > keep: skip`` —— 日志里没有种子，事后再也还原不出那一次；
- ``random.Random(seed + abs(hash(name)) % 100000)`` —— 名字哈希未播种，换进程/
  换 PYTHONHASHSEED 就变一套结果，等于换了个随机。

纯函数、无状态、无 I/O。``random`` 只允许出现在 ``_rng`` 里（结构锁见
``tests/test_det_sampling.py``）。
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

__all__ = ["chance", "rng", "seed_of"]


def seed_of(*parts: Any) -> int:
    """把若干段业务键折成一个稳定的 64 位种子。

    用 sha256 而不是 ``hash()``：``hash()`` 对 str 的结果受 ``PYTHONHASHSEED``
    影响，跨进程会变；sha256 只跟字节有关。

    各段用 ``"\x1f"``（单元分隔符，正常业务键里不会出现）拼接，避免
    ``("ab", "c")`` 与 ``("a", "bc")`` 撞成同一个种子。
    """
    text = "\x1f".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _rng(*parts: Any) -> random.Random:
    """以 ``parts`` 为键的独立随机源（不碰全局 ``random`` 状态）。"""
    return random.Random(seed_of(*parts))


def rng(*parts: Any) -> random.Random:
    """需要一串随机数（而不是一次判定）时用它：同一个 key 给同一串。"""
    return _rng(*parts)


def chance(p: float, *parts: Any) -> bool:
    """以概率 ``p`` 返回 True，且给定 ``parts`` 时结果确定。

    ``p`` 越界不报错——调用方常传“压力 × 清醒度”这类算出来的浮点：
    ``p <= 0`` 恒 False，``p >= 1`` 恒 True。
    """
    if p <= 0.0:
        return False
    if p >= 1.0:
        return True
    return _rng(*parts).random() < p
