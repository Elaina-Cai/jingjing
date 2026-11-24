"""判的输入第 6 样：**在手的事读数**（`docs/decisions/判投记_契约.md` §1）。

三档开关就是三行字
------------------
- `JOB_LEDGER=off`（默认）：**连台账都不碰**，返回空串。既有提示词逐字不变。
- `shadow`：去台账数一下有几件在办（记 `job_readout_shadow` 日志），但**一个字符都不进 prompt**。
- `on`：`JobStore.readouts(chat_id)` 取 ≤3 条、每条 ≤60 字，拼成 `[在手的事]` 块。

为什么读数单独成型而不塞进 `extra` 的别的块：它是**判的第 6 样输入**，与
「世界面 / 自我 / 记忆 / 工具 / 限流」并列；混进哪一块都会让「这回合判看见了什么」
没法追。渲染出来只有两种形状——空串，或块头 + 1~3 行 `- job_id · 目标 · 第 n 步 · 在等什么`。

纪律：**任何异常都降级成空串**。读数丢一轮，判只是不知道有件事在手，不会说错话；
而抛出去会让整个回合拼不出提示词。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from core import features
from core.contracts.plan import READOUT_MAX_CHARS, READOUT_MAX_ITEMS
from safelog import warn

log = logging.getLogger(__name__)

#: 块头。改字必须同步 `docs/decisions/判投记_契约.md` §1 与量具。
JOB_READOUT_HEADER = "[在手的事]"
#: 刚办完的事在读数里留多久 / 最多几条
DONE_WINDOW_S = 300.0
DONE_MAX_ITEMS = 1
DONE_PREFIX = "[刚办完] "
# 失败必须进她的下一轮上下文：真机 2026-10-21「发不了」循环的成因之一是
# `job_advance stop=failed` 只写进台账、没进 prompt——她不知道自己失败过，
# 所以既不改口也不换方法。
FAIL_PREFIX = "[最近没成] "
FAIL_MAX_ITEMS = 1
FAIL_WINDOW_HOURS = 72


def _clip_goal(text: Any, limit: int = 40) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "\u2026"


__all__ = [
    "JOB_READOUT_HEADER",
    "build_job_readout_block",
    "collect_job_readouts",
    "job_readout_mode",
    "render_job_readouts",
]


def job_readout_mode() -> str:
    """开关的三态读法（唯一入口；`tests/test_flag_reader_discipline.py` 看着）。"""
    try:
        return str(features.mode(features.JOB_LEDGER, features.MODE_OFF) or features.MODE_OFF)
    except Exception:  # noqa: BLE001 - 读不到开关就当 off
        warn("core/context/job_readout.py:59 Exception", err=repr(__import__("sys").exc_info()[1]))
        return features.MODE_OFF


def render_job_readouts(readouts: Sequence[str]) -> str:
    """读数 → 提示词块。空列表 → 空串（一个字符都不加）。

    `[最近没成]` 那一行**不被 3 条上限挤掉**：它决定她改不改口；而在等批准的
    条数本来就有上限（`MAX_JOBS_PER_CHAT = 3`）。
    """
    cleaned = [" ".join(str(raw or "").split()) for raw in readouts]
    cleaned = [text for text in cleaned if text]
    if not cleaned:
        return ""
    fails = [text for text in cleaned if text.startswith(FAIL_PREFIX)]
    others = [text for text in cleaned if not text.startswith(FAIL_PREFIX)]
    room = READOUT_MAX_ITEMS - (1 if fails else 0)
    picked = others[:room] + fails[:1]
    return JOB_READOUT_HEADER + "\n" + "\n".join(
        "- " + text[:READOUT_MAX_CHARS] for text in picked
    )


def collect_job_readouts(extra: Any, *, store: Any = None) -> list[str]:
    """把「有哪些事在手」取出来，写进 `extra["job_readouts"]` 并返回。

    `shadow` 档只写 `extra["job_readout_shadow"] = {"open": n}`（够跑读数了），
    `off` 档什么都不写——`extra` 里多出来的键也是行为变化，不该在默认档出现。
    """
    mode = job_readout_mode()
    if mode == features.MODE_OFF or not hasattr(extra, "get"):
        return []
    chat_id = str(extra.get("chat_id") or "")
    if not chat_id:
        return []
    try:
        if store is None:
            from core.jobs.store import JobStore

            store = JobStore()
        if mode == features.MODE_SHADOW:
            open_jobs = store.open_jobs(chat_id)
            extra["job_readout_shadow"] = {"open": len(open_jobs)}
            log.info("job_readout_shadow chat_id=%s open=%d", chat_id, len(open_jobs))
            return []
        reads = list(store.readouts(chat_id))
        # 刚办完的事也要念一句：事一 done 就离开 open_jobs，不说她做成过什么，
        # 她下一句就会否认自己有能力做（真机 2026-10-21：「这工具发不了群消息」）。
        try:
            for row in store.recent_done(chat_id, window_s=DONE_WINDOW_S, limit=DONE_MAX_ITEMS):
                read = DONE_PREFIX + _clip_goal(row.get("goal") or "")
                server_id = str(row.get("server_id") or "")
                if server_id:
                    read += "（已到，server_id=" + server_id[:12] + "）"
                reads.append(read)
        except Exception:  # noqa: BLE001 - 没有就没有
            warn("core/context/job_readout.py:114 Exception", err=repr(__import__("sys").exc_info()[1]))
        try:
            for row in store.recent_failures(
                chat_id, limit=FAIL_MAX_ITEMS, within_hours=FAIL_WINDOW_HOURS
            ):
                detail = str(row.get("detail") or "").strip()
                if detail:
                    reads.append(FAIL_PREFIX + detail[:READOUT_MAX_CHARS])
        except Exception:  # noqa: BLE001 - 没有就没有
            warn("core/context/job_readout.py:123 Exception", err=repr(__import__("sys").exc_info()[1]))
    except Exception as exc:  # noqa: BLE001 - 读数不许弄坏回合
        log.warning("job_readout_failed error=%s", str(exc)[:160])
        return []
    try:
        extra["job_readouts"] = reads
    except Exception:  # noqa: BLE001 - extra 不是 dict 就算了
        warn("core/context/job_readout.py:130 Exception", err=repr(__import__("sys").exc_info()[1]))
    return reads


def build_job_readout_block(extra: Any) -> str:
    """从 `extra` 取读数渲染成块（不查库；查库在 `collect_job_readouts`）。"""
    if not hasattr(extra, "get"):
        return ""
    reads = extra.get("job_readouts") or ()
    if not reads:
        return ""
    return render_job_readouts(reads)
