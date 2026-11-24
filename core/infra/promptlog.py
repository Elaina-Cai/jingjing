"""把「这一拍真正发出去的请求」原样落盘（提示词形状改造施工 · Step 2）。

为什么记录点在这里而不是 `loop/brain.py`：brain 只知道自己**打算**发什么，
真正发给模型的字节是 llm 客户端组装出来的。要回答「她这一拍到底看到了什么」，
证据必须来自发请求的那一行。

落盘：`data/prompt/<turn_id>/request.json`
    {"turn_id": "...", "rounds": [{"i": 1, "at": "...", "provider": "...", "model": "...",
                                   "params": {...}, "messages": [...], "tools": [...], "note": ""}]}

约定（三条，别再改）：
1. 没有 turn（未 `set_turn`，例如单测、验证脚本）就不写任何文件——静默返回 None 不是丢数据，
   是「这一拍不属于任何 turn」。
2. `tools` 为空也要记一条，并在 `note` 里写 `no_tools`——「看不见即没有」是这个仓库的老病。
3. 写失败不抛：落一条 `promptlog_write_failed` 事件后返回 None。观测坏了不许把循环带崩。
"""
from __future__ import annotations

import contextvars
import json
import time
from pathlib import Path
from typing import Any

from core.infra import jsonio
from core.infra.envflags import text

try:  # 根目录的 safelog；导入失败也要能活（观测模块不许成为启动依赖）
    from safelog import warn as _warn
except Exception:  # pragma: no cover
    def _warn(event: str, **fields: Any) -> None:
        import logging
        logging.getLogger(__name__).warning("%s %s", event, fields)

_ROOT = Path(__file__).resolve().parents[2]
_TURN: contextvars.ContextVar[str] = contextvars.ContextVar("promptlog_turn", default="")


def root() -> Path:
    """落盘根目录。`YINYUE_PROMPT_DIR` 优先（测试与临时实验用）。"""
    got = text("YINYUE_PROMPT_DIR")
    return Path(got) if got else _ROOT / "data" / "prompt"


def set_turn(turn_id: str) -> None:
    _TURN.set(str(turn_id or ""))


def turn() -> str:
    return _TURN.get()


def path(turn_id: str) -> Path:
    return root() / str(turn_id) / "request.json"


def _write(dst: Path, payload: dict) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 写口只有一个：core/infra/jsonio.py。自己拼临时名会被 test_jsonio_atomic 当场拦下，
    # 而且固定临时名在并发下就是「两段交织的内容」。
    if not jsonio.atomic_write_json(dst, payload, indent=1):
        raise OSError("atomic_write_json 返回 False：%s" % dst)


def record(*, provider: str = "", model: str = "", params: dict | None = None,
           messages: list | None = None, tools: list | None = None,
           note: str = "") -> str | None:
    """记一轮请求。返回落盘路径（字符串）；没有 turn 或写失败返回 None。"""
    tid = turn()
    if not tid:
        return None
    try:
        dst = path(tid)
        cur = json.loads(dst.read_text(encoding="utf-8")) if dst.exists() else {"turn_id": tid, "rounds": []}
        rounds = list(cur.get("rounds") or [])
        msgs = list(messages or [])
        tls = list(tools or [])
        rounds.append({
            "i": len(rounds) + 1,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "provider": provider or "",
            "model": model or "",
            "params": dict(params or {}),
            "messages": msgs,
            "tools": tls,
            "note": note or ("no_tools" if not tls else ""),
        })
        cur["rounds"] = rounds
        _write(dst, cur)
        return str(dst)
    except Exception as exc:  # noqa: BLE001 - 观测坏了不许把循环带崩
        _warn("promptlog_write_failed", error=repr(exc)[:120])
        return None


def parts_path(turn_id: str) -> Path:
    """这一拍 system / user 各段的名字与字数（施工 Step 9 前置：形状要能量出来）。"""
    return root() / str(turn_id) / "parts.json"


def record_parts(parts: list, *, note: str = "") -> str | None:
    """记 parts：名字 → 字数，外加顺序。同一个 turn 重跑只留最后一次。"""
    tid = turn()
    if not tid:
        return None
    try:
        rows = []
        for i, pair in enumerate(parts or [], 1):
            try:
                name, body = pair[0], pair[1]
            except Exception as exc:  # noqa: BLE001 - 一段坏了不拖累其余段
                _warn("promptlog_part_bad", error=repr(exc)[:80])
                continue
            rows.append({"i": i, "name": str(name), "chars": len(str(body or ""))})
        payload = {
            "turn_id": tid,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total": sum(r["chars"] for r in rows),
            "note": note or "",
            "order": [r["name"] for r in rows],
            "by_name": {r["name"]: r["chars"] for r in rows},
            "parts": rows,
        }
        dst = parts_path(tid)
        _write(dst, payload)
        return str(dst)
    except Exception as exc:  # noqa: BLE001 - 观测坏了不许把循环带崩
        _warn("promptlog_parts_failed", error=repr(exc)[:120])
        return None


def load(turn_id: str) -> dict:
    dst = path(turn_id)
    if not dst.exists():
        return {}
    try:
        return json.loads(dst.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _warn("promptlog_read_failed", turn=str(turn_id)[:32], error=repr(exc)[:120])
        return {}


def turns() -> list[str]:
    """最近的 turn（按目录 mtime 倒序），给 `prompt_show latest` 用。"""
    try:
        kids = [p for p in root().iterdir() if (p / "request.json").exists()]
    except Exception:  # noqa: BLE001
        return []
    kids.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in kids]
