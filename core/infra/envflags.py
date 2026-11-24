# -*- coding: utf-8 -*-
"""环境开关只读这一层：显式关才算关，未配置用调用方给的默认。

为什么收口：以前每个模块各抄一遍
    raw = (os.environ.get("X") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}
写法一样、语义散落——有的只认 "1"，有的 truthy（"0" 也算开），排查时看不出一处和另一处
到底是不是一回事。收口后一条规矩：

1. 未配置（变量不存在）→ 用调用方传的 `default`（默认 False），即"没配就是关"。
   配置成**空值**（裸旗标 `X=` / `X`）→ **True**：写了名字就是"要开"，只是没给值；
   读成 `default` 等于让"手滑写了个空值"静默变关（PIT-092 的老毛病）。
   读**值**的 `text()` 不同：空串仍回 `default`，因为空路径不是"开"。
2. 显式真词 `1/true/yes/on/y/t` → True；显式假词 `0/false/no/off/n/f` → False。
3. 认不出的词 → 用 `default`，并按开关名只告警一次（不静默吞掉）。
4. 只判有/无的开关走这里；读值（路径、房间列表、秒数）不要用 `flag()`。

要改某个功能的默认值，改它那一处 `flag("X", default=True)` 就行，可审查、可回滚。
"""
from __future__ import annotations

import os

from safelog import warn

_ON = frozenset({"1", "true", "yes", "on", "y", "t"})
_OFF = frozenset({"0", "false", "no", "off", "n", "f"})
_warned: set[str] = set()


def flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return True  # 裸旗标＝要开（见模块头 1.；读值看 text()）
    if value in _ON:
        return True
    if value in _OFF:
        return False
    if name not in _warned:
        _warned.add(name)
        warn("env_flag_unrecognized", name=name, value=value[:20], default=default)
    return default


def text(name: str, default: str = "") -> str:
    """读「有值」的环境变量（路径、名单、模型名），不是开关。

    与 `flag()` 同一套口径：未配置/空串 → default；别的原样返回（去首尾空白）。
    `flag()` 只回答是/否，别拿它读值；反过来也别拿 `text()` 判开关。
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    return value or default
