"""同一件事只说一次。

有些告警是"环境如此"（老库缺列、内容不是 XML、OCR 读不到），不是错误；
它们每分钟都成立，但每 2 秒写一行 `logs/silent_warnings.log` 只会把真正的告警埋掉。

用法：
    from core.infra.warn_guard import once
    if once("legacy_columns:" + table, every_s=6 * 3600):
        warn("db/sync_service.legacy_columns", table=table, err=repr(exc))

进程内记忆（不做持久化）：重启后再报一次是合理的 —— 那说明换了新进程、新现场。
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_SEEN: dict[str, float] = {}


def once(key: str, every_s: float = 3600.0) -> bool:
    """key 在 every_s 秒内已经出现过就返回 False（调用方据此跳过告警）。"""
    now = time.monotonic()
    with _LOCK:
        last = _SEEN.get(key)
        if last is not None and (now - last) < float(every_s):
            return False
        _SEEN[key] = now
        return True


def reset(key: str | None = None) -> None:
    """测试用：清掉一个 key（或全部）。"""
    with _LOCK:
        if key is None:
            _SEEN.clear()
        else:
            _SEEN.pop(key, None)


def seen_count() -> int:
    with _LOCK:
        return len(_SEEN)
