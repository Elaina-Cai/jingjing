"""静默失败禁令的统一落点（全仓通用，零依赖）。

2026-10-24 立。一天里四个 bug 全是同一个病根：宽 except 或静默 pass 把错误吞掉，
进程看着健康、日志干净、功能静默缺失。这里只做一件事：把被吞掉的东西写下来。

落点 logs/silent_warnings.log，同一事件每 60 秒最多 5 条、每进程最多 2000 条，
防止热点路径把日志写爆。绝不抛异常、绝不阻塞。
"""
from __future__ import annotations

import os
import time
from datetime import datetime

LOG = os.path.join("logs", "silent_warnings.log")
_PER_EVENT_LIMIT = 5
_PER_EVENT_WINDOW = 60.0
_TOTAL_LIMIT = 2000

_seen: dict[str, list[float]] = {}
_total = 0
# 2026-11-22：这份文件是「只增不切」的第三个（前两个在 core/infra/log_rotate 里）。
# 每进程 2000 条上限挡不住进程重启次数，盘满那次它已经 5.5MB。5MB×3 足够排障。
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3


def _rotate_if_big() -> None:
    """超过上限就把 LOG 换成 .1，旧的依次后移；失败一律放过。"""
    try:
        if os.path.getsize(LOG) < _MAX_BYTES:
            return
    except OSError:
        return
    try:
        for i in range(_BACKUPS - 1, 0, -1):
            src = "%s.%d" % (LOG, i)
            if os.path.exists(src):
                os.replace(src, "%s.%d" % (LOG, i + 1))
        os.replace(LOG, LOG + ".1")
    except OSError:
        pass


def warn(event: str, **fields) -> None:
    """记一行「本该被看见」的事。"""
    global _total
    now = time.time()
    hits = _seen.get(event)
    if hits is None:
        hits = _seen[event] = []
    hits[:] = [t for t in hits if now - t < _PER_EVENT_WINDOW]
    if len(hits) >= _PER_EVENT_LIMIT or _total >= _TOTAL_LIMIT:
        return
    hits.append(now)
    _total += 1
    try:
        _rotate_if_big()
        os.makedirs("logs", exist_ok=True)
        detail = " ".join("%s=%r" % (k, v) for k, v in fields.items())
        line = "%s %s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), event, detail)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except Exception:  # 有意忽略：记日志本身失败不能再抛，否则会把调用方拖死
        return
