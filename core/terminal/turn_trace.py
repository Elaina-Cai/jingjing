"""E0 判决行留痕（``turn_trace``，U-01）。

每回合一行，append-only，**旁注不参与决策**。表由迁移 63 建（账号链），
``turn_id`` 是主键，所以"每个回合一行、重复写 no-op"是表约束保证的。

为什么是旁注，不是账本正行
------------------------
判决是她**自己的决定**，不是外部世界发生的事实；账本（``terminal_events``）只收
有外部来源的事件，把判决塞进去会污染"只追加、永不改旧行"的语义。旁注删掉可整表
重建，代价只有"这几天的读数没了"。

为什么每回合都写（含 silent / abstain）
------------------------------------
开口率 / 弃权率 / 沉默率的分母就是**全部判决行**；只写开口回合等于把分母丢掉，
三个率都会虚高。复查（她当时有没有看懂）才只在真开口的回合有意义——那是完整留痕
（E1），不是这张表。

两行制（E1，U-03）
------------------
- **判决行**：每回合一行，写库（``turn_trace`` 五列 + ``mode`` + ``ts``），供统计
  （开口率 / 弃权率 / 沉默率）与 ``turn_id`` 幂等。
- **完整留痕**：**只在 ``respond`` / ``ask``**（真开口或动手）时再写一份
  ``data/turn_trace/<YYYY-MM-DD>.jsonl``（append-only、一条一行、可人工复查与重放）。
  沉默回合**不写**——否则旁注会被空字段写爆，指标分母也会失真。
- 目录可用 ``YINYUE_TURN_TRACE_DIR`` 覆盖（测试与隔离跑用）。

三档语义（``TURN_TRACE``）
- ``off``    ：一行不写（默认）
- ``shadow`` ：写行、行上带 ``mode=shadow``，不参与决策、不进出口
- ``on``     ：写行并进验收报表

写失败**只 log**，不抛、不回滚、不阻断（与账本旁注同一纪律）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import text

from core import features
from safelog import warn

log = logging.getLogger(__name__)

#: 表名（迁移 63 与 ``db.meta_models.TurnTrace`` 必须同形）。
TABLE = "turn_trace"

#: 建表 DDL（迁移 63 与测试共用；``IF NOT EXISTS`` 保证幂等）。
CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS turn_trace ("
    "turn_id TEXT NOT NULL PRIMARY KEY,"
    "chat_id TEXT DEFAULT '' NOT NULL,"
    "decision TEXT DEFAULT 'silent' NOT NULL,"
    "basis TEXT DEFAULT '[]' NOT NULL,"
    "mode TEXT DEFAULT 'off' NOT NULL,"
    "ts DATETIME NOT NULL"
    ")"
)

CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_turn_trace_chat_ts ON turn_trace (chat_id, ts)"
)

_engine: Any = None


def set_engine(engine: Any) -> None:
    """测试/嵌入用：覆盖默认引擎（``None`` = 回到懒加载的 meta 库）。"""
    global _engine
    _engine = engine


def _get_engine() -> Any:
    global _engine
    if _engine is not None:
        return _engine
    from core.terminal.store_port import get_meta_repo

    _engine = get_meta_repo().engine
    return _engine


def _mode() -> str:
    """唯一读点：``TURN_TRACE`` 三态（默认 off）。"""
    return str(features.mode(features.TURN_TRACE, features.MODE_OFF) or features.MODE_OFF)


@dataclass(frozen=True)
class TurnTraceRow:
    """一行判决留痕（只读投影）。"""

    turn_id: str
    chat_id: str
    decision: str
    basis: tuple[str, ...]
    mode: str
    ts: datetime | None


def _full_dir() -> Path:
    """完整留痕目录（默认 ``<repo>/data/turn_trace``；env 可覆盖）。"""
    override = text("YINYUE_TURN_TRACE_DIR").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "data" / "turn_trace"


def _append_full(
    *,
    turn_id: str,
    chat_id: str,
    decision: str,
    basis: Sequence[str],
    mode: str,
    ts: datetime,
    detail: dict[str, Any] | None,
) -> bool:
    """追加一条完整留痕（JSONL）；失败只 log，绝不阻断主链。"""
    try:
        directory = _full_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{ts.strftime('%Y-%m-%d')}.jsonl"
        record = {
            "turn_id": turn_id,
            "chat_id": chat_id,
            "decision": decision,
            "basis": [str(x) for x in basis],
            "mode": mode,
            "ts": ts.isoformat(timespec="seconds"),
            "detail": normalize_full_detail(**dict(detail or {})),
        }
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 - 旁注失败绝不阻断主链
        log.info(
            "turn_trace_full_failed",
            error=type(exc).__name__,
            turn_id=turn_id,
        )
        return False


#: 完整留痕的字段清单（``判投记_契约.md`` §8.1 那一段表格）。
#:
#: 为什么要有这份清单：完整的留痕**不是想写什么就写什么**——六个字段各自是一个
#: 量具的分母（``trimmed`` 算无依据断言率、``tools`` 算该翻没翻、``memory`` 算写回
#: 审计、``reading`` 算读懂率）。字段少了，率就算不出来，而且没人会发现。
FULL_TRACE_FIELDS: tuple[str, ...] = (
    "reading",
    "trimmed",
    "tools",
    "memory",
    "speech_ref",
    "speech_excerpt",
)

#: 摘录上限（契约 §8.1：``speech_excerpt`` 可选、≤200 字）
SPEECH_EXCERPT_MAX = 200

#: 摘录必须带的标记：文本只在账本，这里是副本
EXCERPT_MARK = "（以账本为准）"


def normalize_full_detail(
    *,
    reading: str = "",
    trimmed: Sequence[str] | None = None,
    tools: Sequence[Any] | None = None,
    memory: Sequence[Any] | None = None,
    speech_ref: str = "",
    speech_excerpt: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """把完整留痕归一成六个字段（缺的补空形状，不消失）。

    ``speech_excerpt`` 是**审计副本**：≤200 字、末尾必须标「（以账本为准）」。
    真身是账本里 ``sequence == speech_ref`` 的那一行——所以 ``speech_ref`` 才是
    事实，摘录只是给人快速对照用的。
    """
    excerpt = str(speech_excerpt or "").strip()
    if len(excerpt) > SPEECH_EXCERPT_MAX:
        excerpt = excerpt[:SPEECH_EXCERPT_MAX].rstrip()
    if excerpt and EXCERPT_MARK not in excerpt:
        excerpt = f"{excerpt}{EXCERPT_MARK}"
    out: dict[str, Any] = {
        "reading": str(reading or ""),
        "trimmed": [str(x) for x in (trimmed or ())],
        "tools": [dict(x) if isinstance(x, Mapping) else x for x in (tools or ())],
        "memory": [dict(x) if isinstance(x, Mapping) else x for x in (memory or ())],
        "speech_ref": str(speech_ref or ""),
        "speech_excerpt": excerpt,
    }
    out.update(extra)
    return out


#: 允许写完整留痕的判决（"开口或动手"）。
FULL_DECISIONS: frozenset[str] = frozenset({"respond", "ask"})


def record_full_detail(
    *,
    turn_id: str,
    chat_id: str = "",
    decision: str = "tool_loop",
    basis: Sequence[str] | None = None,
    detail: dict[str, Any] | None = None,
    ts: datetime | None = None,
) -> bool:
    """只追加一条完整留痕，不写判决行。

    为什么需要这个入口：工具调用不是一次「开口」，它不该多占一行判决行
    （判决行每回合一行是量具的分母，见 ``判投记_契约.md`` §8.1）——但它的
    结果必须进完整留痕的 ``tools`` 字段，否则「该查没查」永远算不出来。
    """
    eff = str(_mode() or features.MODE_OFF)
    if eff == features.MODE_OFF:
        return False
    tid = str(turn_id or "").strip()
    if not tid:
        return False
    return _append_full(
        turn_id=tid,
        chat_id=str(chat_id or ""),
        decision=str(decision or "tool_loop"),
        basis=tuple(str(x) for x in (basis or ())),
        mode=eff,
        ts=ts or datetime.now(),
        detail=dict(detail or {}),
    )


def record_turn_trace(
    *,
    turn_id: str,
    chat_id: str,
    decision: str,
    basis: Sequence[str] | None = None,
    ts: datetime | None = None,
    mode: str | None = None,
    full: bool = False,
    detail: dict[str, Any] | None = None,
) -> bool:
    """写判决行；``full=True`` 且判决是开口/动手时，再追加一条完整留痕。"""
    eff = str(mode or _mode() or features.MODE_OFF)
    if eff == features.MODE_OFF:
        return False
    tid = str(turn_id or "").strip()
    if not tid:
        log.info("turn_trace_skipped", reason="empty_turn_id", chat_id=str(chat_id or ""))
        return False
    payload = json.dumps([str(x) for x in (basis or ())], ensure_ascii=False)
    when = ts or datetime.now()
    try:
        engine = _get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO turn_trace (turn_id, chat_id, decision, basis, mode, ts) "
                    "VALUES (:tid, :cid, :dec, :basis, :mode, :ts)"
                ),
                {
                    "tid": tid,
                    "cid": str(chat_id or ""),
                    "dec": str(decision or "silent"),
                    "basis": payload,
                    "mode": eff,
                    "ts": when,
                },
            )
    except Exception as exc:  # noqa: BLE001 - 旁注失败绝不阻断主链
        warn("core/terminal/turn_trace.py:278 Exception", err=repr(exc))
        name = type(exc).__name__
        if "Integrity" in name or "UNIQUE" in str(exc).upper():
            log.debug("turn_trace_duplicate", turn_id=tid)
        else:
            log.info("turn_trace_write_failed", turn_id=tid, error=str(exc)[:160])
        return False

    if full and str(decision or "silent") in FULL_DECISIONS:
        _append_full(
            turn_id=tid,
            chat_id=str(chat_id or ""),
            decision=str(decision or "silent"),
            basis=tuple(str(x) for x in (basis or ())),
            mode=eff,
            ts=when,
            detail=detail,
        )
    elif full:
        log.info(
            "turn_trace_full_skipped",
            reason="not_speaking",
            decision=str(decision or "silent"),
            turn_id=tid,
        )

    return True


def iter_turn_trace(
    *,
    since: datetime,
    until: datetime | None = None,
) -> list[TurnTraceRow]:
    """按时间窗读判决行（报表/检查器用；读失败返回空表）。"""
    sql = "SELECT turn_id, chat_id, decision, basis, mode, ts FROM turn_trace WHERE ts >= :since"
    params: dict[str, Any] = {"since": since}
    if until is not None:
        sql += " AND ts < :until"
        params["until"] = until
    sql += " ORDER BY ts ASC, turn_id ASC"
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.info("turn_trace_read_failed", error=str(exc)[:160])
        return []
    out: list[TurnTraceRow] = []
    for r in rows:
        try:
            basis = tuple(json.loads(r[3] or "[]"))
        except Exception:  # noqa: BLE001
            warn("core/terminal/turn_trace.py:330 Exception", err=repr(__import__("sys").exc_info()[1]))
            basis = ()
        out.append(TurnTraceRow(str(r[0]), str(r[1]), str(r[2]), basis, str(r[4]), r[5]))
    return out
