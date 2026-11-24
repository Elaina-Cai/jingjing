"""额度分级（PIT-040）：用到 80% 提前说一声，到 100% 拦，两条运行时路径共用这一份。

为什么单独一层
--------------
额度语义在 ``core/rules/engine.py`` 的 ``Quota``，但运行时真正在走的路有两条：

* ``YINYUE_FEATURE_RULES=on``：``RuleEngine.quota_blocked`` 判 ``quota`` 维；
* 默认 ``off``：``core/terminal/proactive_service.py`` 里那串 if 链判同一件事。

阈值（80%）与「每天只说一次」写在任何一个调用点里都会各长一份、各自漂移，所以
**分级与喊话只有这里一处**，调用点只交事实：这个窗口用了几次、上限几次。

三条纪律（照 ``core/observability/startup.py``）
----------------------------------------------
1. **一天只说一次**：靠 ``data/.quota_warn.json`` 里的日期 + 已说过的组合去重；
   同一天重启十次也只说一遍，换天自动清空（文件不随天数长大）。
2. **绝不影响判定**：落盘动作包在 ``try/except`` 里，喊不出来照样返回等级；
   进程内另有一份内存台账，防止落盘失败时同一进程刷屏。
3. **只说不改**：这里不记账、不拒绝——拦不拦仍由调用点按 ``blocked`` 自己决定，
   判定权柄属于 ``core/rules/engine.py``。

反例（违规形状，``docs/开发规矩.md`` §2.11）：在 engine 里写
``used >= limit * 0.8``，又在 ``proactive_service.py`` 里再写一遍——两处阈值迟早不同，
出现第三个调用点时也没人知道还有第二处。
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Callable

import structlog

from core.infra.jsonio import atomic_write_json, read_json

log = structlog.get_logger()

__all__ = [
    "MARKER",
    "WARN_RATIO",
    "classify",
    "observe",
    "remaining",
    "text",
    "warn_at",
]

#: 用到上限的这个比例就提前说一声。**全库只有这一处**（PIT-040 的 80%）。
WARN_RATIO = 0.8

#: 当天已经说过的组合（跨天自动失效；文件不增长）。
MARKER = Path("data") / ".quota_warn.json"

#: 进程内台账：磁盘写失败 / 被占用时的兜底，避免同一进程刷屏。
#: 按「状态文件 + 日期」分桶——不同 marker 文件是不同世界（测试各用各的 tmp），
#: 混在一个桶里会互相把对方刚说过的组合吃掉。
_MEM: dict[tuple[str, str], set[str]] = {}

_WINDOW_NAME = {"hour": "每小时", "day": "每天"}


def warn_at(limit: int) -> int:
    """跨过这个「已用次数」就该提前说一声；``limit <= 0``（不限）返回 0。

    ``floor`` 取整：``limit=3`` 时 warn_at 是 2（不是 3）——上限越小越要提前说，
    否则 3 次里那一档永远落在 ``blocked`` 上，等于没有提醒。
    """
    if limit <= 0:
        return 0
    return max(1, min(limit, math.floor(limit * WARN_RATIO)))


def classify(used: int, limit: int) -> str:
    """一个窗口的等级：``unlimited`` / ``ok`` / ``warn`` / ``blocked``。"""
    if limit <= 0:
        return "unlimited"
    if used >= limit:
        return "blocked"
    if used >= warn_at(limit):
        return "warn"
    return "ok"


def remaining(used: int, limit: int) -> int | None:
    """还能用几次；不限返回 ``None``（不是 0——0 会被读成「用完了」）。"""
    if limit <= 0:
        return None
    return max(0, limit - used)


def text(window: str, used: int, limit: int) -> str:
    """一句人读的话（日志与看板共用，别再各写一份文案）。"""
    name = _WINDOW_NAME.get(window, window)
    left = remaining(used, limit)
    if left is None:
        return f"{name}不限（已用 {used}）"
    return f"{name}上限 {limit}，已用 {used}，还剩 {left}"


def observe(
    key: str,
    window: str,
    used: int,
    limit: int,
    *,
    now: datetime | None = None,
    path: Path | None = None,
    sink: Callable[[str, dict], None] | None = None,
) -> str:
    """判一个窗口的额度，并在 ``warn`` / ``blocked`` 当天第一次喊一声。

    返回 ``unlimited`` / ``ok`` / ``warn`` / ``blocked``；调用点**只按返回值**决定
    拦不拦（``blocked``）。**喊话失败不影响返回值**。``sink`` 给测试用，默认走日志。
    """
    level = classify(used, limit)
    if level in ("warn", "blocked"):
        try:
            _announce_once(key, window, used, limit, level, now=now, path=path, sink=sink)
        except Exception:  # noqa: BLE001 - 喊不出来也不能改判定
            log.warning(
                "quota_notice_failed",
                window=window,
                level=level,
                error=repr(__import__("sys").exc_info()[1])[:160],
            )
    return level


def _announce_once(
    key: str,
    window: str,
    used: int,
    limit: int,
    level: str,
    *,
    now: datetime | None = None,
    path: Path | None = None,
    sink: Callable[[str, dict], None] | None = None,
) -> bool:
    """当天第一次为这个组合喊一声；返回是否真的喊了。"""
    today = (now or datetime.now()).date().isoformat()
    # 组合里带上限：改一行数据改了上限之后要重新说一声（额度变了，旧提醒不算数）
    stamp = f"{key}|{window}|{limit}|{level}"
    marker = Path(path or MARKER)

    slot = (str(marker), today)
    bucket = _MEM.setdefault(slot, set())
    for stale in [key_ for key_ in _MEM if key_[1] != today]:
        _MEM.pop(stale, None)          # 只留当天，内存也不长
    if stamp in bucket:
        return False
    bucket.add(stamp)

    state = read_json(marker, default=None) or {}
    seen = [str(x) for x in (state.get("seen") or [])] if str(state.get("day") or "") == today else []
    if stamp in seen:
        return False
    seen.append(stamp)
    if not atomic_write_json(marker, {"day": today, "seen": seen[-20:]}):
        log.warning("quota_notice_state_failed", day=today, detail="去重状态没落盘，靠内存兜底")

    event = "quota_near_limit" if level == "warn" else "quota_exhausted"
    payload = {
        "window": window,
        "used": used,
        "limit": limit,
        "level": level,
        "key": key,
        "detail": text(window, used, limit),
    }
    if sink is not None:
        sink(event, payload)
    else:
        log.warning(event, **payload)
    return True
