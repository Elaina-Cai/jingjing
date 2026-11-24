# -*- coding: utf-8 -*-
"""噪音闸门：只剩一条机械判据 —— 同一个人的完全相同文本，窗口内不重复发起。

历史：这里曾有六层启发式（模板前缀 [`[自动]` 等]、bigram 近重复 0.8、拍人频率
4 次、消息速率 15 条/分、被标 3 次转静默、疲劳预算「答了 5 轮 + 半数被标」）。
它们全都在替模型回答「这一条算不算刷屏」——那是语义判断，2026-10-23 删除。

留下的判据不需要读内容就能验：同一个人、同一段文字、十分钟内已经处理过一次，
再处理一次不可能得到不同的结果。新内容永远放行（恢复立即生效）。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from safelog import warn
from core.infra.envflags import text
from core.infra import tempo

__all__ = [
    "NoiseVerdict",
    "clear_noise_state",
    "in_noise_scope",
    "noise_gate_mode",
    "noise_verdict",
    "noise_scope",
    "record_skipped",
]

#: 完全相同文本的去重窗口。十分钟内同一人复读同一句，没有新信息。
REPEAT_WINDOW_SECONDS = tempo.s("noise.repeat")  # 数值在 core/infra/tempo.py
#: 太短的文本（「？」「哈」）不进判据：它们本来就会被反复说，且单字撞车概率高。
REPEAT_MIN_CHARS = 4
#: 状态表上限，超了整体清空（内存有界，不参与判断）。
MAX_STATES = 512

_PUNCT_RE = re.compile(r"[\s,.;:!?;:()\[\]<>-]+")


@dataclass(frozen=True)
class NoiseVerdict:
    """一条消息在闸门处的结论：skip=True 表示不发起回合并落账。"""

    skip: bool
    reason: str
    layer: str = ""
    state: str = "normal"

    def as_audit(self) -> dict[str, Any]:
        return {"skip": self.skip, "reason": self.reason, "layer": self.layer, "state": self.state}


_LOCK = threading.Lock()
#: key=(chat_id, sender_id) -> [(ts, norm)]，只保留窗口内的记录。
_STATES: dict = {}


def noise_gate_mode() -> str:
    """NOISE_GATE 三态：off / shadow / on；默认 off。"""
    try:
        from core import features

        return features.mode(features.NOISE_GATE, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到按 off，不阻断
        warn("core/context/noise_gate.py:63 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


def noise_scope() -> tuple:
    """YINYUE_NOISE_GATE_CHATS 逗号分隔的作用域；空 = 不限制。"""
    import os

    raw = text("YINYUE_NOISE_GATE_CHATS")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def in_noise_scope(chat_id: str) -> bool:
    scope = noise_scope()
    return (not scope) or (str(chat_id or "") in scope)


def _norm(text: Any) -> str:
    return _PUNCT_RE.sub("", str(text or "")).casefold()


def _texts(chat_id: str, sender_id: str) -> list:
    key = (str(chat_id or ""), str(sender_id or ""))
    rows = _STATES.get(key)
    if rows is None:
        if len(_STATES) >= MAX_STATES:
            _STATES.clear()
        rows = []
        _STATES[key] = rows
    return rows


def clear_noise_state() -> None:
    """测试/回滚用：清空全部内存态。"""
    with _LOCK:
        _STATES.clear()


def noise_verdict(
    *,
    chat_id: str,
    sender_id: str,
    sender_name: str = "",
    content: str = "",
    is_owner: bool = False,
    now: float | None = None,
) -> NoiseVerdict:
    """对一条入站消息做零成本判定：skip=True 表示这一轮不发起（只认完全相同文本）。"""
    if noise_gate_mode() == "off":
        return NoiseVerdict(False, "off")
    sid = str(sender_id or "")
    if not sid:
        return NoiseVerdict(False, "no_sender")
    if is_owner:
        return NoiseVerdict(False, "owner")
    ts = time.time() if now is None else float(now)
    norm = _norm(content)
    if len(norm) < REPEAT_MIN_CHARS:
        return NoiseVerdict(False, "short")
    with _LOCK:
        rows = _texts(chat_id, sid)
        rows[:] = [(t, v) for t, v in rows if ts - t <= REPEAT_WINDOW_SECONDS]
        repeat = any(v == norm for _, v in rows)
        rows.append((ts, norm))
    if repeat:
        return NoiseVerdict(True, "repeat", layer="message")
    return NoiseVerdict(False, "fresh")


#: L1 确定性记账：同一人同文本只写一次（避免复读写一堆重复事件）。
_SKIPPED_LAST: dict = {}
_SKIPPED_LAST_MAX = 512


def record_skipped(
    model: Any,
    *,
    chat_id: str = "",
    sender_id: str = "",
    sender_name: str = "",
    content: str = "",
    reason: str = "",
    at: str = "",
) -> bool:
    """把被跳过的消息确定性落进②（与正常路径同一个 writer/事件种类；零 token）。

    回看等价（L1）：复读原文本来就在终端库/窗口；这里补的是 N1 会写的
    ``message_observed`` 那一层。同一人同一文本只写一次；写失败返回 False，绝不抛。
    """
    try:
        norm = _norm(content)
        key = (str(chat_id or ""), str(sender_id or ""))
        if norm and _SKIPPED_LAST.get(key) == norm:
            return False
        if len(_SKIPPED_LAST) > _SKIPPED_LAST_MAX:
            _SKIPPED_LAST.clear()
        _SKIPPED_LAST[key] = norm

        from core.context.situation import observe_message

        observe_message(
            model,
            who=str(sender_name or ""),
            targets=(),
            text=str(content or ""),
            genre="复读" if str(reason or "") == "repeat" else "其他",
            topic="",
            at=str(at or ""),
        )
        return True
    except Exception:  # noqa: BLE001 - 记账失败绝不能拖垮回合
        warn("core/context/noise_gate.py:173 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False