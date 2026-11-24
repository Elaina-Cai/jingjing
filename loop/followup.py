"""续话窗口（2026-10-28）。

她说完一句、对方还没接的时候，给她一次"再补一句"的机会。

为什么需要：一拍里 say 成功即结束，下一拍要有人说话才会起来。
于是"我说错了/我还想说"在机制上无处安放 —— 而"补一句"恰恰是最像人的东西。

判据（全部只看事实，不判断该不该说）：
  · 今天账本里最后一条成功发出的 say，且本地证据能证明它真的发过
    （直发必须有 ack=confirmed；走队列的每个气泡都要有精确回执）
  · 距发送 20 ~ 180 秒（队列气泡按落定时间，账本时间仍限制最大年龄）
  · 那个会话自她说完之后**没有**别人再说话（is_self=0）
  · 同一句只放行一次；全局 15 分钟内最多一次
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from core.infra.jsonio import atomic_write_json, read_json
from core.infra.envflags import text

_ROOT = Path(__file__).resolve().parents[1]
LEDGER_DIR = _ROOT / "data" / "loop"
WORLD_DB = _ROOT / "data" / "world.db"
USED = _ROOT / "data" / "loop" / "followup_used.json"

WINDOW_S = float(text("YINYUE_FOLLOWUP_WINDOW_S", "180") or 180)
MIN_AGE_S = float(text("YINYUE_FOLLOWUP_MIN_AGE_S", "20") or 20)
MIN_GAP_S = float(text("YINYUE_FOLLOWUP_MIN_GAP_S", "900") or 900)


def _ledger_path() -> Path:
    # 流水文件名只有一个来源（PIT-111）：路径去问 core.infra.actions
    from core.infra import actions as _actions

    return Path(_actions.path())


def _last_say() -> dict | None:
    """今天最后一条真的发出去的 say。读不到就 None。"""
    p = _ledger_path()
    if not p.is_file():
        return None
    last = None
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"act": "say"' not in line and '"act":"say"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get("act")) != "say":
                    continue
                if str(row.get("result")) not in ("sent", "ok"):
                    continue
                last = row
    except Exception:
        return None
    return last


def _ts(text: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(str(text)[:19], fmt))
        except Exception:
            continue
    return 0.0


def _say_scope(row: dict) -> tuple[str, str, str] | None:
    """账本 say 顶层 scope：平台、账号、会话都齐全才算数。

    旧账（没有 scope）和跨会话的 target 都没法安全续话，返回 None。
    """
    scope = row.get("scope") if isinstance(row, dict) else None
    if not isinstance(scope, dict):
        return None
    platform = str(scope.get("platform") or "")
    account_id = str(scope.get("account_id") or "")
    chat_id = str(scope.get("chat_id") or "")
    if not (platform and account_id and chat_id):
        return None
    return platform, account_id, chat_id


def _heard_after(scope: tuple[str, str, str], said_at: str) -> bool | None:
    """检查原会话是否有新话。None 表示无法核实。"""
    if not WORLD_DB.is_file():
        return None
    try:
        from contextlib import closing
        from db.world_db import to_utc

        cutoff = to_utc(said_at)
        if not cutoff:
            return None
        with closing(sqlite3.connect(WORLD_DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)) as con:
            row = con.execute(
                "select 1 from inbound_event "
                "where platform = ? and account_id = ? and chat_id = ? "
                "and ifnull(is_self,0) = 0 and ts_utc > ? limit 1",
                (*scope, cutoff),
            ).fetchone()
    except Exception:
        return None
    return row is not None


def _used() -> dict:
    try:
        return read_json(USED, {})
    except Exception:
        return {}


def _mark(key: str) -> None:
    d = _used()
    d["last_key"] = key
    d["last_at"] = time.time()
    try:
        atomic_write_json(USED, d)
    except Exception:
        pass


def _proven_send_at(row: dict, scope: tuple[str, str, str], at: float) -> float | None:
    """本地能证明这句真的发过时，返回最晚观察时间（队列落定或账本时间）。

    判据只在 ``ledger.confirmed_say_at`` 一处，这里不再保留第二份规则。
    """
    from .ledger import confirmed_say_at

    return confirmed_say_at(row, scope, at)


def due(now: float | None = None) -> dict | None:
    """该不该放一次"续话"的拍。返回事实，不返回"建议"。"""
    now = float(now or time.time())
    row = _last_say()
    if not row:
        return None
    said_at = str(row.get("at") or "")
    at = _ts(said_at)
    if at <= 0 or not MIN_AGE_S <= now - at <= WINDOW_S:
        return None
    target = str(row.get("target") or "")
    text = str(row.get("text") or "")
    scope = _say_scope(row)
    if scope is None or target != scope[2]:
        return None
    key = json.dumps([*scope, int(at)], ensure_ascii=False)
    st = _used()
    if st.get("last_key") == key:
        return None
    if float(st.get("last_at") or 0) > 0 and (now - float(st["last_at"])) < MIN_GAP_S:
        return None
    observed_at = _proven_send_at(row, scope, at)
    if observed_at is None or now - observed_at < MIN_AGE_S:
        return None
    if _heard_after(scope, said_at) is not False:
        return None
    _mark(key)
    return {"target": target, "text": text, "said_at": said_at,
            "age_s": int(now - observed_at), "scope": scope}


def note(w: dict) -> list[str]:
    """给她的那几行事实（不催，也不拦）。"""
    if not w:
        return []
    mins = max(1, int(w.get("age_s") or 0) // 60)
    return [
        "你刚说完上一句（%d 分钟前，对方还没接）：" % mins,
        "    「%s」" % str(w.get("text") or "")[:60],
        "    你可以补一句，也可以就这么放着。",
    ]
