"""图像屏障：这一拍要说的东西里有图、图还没看清，就先别开口。

用户 2026-10-27 的要求（原话）："等图片解析来了之后，再接着后面的信息拼上去，
拼完才是模型看到的，这时候模型看完才说话"。所以这一层只做一件事：**同步等**窗口
里的媒体行都拿到描述，没齐就告诉调用方"这一拍别说话"。

三道刹车（没有它们，一张坏图能让她永远不开口）：
1. 只等**动作得到**的行：本地有文件、或者有 md5 的行才算数；"no clue recorded"
   这种连 md5 都没有的行，等一辈子也不会变——直接当已就绪。
2. 总等待上限（默认 20 秒）：到点返回未就绪，本拍跳过；描述落库后
   ``ports.world_stamp()`` 里的 ``media_ref.desc_version()`` 一变，她会被再叫醒一次。
3. 同一张图失败两次、或已经退避到很久以后，就不再拿它挡路（10 分钟后再试）。

开关：``YINYUE_MEDIA_BARRIER=wait|off``（默认 wait；写错按 off 处理 —— 出错的
时候宁可回到旧行为，也不要突然不回话）。
"""
from __future__ import annotations

import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from .safelog import warn

ENV = "YINYUE_MEDIA_BARRIER"
MODES = ("wait", "off")
DEFAULT_MODE = "wait"
WINDOW_S = 600.0          # 只等最近 10 分钟的行；更早的历史图交给 ports 的 prefetch
TOTAL_BUDGET_S = 20.0     # 一次等待最多这么久
PER_ROW_BUDGET_S = 6.0    # 单张图最多给它这么久
WORKERS = 2               # 同时最多问两张（视觉接口限流）
MAX_TRIES = 2             # 同一张图失败这么多次就不再挡路
GIVE_UP_S = 900.0         # 放弃后多久再重新拿它挡路
KINDS = ("image", "quote_image", "emoji")

_GIVE_UP: dict = {}


def mode() -> str:
    """``wait`` / ``off``。写错值按 off 处理（宁可旧行为，也不要突然不吭声）。"""
    got = (os.environ.get(ENV) or "").strip().lower()
    if not got:
        return DEFAULT_MODE
    return got if got in MODES else "off"


def _now() -> datetime:
    return datetime.now()


DEAD_STATUSES = ("missing", "failed", "skipped", "ignored")


def _actionable(row: dict) -> bool:
    """这张图还有救吗：本地有文件、或者有 md5 能去找。

    2026-11-01：``status`` 已经说"没救了"的行（本地文件早就不在盘上、连 md5 都没有）
    不该再拿来挡路——它们会被 ``status='missing'`` 标掉（见 media_barrier 的清理脚本），
    以前只看"有没有 md5"，一张只剩 md5 的死行能每 10 分钟让整拍白等 20 秒。
    """
    if str(row.get("status") or "").strip().lower() in DEAD_STATUSES:
        return False
    path = str(row.get("local_path") or "").strip()
    if path and os.path.exists(path):
        return True
    return bool(str(row.get("md5") or "").strip()) and len(str(row.get("md5") or "").strip()) >= 16


def _given_up(key: str) -> bool:
    hit = _GIVE_UP.get(key)
    if not hit:
        return False
    if hit[0] < MAX_TRIES:          # 前两次超时还要再试，别一张图一次抖动就放过去
        return False
    return hit[1] <= time.time() <= hit[1] + GIVE_UP_S


def _bump_give_up(key: str) -> None:
    n = (_GIVE_UP.get(key) or (0, 0))[0] + 1
    _GIVE_UP[key] = (n, time.time())


def pending(db_path: str, *, now: datetime | None = None, window_s: float = WINDOW_S) -> list:
    """窗口内、还没描述、而且还有救的媒体行（按时间正序 —— 先发的先看清）。"""
    now = now or _now()
    since = (now - timedelta(seconds=float(window_s))).strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        "select id, msg_id, chat_id, kind, status, coalesce(md5,'') as md5,"
        " coalesce(local_path,'') as local_path, coalesce(description,'') as description,"
        " coalesce(attempts,0) as attempts, coalesce(next_retry_at,'') as next_retry_at,"
        " created_at from message_media"
        " where created_at >= ? and (description is null or trim(description) = '')"
        " and coalesce(kind,'') in ('image','quote_image','emoji')"
        " and coalesce(status,'') not in ('missing','failed','skipped','ignored')"
        " order by created_at"
    )
    rows: list = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % str(db_path).replace("\\", "/"), uri=True, timeout=2.0)
        try:
            con.row_factory = sqlite3.Row
            for r in con.execute(sql, (since,)):
                row = {k: r[k] for k in r.keys()}
                if not _actionable(row):
                    continue
                if _given_up("%s#%s" % (db_path, row.get("id"))):
                    continue
                rows.append(row)
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - 读不到就当没有，绝不因此不开口
        warn("media_barrier.pending.Exception", err=repr(exc)[:160], db=str(db_path))
        return []
    return rows


def _md5_of(row: dict) -> str:
    md5 = str(row.get("md5") or "").strip().lower()
    if len(md5) == 32:
        return md5
    stem = os.path.splitext(os.path.basename(str(row.get("local_path") or "")))[0].lower()
    for cut in ("_t", "_b", "_h"):
        if stem.endswith(cut):
            stem = stem[: -len(cut)]
    return stem if len(stem) == 32 else ""



_ENGINE_CACHE: dict = {}


def _engine_for(db_path: str):
    """账号库的可写 engine（描述要写到**这一行**所在的库，不是进程默认库）。

    2026-11-01：以前走 ``media.ensure_image_description(path)`` 不传 engine →
    内部回落到 ``get_repo()``（loop 进程没有账号上下文，回落成共享库）→ 描述只进
    内存缓存、行上永远 NULL → 屏障每 15 分钟白等 20 秒并把整拍跳过。
    """
    key = os.path.abspath(str(db_path or ""))
    if key in _ENGINE_CACHE:
        return _ENGINE_CACHE[key]
    eng = None
    try:
        from db.repository import RepositoryManager

        eng = RepositoryManager(key).engine
    except Exception as exc:  # noqa: BLE001 - 拿不到可写句柄不该拖垮这一拍
        warn("media_barrier.engine_unavailable", db=key[-48:], err=repr(exc)[:160])
        eng = None
    if len(_ENGINE_CACHE) > 16:
        _ENGINE_CACHE.clear()
    _ENGINE_CACHE[key] = eng
    return eng


def ask_once(row: dict, budget_s: float, *, db_path: str = "") -> bool:
    """把这一张图往前推一步：能问视觉就问（写回库），问不了就算了。"""
    path = str(row.get("local_path") or "").strip()
    md5 = _md5_of(row)
    eng = _engine_for(db_path) if db_path else None
    try:
        if path and os.path.exists(path):
            from core.conversation import media as _media

            got = _media.ensure_image_description(path, engine=eng, budget_s=budget_s)
            if not got:
                warn("media_barrier.described_but_empty", row=row.get("id"),
                     path=os.path.basename(path)[-24:])
            return bool(got)
        if md5:
            from core.conversation import media_ref as _mr

            return bool(
                _mr.describe_md5(
                    md5,
                    engine=eng,
                    path=path,
                    chat_id=str(row.get("chat_id") or ""),
                    created_at=str(row.get("created_at") or ""),
                    mode="wait",
                    budget_s=budget_s,
                )
            )
        from core.conversation import media_registry as _reg

        _reg.resolve_pending(_reg._engine(), limit=4)
    except Exception as exc:  # noqa: BLE001 - 一张图失败不许拖垮这一拍
        warn("media_barrier.ask.Exception", err=repr(exc)[:160], row=row.get("id"))
    return False


def wait_ready(
    db_path: str,
    *,
    now: datetime | None = None,
    window_s: float = WINDOW_S,
    total_budget_s: float = TOTAL_BUDGET_S,
    per_row_s: float = PER_ROW_BUDGET_S,
    workers: int = WORKERS,
    ask=None,
) -> tuple:
    """等窗口里的图都看清。返回 ``(ready, 还差哪些)``。

    ``ready=False`` 时调用方应当**跳过这一拍**（不消耗世界指纹），等描述落库后
    指纹自己会再变。``ask`` 只为测试注入，默认走真视觉。
    """
    if mode() == "off":
        return True, []
    _ask_raw = ask or ask_once

    def _ask(row, per):
        try:
            return _ask_raw(row, per, db_path=db_path)
        except TypeError:
            return _ask_raw(row, per)
    started = time.time()
    deadline = started + max(0.5, float(total_budget_s))
    rows: list = []
    while True:
        rows = pending(db_path, now=now, window_s=window_s)
        if not rows:
            return True, []
        left = deadline - time.time()
        if left <= 0.05:
            for row in rows:
                _bump_give_up("%s#%s" % (db_path, row.get("id")))
                warn("media_barrier.give_up", row=row.get("id"),
                     kind=row.get("kind"), attempts=row.get("attempts"),
                     path=os.path.basename(str(row.get("local_path") or ""))[-24:])
            warn(
                "media_barrier.timeout",
                pending=len(rows),
                waited_s=round(time.time() - started, 1),
                rows=[r.get("id") for r in rows][:6],
            )
            return False, rows

        per = max(1.0, min(float(per_row_s), left))
        batch = rows[: max(1, int(workers))]
        try:
            with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
                list(pool.map(lambda r: _ask(r, per), batch))
        except Exception as exc:  # noqa: BLE001
            warn("media_barrier.pool.Exception", err=repr(exc)[:160])
        if time.time() < deadline:
            time.sleep(0.3)


def status(db_path: str, *, window_s: float = WINDOW_S) -> dict:
    """给面板/自检看的现状：还有几张图没看清。"""
    rows = pending(db_path, window_s=window_s)
    return {
        "ok": True,
        "mode": mode(),
        "pending": len(rows),
        "rows": [{"id": r.get("id"), "kind": r.get("kind"), "at": str(r.get("created_at") or "")[:19]} for r in rows[:12]],
    }
