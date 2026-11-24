"""被拦下来的话：旁证的读侧（按哪道闸 + 按天 + 长期小计）。

写侧在 `loop/ports.py::_log_blocked`（四道闸共用 `logs/blocked.jsonl`，一行一条、
内容闸那条不记正文）。这个模块做三件事：

1. 把 `why` 归到「哪道闸」——白名单 / 内容闸 / 外发闸 / 自发闸 / 其它；
2. 按天聚合，给周报（§45）和面板「最近 N 天」用；
3. 把每天的条数**攒成一份长期小计**（`<旁证目录>/blocked_days.json`）——旁证只留两代，
   轮转之后老日子就再也数不出来了；小计按天取 `max` 合并，所以不会因为重读而翻倍。

为什么单独立一个模块：周报在 `core/` 里，不能去 import `console/executor.py`
（那是面板后端），也不能让周报为了读一个旁证文件去假装成面板。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

AT_FMT = "%Y-%m-%d %H:%M:%S"

#: 四道闸 + 兜底。顺序就是面板与周报里的显示顺序。
CATEGORIES: tuple[str, ...] = ("allowlist", "content", "outbound", "spont", "other")

CATEGORY_LABELS: dict[str, str] = {
    "allowlist": "不在说话白名单里",
    "content": "内容闸",
    "outbound": "外发闸（去重 / 太密 / 空话）",
    "spont": "自发闸",
    "other": "其它",
}

#: 精确码 -> 类别。`content:*` / `spont:*` 是带参数的，见 `category()`。
_WHY_CATEGORY: dict[str, str] = {
    "not_in_allowlist": "allowlist",
    "unknown_chat": "allowlist",
    "policy_check_failed": "allowlist",
    "duplicate": "outbound",
    "too_soon": "outbound",
    "empty_text": "outbound",
}

BLOCK_LABELS: dict[str, str] = {
    "not_in_allowlist": "不在说话白名单里",
    "duplicate": "同一句话刚说过（120 秒内不重复）",
    "too_soon": "自发开口太密（同一会话 300 秒一条）",
    "empty_text": "空话（没内容可发）",
    "unknown_chat": "认不出这个会话",
    "policy_check_failed": "名单读不出来（按不许说话处理）",
}

#: 小计文件名（与旁证同目录，跟着旁证走——测试把旁证钉到 tmp，小计也自动隔离）。
ROLLUP_NAME = "blocked_days.json"

#: 小计留多久。一天一行、一行几十字节，一年多也就几十 KB。
ROLLUP_KEEP_DAYS = 400


def default_path() -> str:
    """旁证文件的默认位置（`logs/blocked.jsonl`）。"""
    return os.path.join(ROOT, "logs", "blocked.jsonl")


def rollup_path(log_path: str | None = None) -> str:
    """按天小计的位置：跟旁证同目录（测试只要钉住旁证，小计就不会漏进生产）。"""
    p = str(log_path or default_path())
    return os.path.join(os.path.dirname(p), ROLLUP_NAME)


def category(why: str) -> str:
    """这条 `why` 是哪道闸拦的。认不出归 ``"other"``（不猜）。"""
    w = str(why or "")
    if w.startswith("content:"):
        return "content"
    if w.startswith("spont:"):
        return "spont"
    return _WHY_CATEGORY.get(w, "other")


def label(why: str) -> str:
    """把 `why` 翻成人话（面板与周报共用同一套词，免得两边对不上）。"""
    w = str(why or "")
    if w.startswith("content:"):
        return "内容闸拦下：%s" % w.split(":", 1)[1]
    if w.startswith("spont:"):
        return "自发闸拦下：%s" % w.split(":", 1)[1]
    return BLOCK_LABELS.get(w, w or "（没写原因）")


def read_rows(path: str | None = None) -> tuple[list[dict], int, bool]:
    """读旁证。返回 ``(行, 读不懂的行数, 文件是不是不存在)``。

    读不懂的行**不丢也不炸**：计入坏行数，别让它把整份汇总带没。
    """
    p = str(path or default_path())
    rows: list[dict] = []
    bad = 0
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    got = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(got, dict):
                    rows.append(got)
                else:
                    bad += 1
    except FileNotFoundError:
        return [], bad, True
    except OSError:
        return [], bad, False
    return rows, bad, False


def _load_rollup(path: str) -> tuple[dict, bool]:
    """读按天小计。返回 ``({日期: {n, cats}}, 文件是否存在)``。"""
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        return {}, False
    days = got.get("days") if isinstance(got, dict) else None
    if not isinstance(days, dict):
        return {}, True
    out: dict[str, dict] = {}
    for key, cell in days.items():
        if not isinstance(cell, dict):
            continue
        cats = cell.get("cats")
        out[str(key)] = {
            "n": int(cell.get("n") or 0),
            "cats": {str(k): int(v or 0) for k, v in (cats or {}).items()} if isinstance(cats, dict) else {},
        }
    return out, True


def _save_rollup(path: str, days: dict) -> bool:
    """写按天小计（走 `jsonio` 的原子写，唯一临时名 + rename 都在那一层）。"""
    from core.infra import jsonio

    body = {
        "version": 1,
        "updated_at": time.strftime(AT_FMT),
        "days": {k: {"n": int(v.get("n") or 0), "cats": dict(v.get("cats") or {})}
                 for k, v in days.items()},
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return bool(jsonio.atomic_write_json(path, body, indent=1, sort_keys=True))
    except Exception:  # noqa: BLE001 - 小计写不下不影响这一次的汇总（下面照旧返回合并结果）
        return False


def _merge_days(live: dict, store_path: str, *, store: bool = True) -> tuple[dict, bool]:
    """把「这次从旁证里数出来的」与「以前攒下的」按天取 max 合并。

    为什么是 max 而不是相加：同一天会被数很多次（每看一眼面板、每周报一次），
    相加就翻倍；而旁证轮转后老日子只会变少，所以取 max 既不翻倍也不丢历史。
    """
    old, had = _load_rollup(store_path)
    merged: dict[str, dict] = {}
    for src in (old, live):
        for key, cell in src.items():
            dst = merged.setdefault(str(key), {"n": 0, "cats": {}})
            dst["n"] = max(int(dst.get("n") or 0), int((cell or {}).get("n") or 0))
            for c, v in dict((cell or {}).get("cats") or {}).items():
                dst["cats"][str(c)] = max(int(dst["cats"].get(str(c)) or 0), int(v or 0))
    for cell in merged.values():
        # 一天的总数至少等于它各道闸之和：词表变过时也不会出现「总数 < 分项」。
        cell["n"] = max(int(cell.get("n") or 0), sum(int(v) for v in cell["cats"].values()))
    floor = (datetime.now() - timedelta(days=ROLLUP_KEEP_DAYS)).strftime("%Y-%m-%d")
    merged = {k: v for k, v in merged.items() if k >= floor}
    if store and merged != old:
        _save_rollup(store_path, merged)
    return merged, had


def summarize(days: int = 7, *, path: str | None = None, now: Any = None,
              store: bool = True) -> dict:
    """最近 `days` 天（含今天）被拦下来的话，按道闸与按天汇总。

    只读旁证；`store=True` 时顺手把每天的条数并进长期小计（`blocked_days.json`），
    这样旁证轮转掉之后「上周三挡了几条」还查得到。

    返回的 ``missing`` 为真表示**旁证和小计都不存在**——调用方必须把它显示成
    「查不到」，不许当成「一次都没拦过」（那两件事的结论完全相反）。
    """
    span = max(1, int(days or 7))
    p = str(path or default_path())
    now_ts = time.time() if now is None else float(now)
    now_dt = datetime.fromtimestamp(now_ts)
    cutoff = (now_dt - timedelta(days=span - 1)).date()

    rows, bad, missing = read_rows(p)
    live: dict[str, dict] = {}
    by_label: dict[str, int] = {}
    oldest = ""
    newest = ""
    for row in rows:
        at = str(row.get("at") or "")
        try:
            when = datetime.strptime(at, AT_FMT)
        except ValueError:
            bad += 1
            continue
        if when.date() < cutoff:
            continue
        why = str(row.get("why") or "")
        key = when.strftime("%Y-%m-%d")
        cell = live.setdefault(key, {"n": 0, "cats": {}})
        cell["n"] += 1
        cat = category(why)
        cell["cats"][cat] = cell["cats"].get(cat, 0) + 1
        lb = label(why)
        by_label[lb] = by_label.get(lb, 0) + 1
        if not oldest or at < oldest:
            oldest = at
        if at > newest:
            newest = at

    store_path = rollup_path(p)
    merged, had_store = _merge_days(live, store_path, store=store)

    by_cat: dict[str, int] = {}
    by_day: list[dict] = []
    total = 0
    for key in sorted(merged, reverse=True):
        cell = merged[key]
        if key < cutoff.strftime("%Y-%m-%d"):
            continue
        total += int(cell.get("n") or 0)
        for c, v in dict(cell.get("cats") or {}).items():
            by_cat[c] = by_cat.get(c, 0) + int(v)
        by_day.append({"date": key, "n": int(cell.get("n") or 0), "cats": dict(cell.get("cats") or {})})

    today = now_dt.strftime("%Y-%m-%d")
    return {
        "ok": True,
        "path": p,
        "days": span,
        "total": total,
        "today": int((merged.get(today) or {}).get("n") or 0),
        "today_date": today,
        "by_category": {k: by_cat[k] for k in CATEGORIES if k in by_cat},
        "by_label": by_label,
        "by_day": by_day,
        "bad_lines": bad,
        "missing": bool(missing and not had_store),
        "rollup": store_path,
        "oldest": oldest,
        "newest": newest,
    }
