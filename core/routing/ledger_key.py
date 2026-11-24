"""台账公共外键（2026-10-23）：同一条消息在各种台账里必须能对上。

为什么不是"给某个消费者补个 id"：三条台账产生的时刻不同 —— 路由决策在回合建立
**之前**（判成不回就没有回合，也就没有 trace_id），感知在回合内，addressee 影子
读数按消息级跑（和建不建回合无关）。三者唯一共有的键是**消息身份 event_id**。

所以约定：每条台账都写 ``{kind, event_id, chat_id, at, trace_id?}``，
``trace_id`` 只在有回合时才有。读侧配对顺序固定：
event_id 精确匹配 → 退 (chat_id, 时间窗) → 认不出就不进分母。

这里只做"拼键 + 写行"，**不做任何判定**，且永不抛（台账不许影响业务）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from safelog import warn
from core.infra.envflags import text

LEDGER_PATH_ENV = "YINYUE_LEDGER_PATH"
_DEFAULT_NAME = "ledger.jsonl"


@dataclass(frozen=True)
class LedgerKey:
    event_id: str = ""
    chat_id: str = ""
    at: str = ""
    trace_id: str = ""

    def as_dict(self) -> dict[str, str]:
        out = {"event_id": self.event_id, "chat_id": self.chat_id, "at": self.at}
        if self.trace_id:
            out["trace_id"] = self.trace_id
        return out


def key_of(extra: Any, *, trace_id: str = "", at: str = "") -> LedgerKey:
    """从回合 extra 里拼出公共键（取不到就留空串，不猜）。"""
    # 既吃 context（有 .extra）也吃 extra 本身（dict）——调用方两种都会传。
    candidate = getattr(extra, "extra", extra)
    data = candidate if hasattr(candidate, "get") else {}
    return LedgerKey(
        event_id=str(data.get("parent_event_id") or data.get("event_id") or ""),
        chat_id=str(data.get("chat_id") or ""),
        at=at or datetime.now().isoformat(timespec="milliseconds"),
        trace_id=str(trace_id or ""),
    )


def default_path() -> Path:
    raw = text("YINYUE_LEDGER_PATH")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent.parent / "logs" / _DEFAULT_NAME


def emit(kind: str, key: LedgerKey, *, path: Path | None = None, **payload: Any) -> bool:
    """写一行台账（**永不抛**）。"""
    try:
        item: dict[str, Any] = {"kind": str(kind or "")}
        item.update(key.as_dict())
        item.update({k: v for k, v in payload.items() if v is not None})
        target = path or default_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001 - 审计不许影响业务
        warn("core/routing/ledger_key.py:72 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False