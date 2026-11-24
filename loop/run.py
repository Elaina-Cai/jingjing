"""她自己醒来：世界变了才起一拍。

不是"每秒跑一次模型"——是每 POLL 秒看一眼世界指纹，只有变了才叫醒她。
三道刹车：指纹没变不起、同一会话 MIN_GAP 内不重复、每拍 Tick 超时。

    python -m loop.run            # 常驻
    python -m loop.run --once     # 只跑一拍（调试）
    python -m loop.run --dry      # 只决定不发送
"""
from __future__ import annotations

from core.infra import duration  # 时长只有一个单位：毫秒（UI-066）
from core.infra.duration import to_ms, sleep_s, now_ms, human  # noqa: F401
from core.infra import liveness  # 心跳文件名与判死口径只有一处

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime

import structlog

from core.conversation import media_ref
from loop import outbox, brain, direct_send, history, ledger, media_barrier, ports, selfstate, snapshot
from .safelog import warn
from core.infra.warn_guard import once
from loop import goal_review
_GOAL_REVIEWS = goal_review.ReviewDispatcher()
from core.context import intention_close  # P1-1 对话侧销账（off/shadow/on，默认 off）
from core.quality import feedback as _feedback  # 2026-11-04：repeat/correction 采集接回现役回合
from core.infra.jsonio import atomic_write_json
from core.infra.envflags import text
POLL_MS = to_ms("400ms")   # 多久看一眼世界（只问「世界有没有被动过」，不问模型）
MIN_TICK_INTERVAL_MS = to_ms("4s")   # 两拍之间至少隔这么久（这是**拍子节奏**，不是发车间隔）
# 2026-10-26：世界停了（主程序死了）的时候，最多半小时叫醒她一次说这件事，
# 否则每拍都醒一次，她会反复念「我这边好像卡住了」。
STALE_WAKE_MS = to_ms("1800s")
_STALE_WAKE = [0]
_HIST_DAY = [""]  # 2026-10-27 晚：小本本当天的日号，写过就记住
_HIST_TRY = [0.0]  # 2026-10-27 晚：上次尝试写小本本的时间戳（节流用）
# 世界卡住时她最多为此开口几次：再往后就只是噪音（每小时一条日志告警，人来看）。
STALE_WAKE_LIMIT = 3
_STALE_COUNT = [0]
_PEND_TRIES = [0]  # 还有没轮到的★：连着想再去几次（原代码引用了这个名字却没定义 → 每拍 NameError）
_PEND_LAST = [0]
_ERR_N = [0]          # 循环里连续出错的次数（2026-10-31：为了别只印一行 repr）
_ERR_LAST = [0]       # 上次印完整栈的时刻（毫秒）      # 上一次「为待办窗口主动再拍一次」的时刻
_PEND_GAP_MS = 20_000  # 待办窗口的重试间隔：不靠世界变化，自己隔 20 秒再拍
TICK_TIMEOUT_MS = 4 * 60 * 60 * 1000   # 2026-10-31：主人要求取消上限（原 90s）——单窗口回合不再 90 秒被砍
# 心跳承诺节拍：最坏情况 = 一拍跑满超时 + 两拍之间最小间隔 + 几次轮询
BEAT_MS = TICK_TIMEOUT_MS + MIN_TICK_INTERVAL_MS + to_ms("5s")  # 承诺给监管者的心跳节拍
_HB_EVERY_MS = to_ms("15s")   # 心跳最多这么久写一次

#: 2026-11-06：心跳里留一道「刚才那段时间去哪了」的账。
#: 机器睡过去 / 进程被挂起时没人写心跳，醒来第一跳的墙钟差就是那段空白；
#: 只把**最近一次**超过阈值的空白留在文件里，监管者 5 分钟才看一次也读得到。
#: 30 分钟以下不算：重启、GC、一次长拍的抖动都比这短。
_HB_GAP_ALERT_MS = to_ms("30m")
_HIST_EVERY_MS = to_ms("300s")  # 历史账本最多这么久试一次
HEARTBEAT = liveness.path("engine")
STOP_FILE = os.path.join("logs", "loop.stop")

# 2026-11-05（§35.6 第 2 条）：引擎自己也要记退出账 —— 顶层 import，守卫才看得见这个名字。
from core.infra import exitlog as _exitlog  # noqa: E402


def _log():

    try:
        os.makedirs("logs", exist_ok=True)
        _fh = open(os.path.join("logs", "loop_engine.out"), "a", encoding="utf-8", buffering=1)
        sys.stdout = _fh
        sys.stderr = _fh
    except Exception:  # noqa: BLE001
        warn("run:39 Exception", err=repr(__import__("sys").exc_info()[1]))
    # 2026-10-27 晚：这里原来是 make_filtering_bound_logger(WARNING)，
    # 把整个引擎进程里 structlog 的 INFO 全掐掉了（wx_* / self_history_written 一条都进不了日志）。
    # 统一日志：走 core.infra.logging，与调度器写同一个 logs/yinyue.log。
    try:
        from core.infra.logging import init_logging

        init_logging(level=(text("YINYUE_LOG_LEVEL") or "INFO"), log_dir="logs",
                      filename="loop_engine.log")  # 2026-11-04：独立文件，别跟调度器抢轮转
    except Exception:  # noqa: BLE001
        warn("run:_log init_logging", err=repr(__import__("sys").exc_info()[1]))
    logging.getLogger("loop.run").info("engine_log_sink_ready log_dir=%s", "logs")
# 2026-10-26：下面的"凌晨写小本本"用了 log.info/log.warning，但这个模块从来没有 log
# （ruff F821）。所以那条路径每次都在 except 里静默失败——她自己的小本本一直是空的。
log = logging.getLogger("loop.run")


_SPONT_BLOCK_LOG: dict = {"key": None, "at": 0.0}
_SPONT_BLOCK_EVERY_S = 600.0


def _spont_block_due(key, now: float) -> bool:
    """自发闸同一停因 10 分钟只记一次；停因变了立刻记。"""
    if key != _SPONT_BLOCK_LOG["key"] or now - float(_SPONT_BLOCK_LOG["at"]) >= _SPONT_BLOCK_EVERY_S:
        _SPONT_BLOCK_LOG.update({"key": key, "at": float(now)})
        return True
    return False


_HOT_CACHE: dict = {"at": 0.0, "value": frozenset()}
_HOT_TTL_S = 20.0


def _hot_chats(now_ts: float, window_s: float) -> set:
    """最近窗口里有人说话的会话。

    2026-11-22：这个查询原来在自发闸里每轮轮询（400ms）都跑一次，一天二十多万次，
    空转的引擎也占着约 8% 的 CPU。`GROUP_QUIET_S` 默认 1800 秒，20 秒的缓存
    对判据没有实际影响。
    """
    if now_ts - float(_HOT_CACHE["at"]) < _HOT_TTL_S:
        return set(_HOT_CACHE["value"])
    got = {str(e.get("chat_id") or "")
           for e in ports.peer_events_since(now_ts - float(window_s), limit=50)}
    _HOT_CACHE.update({"at": float(now_ts), "value": frozenset(got)})
    return got


def _fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# 2026-10-31：一拍里要跑 N 个窗口、每个窗口又不设步数上限，
# 一拍可能好几分钟；主循环那份心跳只在窗口之间写，
# 监管者于是整段时间看不到进展。这里存一份「我在哪」给 one_tick 用。
_ALIVE: dict = {}


def _heartbeat_gap(prev: dict, now_ms: int, pid: int) -> dict:
    """心跳里要带的「刚才那段空白」。纯函数：只算，不读写任何文件。

    机器进入 Modern Standby 时进程被挂起、15 秒一次的心跳整段消失；醒来第一跳的
    墙钟差就是那段空白。**只跟同一个 pid 比**：pid 变了是重启，那段空白归启动账
    （`core/infra/exitlog.record_start`），在这里再记一次会让同一段停机被数两遍。
    """
    try:
        prev_ms = int((prev or {}).get("at_ms") or 0)
    except (TypeError, ValueError):
        prev_ms = 0
    same_proc = str((prev or {}).get("pid") or "") == str(pid)
    if not (same_proc and prev_ms):
        return {}
    gap = int(now_ms) - prev_ms
    if gap < _HB_GAP_ALERT_MS:
        return {}
    return {"gap_ms": gap, "gap_at_ms": int(now_ms),
            "gap_prev_text": str((prev or {}).get("at_text") or "")}


def _write_heartbeat(data: dict) -> None:
    """写心跳。**at_ms / beat_ms 由这里统一盖**，调用方不许自己写（判死口径只有一个）。

    2026-11-06（A）：顺手记一段「刚才那段时间去哪了」。`at_ms` 用的是墙钟
    （`duration.now_ms()`），机器进入 Modern Standby 时进程被挂起、15 秒一次的心跳
    会整段消失；醒来第一跳的墙钟差就是那段空白。只留**最近一次**超过
    `_HB_GAP_ALERT_MS` 的空白（`gap_ms` / `gap_at_ms` / `gap_prev_text`），因为
    心跳文件每 15 秒就被覆盖一次，监管者 5 分钟才看一次，只写在当次跳里会被冲掉。
    """
    try:
        os.makedirs("logs", exist_ok=True)
        if "at" in data:  # 调用方给的是给人看的时间串，挪到 at_text，别和契约字段抢名字
            data["at_text"] = data.pop("at")
        prev = _hb_read()
        now = duration.now_ms()
        _gap = _heartbeat_gap(prev, now, os.getpid())
        if _gap:
            data.update(_gap)
            log.warning(f"engine_gap: gap_hours={_gap['gap_ms'] / 3600000.0:.1f} "
                        f"prev={_gap['gap_prev_text']}（这段时间没有任何心跳：机器睡眠或进程被挂起）")
        else:
            # 不覆盖上一次的空白记录：它是「她不在过」的唯一凭据（心跳每 15 秒覆盖一次）。
            for _k in ("gap_ms", "gap_at_ms", "gap_prev_text"):
                if _k not in data and prev.get(_k):
                    data[_k] = prev[_k]
        data["at_ms"] = now
        data["beat_ms"] = BEAT_MS
        atomic_write_json(HEARTBEAT, data, indent=2)
    except Exception:  # noqa: BLE001
        warn("run:53 Exception", err=repr(__import__("sys").exc_info()[1]))


# 2026-10-31：启动/建快照/模型思考期间也必须有心跳。
# 监管器只看 at_ms（beat_ms × GRACE ≈ 5 分钟没动就 kill），而以前只有"进入某个窗口"
# 和"一拍跑完"才写——启动时 build_snapshot 跑 vision 能到 2~3 分钟，超时就被自己人
# 砍掉，外面看起来就是"重启了没反应"（今天 09:19 / 09:25 / 09:32 / 12:19 各一次）。
_HB_KEEP_S = 5.0                     # 多久检查一次（真写还是要 stale 才写）
_HB_PHASE: dict = {"phase": "starting"}


def _hb_read() -> dict:
    try:
        with open(HEARTBEAT, encoding="utf-8") as fh:
            cur = json.load(fh)
        return cur if isinstance(cur, dict) else {}
    except (OSError, ValueError):
        return {}


def _hb_beat_once() -> bool:
    """把"我还活着"盖一遍：只动 pid / phase / at_text / at_ms / beat_ms，
    busy、window、ticks 这些由 one_tick 自己写，不覆盖（否则"忙到哪一步"就丢了）。"""
    try:
        cur = _hb_read()
        cur["pid"] = os.getpid()
        cur["phase"] = _HB_PHASE.get("phase") or "starting"
        cur["at_text"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _write_heartbeat(cur)
        return True
    except Exception:  # noqa: BLE001
        return False


def _hb_stale(cur: dict) -> bool:
    """心跳是否该补：没有 at_ms、或者已经超过 _HB_EVERY_MS 没刷新。"""
    try:
        at = int((cur or {}).get("at_ms") or 0)
    except (TypeError, ValueError):
        return True
    return (not at) or (duration.now_ms() - at >= _HB_EVERY_MS)


def _hb_keepalive(stop: "threading.Event") -> None:
    while not stop.wait(_HB_KEEP_S):
        if not _hb_stale(_hb_read()):
            continue          # 别人刚写过，不重复写（保持"最多 15 秒一次"的口径）
        _hb_beat_once()


def _wake_facts(last_tick: int, cell: dict | None = None) -> dict:
    """普通唤醒只取当前账号快照里这一窗的最近消息。"""
    elapsed = None
    peers, mine = [], []
    if last_tick:
        elapsed = duration.now_ms() - last_tick
        since = datetime.fromtimestamp(last_tick // 1000).strftime("%Y-%m-%d %H:%M:%S")
        window = cell or {}
        label = str(window.get("label") or window.get("chat_id") or "")
        peer = window.get("peer_last") or {}
        if str(peer.get("at") or "") > since:
            peers.append({"at": peer["at"], "chat": label, "who": peer.get("speaker", ""),
                          "text": str(peer.get("text") or "")[:120]})
        own = window.get("my_last") or {}
        if str(own.get("at") or "") > since:
            mine.append({"at": own["at"], "who": label, "text": str(own.get("text") or "")[:120]})
    return {"since_last_tick_ms": elapsed, "peers": peers, "mine": mine}


_HK_AT = 0.0


def _housekeep(min_gap_ms: int = 30000) -> None:
    # 2026-10-28：desk 挂了也要回收 —— TTL 过期与死信回收挂到引擎主循环，不再只靠 desk。
    global _HK_AT
    _n = duration.now_ms()
    if _HK_AT and (_n - _HK_AT) < int(min_gap_ms):
        return
    _HK_AT = _n
    try:
        from db import world_db as _W
        _W.reap_stale(120.0)
        _W.expire(on_expired=outbox.note_expired)
        log.debug("queue_housekeep_ok")
    except Exception as _e:
        try:
            log.warning(f"queue_housekeep_failed: error={str(_e)[:200]}")
        except Exception:
            pass

_ATTEND_MAX = 3          # 一拍最多处理几个会话（与旧的分窗上限同吞吐）
_MIN_SALIENCE = 12.0     # 低于此分不看：目前只当安全网（见 salience 注释）
_OWNER_CACHE: dict[str, bool] = {}


def _is_owner_chat(chat_id: str) -> bool:
    """这个会话是不是主人的私聊？判不出来就当不是（判据坏了不许抢注意力）。"""
    if not chat_id:
        return False
    if chat_id in _OWNER_CACHE:
        return _OWNER_CACHE[chat_id]
    got = False
    try:
        from core.jobs.ask import is_owner_private
        got = bool(is_owner_private(chat_id))
    except Exception as exc:  # noqa: BLE001 - 判据坏了不许带崩一拍
        from safelog import warn
        warn("run_owner_probe_failed", chat=chat_id, error=repr(exc)[:80])
    _OWNER_CACHE[chat_id] = got
    return got


def _age_min(at: str, now) -> float:
    """距现在多少分钟。解析不了返回一个很大但有限的值（排到最后，不报错）。"""
    try:
        from datetime import datetime as _dt
        return max(0.0, (now - _dt.strptime(str(at)[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60.0)
    except Exception:  # noqa: BLE001 - 时间戳坏了不许带崩
        return 1e6


def salience(cell: dict, *, now=None) -> tuple[float, dict]:
    """这个会话此刻值不值得看（2026-10-31 起替代旧的“最久没人理、取前 N 个”）。

    分项都可单独解释，便于在 trace 里读：
      need ：「有人在问我」40；「有人在说话、没叫我」12；其余不进候选（0）
      fresh：越新越高，20 分钟半衰 → 30 * 0.5 ** (age/20)
      wait ：等太久自动升级（替代旧排序）：超过 10 分钟后每分钟 +0.5，封顶 20
      owner：主人私聊 +20（他在等）
      quiet：她在安睡时段 -12
    候选集仍是 tier=="ask" 或 after_mine，与旧实现一致；
    _MIN_SALIENCE 只挡“极旧 + 静夜”这种明显不值得的，不做额外筛选。
    """
    from datetime import datetime as _dt
    now = now or _dt.now()
    tier = str(cell.get("tier") or "")
    after = bool(cell.get("after_mine") or [])
    if tier == "ask":
        need = 40.0
    elif after:
        need = 12.0
    else:
        return 0.0, {"need": 0.0, "fresh": 0.0, "wait": 0.0, "owner": 0.0, "quiet": 0.0}
    age = _age_min(str(cell.get("last_at") or ""), now)
    fresh = 30.0 * (0.5 ** (age / 20.0)) if age < 1e6 else 0.0
    wait = min(20.0, max(0.0, (age - 10.0) * 0.5)) if after else 0.0
    chat_id = str(cell.get("chat_id") or "")
    owner = 20.0 if _is_owner_chat(chat_id) else 0.0
    quiet = 0.0
    try:
        from loop import selfstate
        quiet = -12.0 if selfstate.in_quiet_hours() else 0.0
    except Exception as exc:  # noqa: BLE001
        from safelog import warn
        warn("run_quiet_probe_failed", error=repr(exc)[:80])
    parts = {"need": need, "fresh": fresh, "wait": wait, "owner": owner, "quiet": quiet}
    return sum(parts.values()), parts


def pick_next(snap: dict, *, taken=(), now=None) -> tuple[str, dict]:
    """挑下一个该看的会话（一次挑一个；一拍看几个由调用方定）。"""
    from datetime import datetime as _dt
    now = now or _dt.now()
    best_key, best = "", None
    for cell in (snap.get("cells") or []):
        key = str(cell.get("chat_id") or cell.get("label") or "")
        if not key or key in taken:
            continue
        score, parts = salience(cell, now=now)
        if score < _MIN_SALIENCE:
            continue
        if best is None or score > best["score"]:
            best_key, best = key, {"chat": key, "score": round(score, 2),
                                   "age_min": round(_age_min(str(cell.get("last_at") or ""), now), 1),
                                   "tier": str(cell.get("tier") or ""), "parts": parts}
    if best is None:
        return "", {}
    return best_key, best


def _window_ids(w: str, cells) -> tuple:
    """这一窗的 (platform, chat_id)。

    2026-10-31：原来只从快照 cells 里找，cells 一空就都成了 ("","")，
    于是群聊永远套用私聊那套渠道约定（群里不 @、连发、还发语音）。
    现在 cells 里找不到就按 chat_id 的形态判：微信群 …@chatroom，QQ 群 group:…，
    微信私聊 wxid_…/filehelper…，QQ 私聊纯数字。判不出来才留空（走 DEFAULT）。
    """
    _w = str(w or "").strip()
    for c in cells or []:
        ids = (str(c.get("chat_id") or ""), str(c.get("label") or ""))
        if _w and _w in ids:
            _c0 = str(c.get("chat_id") or "") or _w
            _p0 = str(c.get("platform") or "")
            if not _p0:                      # cells 有这条但没写平台：按 chat_id 形态补
                if _c0.endswith("@chatroom"):
                    _p0 = "wechat"
                elif _c0.startswith("group:"):
                    _p0 = "qq"
                elif _c0.startswith("wxid_") or _c0 == "filehelper":
                    _p0 = "wechat"
                elif _c0.isdigit():
                    _p0 = "qq"
            return _p0, _c0
    k = str(w or "").strip()
    if k.endswith("@chatroom"):
        return "wechat", k
    if k.startswith("group:"):
        return "qq", k
    if k.startswith("wxid_") or k == "filehelper":
        return "wechat", k
    if k.isdigit():
        return "qq", k
    return "", ""


def _quiet_followup_scope(followup: dict | None, db: str, cells) -> tuple[str, ...]:
    """在刚取到的快照中确认原会话仍然安静。"""
    scope = (followup or {}).get("scope")
    if not db or not isinstance(scope, (tuple, list)) or len(scope) != 3 or not all(scope):
        return ()
    platform, account_id, chat_id = scope
    if goal_review.database_scope(db) != (platform, account_id) or followup.get("target") != chat_id:
        return ()
    cell = next((item for item in cells if (item.get("platform"), item.get("chat_id")) == (platform, chat_id)), None)
    if cell is None or any(not intention_close._flag_of(row, "is_self") for row in cell.get("after_mine") or ()):
        return ()
    return tuple(scope)


class _FeedbackRepo:
    """给 feedback 采集器用的最小 repo：只读这一拍的账号库，取某会话最近的消息。

    为什么不用 core.bootstrap.get_account_repo()：引擎一拍只对着一个账号库
    （ports.pick_db()），而 get_account_repo() 给的是「默认账号」——多账号时会读错库。
    这里自己开只读连接，错不了账号，也不会写。
    """

    def __init__(self, db_path: str) -> None:
        self._db = db_path
        self.messages = self   # 采集器调用 repo.messages.get_by_chat(...)

    def get_by_chat(self, chat_id: str, limit: int = 12):
        try:
            con = ports._connect(self._db)
        except Exception as exc:   # 库被占/权限：这一拍不采信号，但绝不让它拖垮回合
            warn("feedback_db_unavailable", db=str(self._db), error=repr(exc)[:80])
            return []
        try:
            cur = con.execute(
                "select id, chat_id, content, is_self from messages "
                "where chat_id = ? order by created_at desc limit ?",
                (chat_id, int(limit)),
            )
            # 采集器 _scan_one 的契约：它从 reversed(history) 里取**第一条**非自己的话
            # 当"上一句"——所以这里必须按时间**升序**给它（它假定 repo 给的是旧→新）。
            # 仓库里 db.repo_messages.get_by_chat 给的是 desc，直接传会让它把最老的一句
            # 当成"上一句"，repeat 几乎永远判不出来（2026-11-04 实测 0 条）。
            return [{"msg_id": str(r["id"]), "chat_id": r["chat_id"],
                     "content": (r["content"] or ""), "is_self": bool(r["is_self"])}
                    for r in list(cur)[::-1]]
        except Exception as exc:
            warn("feedback_history_failed", chat_id=str(chat_id), error=repr(exc)[:80])
            return []
        finally:
            try:
                con.close()
            except Exception as exc:
                warn("feedback_close_failed", db=str(self._db), error=repr(exc)[:60])


def _speak_allowed(platform: str, chat_id: str) -> bool:
    """这个会话现在能不能开口。读不到策略就按放行（不能因为读配置把她锁死）。"""
    try:
        from loop import speak_policy
        return bool(speak_policy.allowed(platform, chat_id)[0])
    except Exception:  # noqa: BLE001
        return True


def _cell_addressing(cell: dict | None, *, resumed_row=None) -> dict:
    """供主动唤醒和分窗回合复用；每个结论只属于提供消息的会话。"""
    if not cell:
        return {}
    from loop import addressing
    try:
        verdict = (addressing._with_request("named", resumed_row, "之前有一条点名还没接上", 1)
                   if resumed_row is not None else addressing.verdict(
            cell.get("after_mine") or [], is_private=cell.get("chat_type") != "group",
            my_last_at=cell.get("my_last_at"), peer_before_mine=cell.get("peer_before_mine")))
        addressed = {
            "chat_id": str(cell.get("chat_id") or ""), "kind": verdict.kind,
            "why": verdict.why, "who": verdict.who, "n": verdict.n,
            "short": verdict.short(), "must_answer": verdict.must_answer,
            "request_text": verdict.request_text, "request_row_id": verdict.request_row_id,
        }
        if verdict.request_context_row_ids:
            addressed["request_context_row_ids"] = verdict.request_context_row_ids
        if verdict.other_named_requests:
            addressed["other_named_requests"] = [
                {"who": item.who, "request_text": item.request_text, "request_row_id": item.request_row_id}
                for item in verdict.other_named_requests
            ]
        # 2026-11-24：能读不能说的会话（不在说话白名单）不许再判「必答」——
        # 11-20 实测 6 次 say_failed:not_in_allowlist，还有 1 次 rest 被 required_reply_pending 挡住。
        _plat = str(cell.get("platform") or "")
        if _plat and not _speak_allowed(_plat, str(cell.get("chat_id") or "")):
            addressed["must_answer"] = False
            addressed["read_only"] = True
        return addressed
    except Exception as exc:
        warn("addressing_failed", chat_id=cell.get("chat_id"), err=repr(exc))
        return {}


def _feedback_rows(rows) -> list:
    """把这一拍新到的话转成反馈采集器认的形状（Row 的字段名和它要的不一样）。"""
    out = []
    for r in rows or ():
        out.append({
            "chat_id": str(getattr(r, "chat_id", "") or ""),
            "chat_type": str(getattr(r, "chat_type", "") or ""),
            "msg_id": str(getattr(r, "msg_id", "") or ""),
            "sender_id": str(getattr(r, "speaker", "") or ""),
            "content": str(getattr(r, "text", "") or getattr(r, "raw", "") or ""),
            "is_self": bool(getattr(r, "is_self", 0)),
        })
    return out


def _recall_query(fresh, review_items) -> str:
    """这一窗的检索输入（B1）：真实触发文本，带说话人；没有新消息时用目标线索。

    只取同一窗、非自己发的入站行，不做跨窗拼接。refs 由调用方单独传给 run_loop
    作为来源记录，不参与匹配。
    """
    candidates: list[tuple[str, str]] = []   # (speaker 前缀, 正文)
    for row in (fresh or [])[:4]:
        if getattr(row, "is_self", False):
            continue
        text = str(getattr(row, "text", "") or "").strip()
        if not text:
            continue
        who = str(getattr(row, "speaker", "") or "").strip()
        candidates.append((("%s: " % who) if who else "", text))
    if not candidates and review_items:
        try:
            from loop import goal_review

            candidates.append(("", goal_review.query_hint(review_items)))
        except Exception as exc:  # noqa: BLE001 - 线索取不到就按无 query 处理
            warn("recall_query_hint_failed", err=repr(exc)[:120])
    if not candidates:
        return ""
    # 按候选人数公平分配 200 字预算：单人拿满，N 人每人约 200 // N；
    # 每人至少留 speaker 前缀 + 24 字正文。截断只发生在正文，
    # 前缀永远完整，长消息不会把后面的人整条挤掉（总和仍超时用 [:200] 兜底）。
    per = max(200 // len(candidates), 1)
    parts = [prefix + body[:max(24, per - len(prefix))] for prefix, body in candidates]
    return " ".join(parts)[:200]


def _request_recall_sources(addressed: dict, fresh_sources: list, current_sources: list) -> list:
    """Keep the active message first and reuse the group's verified continuation context."""
    context_ids = list(addressed.get("request_context_row_ids") or ())
    # 另一位点名的人也要召回自己的相关记忆
    context_ids += [
        item.get("request_row_id") for item in (addressed.get("other_named_requests") or ())
        if item.get("request_row_id")
    ]
    if not current_sources or not context_ids:
        return current_sources
    wanted = {str(row_id) for row_id in context_ids}
    out = list(current_sources)
    seen = {str(getattr(row, "msg_id", "") or "") for row, _ in out}
    for row, ref in fresh_sources:
        msg_id = str(getattr(row, "msg_id", "") or "")
        if msg_id in wanted and msg_id not in seen:
            seen.add(msg_id)
            out.append((row, ref))
    return out


def _fresh_message_sources(rows, scope) -> list:
    """给窗口里的新消息附上同一账号和会话的证据编号。"""
    pairs = [(row, intention_close._evidence_ref(row, scope))
             for row in (rows or ()) if not intention_close._flag_of(row, "is_self")]
    # 2026-11-21：有真人说话、却一条编号都没解析出来，等于整条认知链静默熄火
    # （线上实况：inbound_event.platform_msg_id 全空，回查只能拿本地行号去撞事件流的
    #  平台消息号，永远撞不上 —— 评价/情绪/经验因此一次都没启动过，却没有任何日志）。
    if pairs and not any(ref for _, ref in pairs) and once(
            "evidence_refs_unresolved:" + str(getattr(scope, "chat_id", "")), every_s=1800):
        warn("evidence_refs_unresolved", chat_id=str(getattr(scope, "chat_id", "")),
             rows=len(pairs),
             platform_msg_id_empty=sum(1 for row, _ in pairs
                                       if not str(getattr(row, "platform_msg_id", "") or "")))
    return pairs


async def _resume_group_request(db: str, scope, *, now: datetime) -> tuple | None:
    """Read a still unanswered recent row without waking the loop or granting authority."""
    from datetime import timedelta
    from loop import addressing
    from core.memory.belief_commit import evidence, evidence_state

    if not scope.complete or goal_review.database_scope(db) != scope.key[:2]:
        return None
    floor = (now - timedelta(seconds=addressing.FOLLOW_WINDOW_S)).isoformat(sep=" ", timespec="seconds")
    rows = ledger.read_receipts(limit=160, day=now.date().isoformat())
    if floor[:10] != now.date().isoformat():
        rows = ledger.read_receipts(limit=160, day=floor[:10]) + rows
    attempts = set()
    pending = set()
    for receipt in reversed(rows):
        if (receipt.get("act") not in brain.REPLY_ACTS or receipt.get("target") != scope.chat_id
                or receipt.get("result") not in {"sent", "queued"}
                or not ledger._receipt_in_scope(receipt, scope.key)):
            continue
        if str(receipt.get("at") or "") < floor:
            break
        replied = receipt.get("reply_to_row_ids") or []
        candidates = receipt.get("reply_candidate_row_ids") or []
        resumed = receipt.get("reply_resumed_row_id")
        if any(not isinstance(ids, list) or any(not isinstance(row_id, str) for row_id in ids)
               for ids in (replied, candidates)):
            break
        if not (replied or resumed):
            break  # A later unbound reply may already have handled the old question.
        attempts.update(replied)
        if isinstance(resumed, str) and resumed:
            attempts.add(resumed)
        pending.update(candidates)
    remaining = pending - attempts
    if not remaining:
        return None
    rows = await ports.recent_in_chat(db, scope.chat_id, limit=24)
    newer_speakers = set()
    for row in sorted(rows, key=lambda item: (item.at, item.msg_id), reverse=True):
        if row.at < floor:
            break
        if row.chat_id != scope.chat_id or row.is_self or not row.sender_id:
            continue
        if str(row.msg_id) in remaining and row.sender_id not in newer_speakers:
            text = addressing._request_text(row, full=True)
            ref = intention_close._evidence_ref(row, scope)
            proof = evidence(ref, scope, frozenset({ref})) if ref else None
            if (text and not addressing.explicit_reply_pause(text) and proof
                    and (proof.actor_account_id or proof.actor_person_id) == row.sender_id
                    and evidence_state(ref, scope) == "alive"):
                return row, ref
        newer_speakers.add(row.sender_id)
    return None


async def one_tick(*, dry_run: bool, llm, wake: dict | None = None) -> dict:
    """一拍 = 选窗口 + 给每个被选中的会话各开一个回合（2026-10-28 替换「一份总览跑一次」）。"""
    db = (wake or {}).get("review_db") or ports.pick_db()
    # 续话只属于产生它的平台/账号/会话；其余窗口的 wake 里不许带它。
    _base_wake = dict(wake or {})
    _fu = _base_wake.pop("followup", None)
    _HB_PHASE["phase"] = "building"      # 这一句之后到第一行心跳之间可能好几分钟
    _hb_beat_once()
    snap = snapshot.build_snapshot(db)
    _HB_PHASE["phase"] = "windows"
    text = snapshot.render_text(snap)
    fp = _fingerprint(text)
    windows: list[str] = []
    picks: list[dict] = []
    _now = datetime.now()
    while True:   # 2026-10-31：主人要求取消上限（原 while len(windows) < _ATTEND_MAX）——一拍的会话全处理
        _k, _d = pick_next(snap, taken=tuple(windows), now=_now)
        if not _k:
            break
        windows.append(_k)
        picks.append(_d)
    for cell in snap.get("cells") or []:
        platform = str(cell.get("platform") or "")
        chat = str(cell.get("chat_id") or "")
        if goal_review.for_window((wake or {}).get("goal_reviews") or [], db=db, platform=platform, chat_id=chat):
            window = chat
            if window and not any(_window_ids(existing, snap.get("cells") or []) == (platform, chat) for existing in windows):
                windows.append(window)
    cells = snap.get("cells") or []
    _fu_scope = _quiet_followup_scope(_fu, db, cells)
    if _fu_scope and not any(_window_ids(w, cells) == (_fu_scope[0], _fu_scope[2]) for w in windows):
        windows.append(_fu_scope[2])
    if not windows:
        windows = [""]
    _talkers = [str(c.get("label") or c.get("chat_id") or "")
                for c in (snap.get("cells") or []) if (c.get("after_mine") or [])]
    receipts: list[dict] = []
    _tl: list[tuple] = []      # 2026-10-31：每一窗花了多久（主人问"回复为什么慢"）
    _tick_since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")   # 2026-10-28 ③ 去重起点（台账里按它筛）
    _tk0 = duration.now_ms()
    for _i, _w in enumerate(windows, 1):
        _write_heartbeat({**_ALIVE, "pid": os.getpid(), "busy": True,
                          "window": {"i": _i, "n": len(windows), "name": _w},
                          "at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        _tw0 = duration.now_ms()
        _plat, _cid = _window_ids(_w, snap.get("cells") or [])
        # back=False：回看行不在这里出，交给下面的 [原文] 段（同一批话不重复）
        _txt = snapshot.render_text(snap, only=_w, back=False) if _w else text
        # 2026-10-31 Step 5 接线：同一窗、同一次取数，世界带注释、原文不带。
        # 2026-10-31：条数不卡了，按字数自适应（TURNS_BUDGET_CHARS=1500）
        _turns = snapshot.render_turns(snap, _w, limit=0) if _w else ""
        if _turns:
            _turns = "[原文]（这一窗，按时间顺序，不带注释）\n" + _turns
        _fresh: list = []
        # 2026-11-04：这一窗的无条件读数（下面各处把结果填进来）。
        # 之前只在「有结果」时才打日志，于是 feedback_recorded=0 分不清
        # 「没信号」还是「这段代码根本没跑到」——loop.run 这个 logger 只有启动行。
        _fresh_n = 0
        _fb_n = 0
        _ds_orders = 0
        _ds_sent = 0
        # 2026-11-03 P1-1：先划账。这一窗新到的话里若有人说「办完了」，把她欠着的
        # 那条关掉（纯本地、零外发；开关 YINYUE_FEATURE_OPEN_LOOP_CLOSE，默认 off）。
        _cell = next((c for c in (snap.get("cells") or []) if _cid and (c.get("chat_id") or c.get("label")) == _cid), None)
        try:
            _fresh = list((_cell or {}).get("after_mine") or [])
            _fresh_n = len(_fresh)
            if _fresh:
                # 关闭要写「属于哪个会话」的完成事实：身份只在回合现场有，缺了就不写。
                from core.context.cognitive_commit import Scope as _CogScope

                _chat_type = str((_cell or {}).get("chat_type") or "")
                _is_group = _chat_type == "group" or str(_cid or "").endswith("@chatroom")
                _scope = _CogScope(
                    platform=str(_plat or ""),
                    account_id=os.path.basename(os.path.dirname(db)),
                    chat_id=str(_cid or ""),
                    chat_type=_chat_type or ("group" if _is_group else "private"),
                    group_id=str(_cid or "") if _is_group else "",
                )
                intention_close.close_from_rows(_cid, _fresh, scope=_scope)
            # 2026-11-04：把这一拍新到的话交给反馈采集器。它原来挂在已删的
            # agents/conversational.py 上（10-24 之后一条样本都没进过），于是
            # 「被重复问同一句」「主人纠正」两种信号没有数据，digest 永远是 0。
            # 只读账号库（不猜账号）+ 写 meta 的反馈样本表；失败一律吞掉，不影响这一拍。
            _fb = _feedback_rows(_fresh)
            if _fb:
                _n = _feedback.record_feedback_signals(
                    _fb, repo=_FeedbackRepo(db), platform=_plat or "",
                    account_id=os.path.basename(os.path.dirname(db)))
                _fb_n = int(_n or 0)
                if _fb_n:
                    log.info("feedback_recorded chat_id=%s n=%s", _cid or "-", _fb_n)
        except Exception:
            warn("intention_close_tick_failed", err=repr(__import__("sys").exc_info()[1]))
        # 2026-11-04 P3-5「点名即发」：主人在私聊里点名「在 X 群里说一句 …」时，直接投进
        # 消息队列发出去。这条原是 core/jobs/direct.py（jobs 执行链随旧引擎一起删了，它成了
        # 孤儿，唯一调用方 agents/conversational.py 也没了），今天重写成 loop/direct_send.py：
        # 只认私聊指令、只认主人（WECHAT_OWNER_WXIDS）、每条消息只发一次、认不出会话名就不发。
        # 开关 YINYUE_FEATURE_DIRECT_SEND（off/shadow/on，默认 off）；复用 outbound_queue，零新增写路径。
        try:
            # db 传进去才能按 msg_id 回查真实 sender_id：snapshot 把 speaker 换成了显示名
            _ds = await direct_send.run(_fresh, chat_id=_cid, platform=_plat or "",
                                        owners=direct_send.owner_ids(), db=db)
            _ds_orders = int(_ds.get("orders") or 0)
            _ds_sent = int(_ds.get("sent") or 0)
            if _ds_orders:
                log.info("direct_send_done chat_id=%s mode=%s orders=%s sent=%s",
                         _cid or "-", _ds.get("mode"), _ds_orders, _ds_sent)
            _dsq = direct_send.check_deliveries()
            if _dsq.get("checked"):
                log.info("direct_send_checked checked=%s local_observed=%s failed=%s pending=%s",
                         _dsq.get("checked"), len(_dsq.get("local_observed") or []),
                         len(_dsq.get("failed") or []), len(_dsq.get("pending") or []))
        except Exception:
            warn("direct_send_tick_failed", err=repr(__import__("sys").exc_info()[1]))
        # 2026-11-04：每窗一行，无条件。用途只有一个——让「0」有语义：
        # fresh=0 是这一窗没有新话；fb=0 是采集器跑了但没信号；
        # orders=0 是这条窗不是「主人私聊点名」；sent=0 是有指令但一条没发出去。
        log.info("window_done chat_id=%s platform=%s fresh=%s fb=%s orders=%s sent=%s",
                 _cid or "-", _plat or "-", _fresh_n, _fb_n, _ds_orders, _ds_sent)
        from core.context.cognitive_commit import Scope as _EvidenceScope
        _evidence_scope = _EvidenceScope(_plat, os.path.basename(os.path.dirname(db)), _cid)
        _fresh_sources = _fresh_message_sources(_fresh, _evidence_scope)
        if _fresh_sources and not dry_run:
            try:
                intention_close.cancel_quoted_requests(_evidence_scope, _fresh_sources)
            except Exception as exc:
                warn("quoted_goal_cancel_failed", chat_id=_cid, err=repr(exc)[:120])
        _review_items = goal_review.for_window((wake or {}).get("goal_reviews") or [], db=db, platform=_plat, chat_id=_cid)
        _window_wake = {**_base_wake, **_wake_facts((wake or {}).get("last_tick_ms", 0), _cell),
                        "addressed": _cell_addressing(_cell),
                        "goal_reviews": _review_items, "goal_review_only": bool(_review_items and not _fresh)}
        if _fu_scope == _evidence_scope.key:
            _window_wake["followup"] = {key: value for key, value in _fu.items() if key != "scope"}
        _window_wake.pop("resumed_request_row_id", None)
        # 待办只进入产生这条读数的平台、账号和会话。
        _job_scope = _window_wake.pop("job_scope", None)
        if not (_evidence_scope.complete and isinstance(_job_scope, (tuple, list))
                and tuple(_job_scope) == _evidence_scope.key):
            _window_wake.pop("job", None)
            _window_wake.pop("job_line", None)
        _window_wake["inbound_row_ids"] = [str(msg_id) for row, _ in _fresh_sources
                                           if (msg_id := getattr(row, "msg_id", ""))]
        _visible_refs = tuple(ref for _, ref in _fresh_sources if ref)
        _addressed = _window_wake["addressed"]
        _resumed = None
        if (_fresh_sources and (_cell or {}).get("chat_type") == "group"
                and _cell.get("my_last_at") and not _addressed.get("kind")
                and (not (wake or {}).get("last_tick_ms") or _window_wake["peers"])
                and not _review_items and not (wake or {}).get("spontaneous")):
            try:
                _resumed = await _resume_group_request(db, _evidence_scope, now=_now)
            except Exception as exc:
                warn("group_request_resume_failed", chat_id=_cid, err=repr(exc)[:120])
            if _resumed:
                _addressed = _cell_addressing(_cell, resumed_row=_resumed[0])
                _window_wake["addressed"] = _addressed
                _window_wake["resumed_request_row_id"] = _addressed["request_row_id"]
                _visible_refs = (*_visible_refs, _resumed[1])
        _request_id = _addressed.get("request_row_id")
        _decision_sources = _fresh_sources
        if _request_id:
            _decision_sources = [(row, ref) for row, ref in _fresh_sources
                                 if str(getattr(row, "msg_id", "") or "") == _request_id]
        if _resumed:
            _decision_sources = [_resumed]
        _current_row, _current_ref = max(
            _decision_sources, key=lambda pair: (getattr(pair[0], "at", ""), getattr(pair[0], "msg_id", 0) or 0),
            default=(None, ""),
        )
        _appraisal_anchor = (_current_ref, getattr(_current_row, "quote_id", ""))
        _recall_sources = _request_recall_sources(_addressed, _fresh_sources, _decision_sources)
        # 主请求之后召回的他人点名也要各自成为独立评价候选。
        _appraisal_related_refs = tuple(ref for _, ref in _recall_sources[len(_decision_sources):] if ref)
        try:
            _r = await asyncio.wait_for(
                brain.run_loop(db, _txt, snap_turns=_turns, llm=llm, dry_run=dry_run, wake=_window_wake, focus=_w, platform=_plat, chat_id=_cid,
                               sender_id="" if _resumed else ports.turn_sender(_fresh, _cid),
                               visible_event_ids=_visible_refs, tick_since=_tick_since,
                               recall_query=_recall_query([row for row, _ in _recall_sources], _review_items),
                               recall_refs=tuple(ref for _, ref in _recall_sources if ref),
                               appraisal_anchor=_appraisal_anchor,
                               appraisal_context_refs=tuple(ref for _, ref in _recall_sources[len(_decision_sources):] if ref),
                               appraisal_related_refs=_appraisal_related_refs,
                               others=[x for x in _talkers if x and x != _w]),
                timeout=duration.sleep_s(TICK_TIMEOUT_MS))
        except asyncio.TimeoutError:
            warn("run:65 asyncio.TimeoutError", err=repr(__import__("sys").exc_info()[1]))
            ledger.append_receipt(act="timeout", target=_w, why="这一拍超时，先停下",
                                 result="timeout", blocked_reason="Tick 超时")
            _r = [{"act": "timeout"}]
        goal_review.finish(_review_items, _r or [], dry_run=dry_run, at=datetime.now().isoformat(sep=" ", timespec="seconds"))
        _tl.append((_w or "-", round(duration.epoch_s(duration.now_ms() - _tw0), 1), len(_r or [])))
        receipts.extend(_r or [])
    _asks = sum(1 for c in (snap.get("cells") or []) if c.get("tier") == "ask")
    return {"at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "fingerprint": fp,
            "receipts": receipts, "world": text, "windows": windows, "timings": _tl,
            "pending": max(0, _asks - len([w for w in windows if w])),
                "picks": picks}


_SINGLETON_PORT = 39871   # 本机专用：谁先 bind 上谁是唯一引擎
_SINGLETON_SOCK = None


_MUTEX_NAME = "Global\\yinyue_loop_engine"
_mutex_handle = None


def _acquire_mutex() -> bool:
    """Windows 命名互斥体单实例。

    2026-10-24 两次踩坑后的定论：
    · 文件锁：锁文件被删/重建就失效（曾出现 4 个引擎并存）；
    · 端口独占：进程被杀后留 TIME_WAIT，导致"刚杀掉想立刻重启"反而绑不上。
    命名互斥体由内核持有，进程一死立刻释放，两个毛病都没有。
    """
    global _mutex_handle
    try:
        import ctypes

        # 2026-10-24：windll 不设置 ctypes 的 last-error，GetLastError() 恒为 0，
        # 于是"互斥体判重"从来没生效过（出现过两个引擎同时跑）。改用 use_last_error。
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
        err = ctypes.get_last_error()
        if not handle:
            return True          # 建不出来就放行，别把引擎拦死在门口
        if err == 183:           # ERROR_ALREADY_EXISTS：已有引擎持有
            return False
        _mutex_handle = handle
        return True
    except Exception:            # noqa: BLE001 - 非 Windows 或 ctypes 缺失
        warn("run:104 Exception", err=repr(__import__("sys").exc_info()[1]))
        return True


def _acquire_singleton():
    if not _acquire_mutex():
        # 2026-10-24：原来这里 return False，而 main() 只判 `is None`，
        # 于是"判重"形同虚设 → 两个引擎同时跑、双拍双发。
        return None
    """独占锁：同一台机器只允许一个引擎实例（重复启动会双拍双发）。

    2026-10-24：原来只用 msvcrt 文件锁，结果出现过 4 个引擎并存——
    因为锁文件一旦被删/被重建，两个进程锁的就不是同一个对象了。
    现在主判据改成「绑一个本机端口」：端口是内核级独占，删不掉、抢不走；
    文件锁退化成"记录 pid"的辅助信息。
    """
    global _SINGLETON_SOCK
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Windows 上必须用 EXCLUSIVEADDRUSE：SO_REUSEADDR=1 会让第二个进程也绑得上
        # 同一端口（实测两个引擎"都成功"）。单实例真正的判据是上面的互斥体。
        try:
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        except OSError:
            warn("run:129 OSError", err=repr(__import__("sys").exc_info()[1]))
        s.bind(("127.0.0.1", _SINGLETON_PORT))
        s.listen(64)  # 2026-10-24：backlog 太小会让端口探测偶尔被拒，看门狗误判引擎已死
        # 2026-10-24：只 listen 不 accept，待处理连接会把 backlog 占满，
        # 于是"探测端口"的连接被拒 → 看门狗以为引擎死了，每分钟白拉一次进程。
        # 这里起一条小线程，来一个连就立刻关掉，让端口探测永远通。
        import threading

        def _start_accept_loop(sock):
            def _loop():
                while True:
                    try:
                        conn, _ = sock.accept()
                        try:
                            conn.close()
                        except OSError:
                            warn("run:accept-close OSError", err=repr(__import__("sys").exc_info()[1]))
                    except OSError:
                        warn("run:accept OSError", err=repr(__import__("sys").exc_info()[1]))
                        return
            threading.Thread(target=_loop, name="loop-accept", daemon=True).start()

        _start_accept_loop(s)
    except OSError:
        warn("run:133 OSError", err=repr(__import__("sys").exc_info()[1]))
        s.close()
        return None
    _SINGLETON_SOCK = s
    try:
        os.makedirs("logs", exist_ok=True)
        with open(os.path.join("logs", "loop_engine.lock"), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        warn("run:141 OSError", err=repr(__import__("sys").exc_info()[1]))
    return s


def _pend_due(at_ms: int | None = None) -> bool:
    """还有没轮到的窗口、且已经过了退避间隔 —— 该主动再拍一次了。

    2026-10-31：这一条以前不存在。`last_fp = "__pending__"` 原本靠"世界指纹变了"
    触发重试，可待办窗口本身不会改世界，于是主循环在 idle 分支就 continue 掉了：
    第 4 个该回的会话永远等不到下一拍（10-30 23:02 那句「在吗」就是这么悬着的）。
    """
    now = duration.now_ms() if at_ms is None else at_ms
    return _PEND_TRIES[0] > 0 and now - _PEND_LAST[0] >= _PEND_GAP_MS


def _advance_snapshot_jobs(snap: dict, db: str, *, dry_run: bool) -> tuple[dict, str, tuple[str, ...]]:
    """先处理已经到达的明确取消，再把仍在手的任务推进一步。

    第三个返回值是这次读数的来源 Scope.key：每一拍的 job/job_line 只属于
    那一个会话，调用方据此决定能不能把它们带进别的窗口的 prompt。
    """
    if dry_run:
        return {}, "", ()

    job: dict = {}
    job_line = ""
    job_scope: tuple[str, ...] = ()
    try:
        from core.context.cognitive_commit import Scope
        from loop import job_bridge

        platform, account_id = goal_review.database_scope(db)
        if not platform or not account_id:
            return {}, "", ()
        cells = snap.get("cells") or []
        owner_windows = [
            (cell, Scope(platform, account_id, chat_id))
            for cell in cells
            if (cell.get("platform") in (None, "", platform)
                and (chat_id := str(cell.get("chat_id") or "")) and _is_owner_chat(chat_id))
        ]
        for cell, scope in owner_windows:
            try:
                sources = _fresh_message_sources(cell.get("after_mine"), scope)
                if sources:
                    intention_close.cancel_quoted_requests(scope, sources)
            except Exception as exc:
                warn("quoted_goal_cancel_failed", chat_id=scope.chat_id, err=repr(exc)[:120])
        for _c in cells:
            _rows2 = list(_c.get("after_mine") or [])
            if not _rows2:
                continue
            _lastr = _rows2[-1]
            if int(getattr(_lastr, "is_self", 0) or 0):
                continue
            _opened = job_bridge.open_from_owner(
                str(_c.get("chat_id") or ""),
                str(getattr(_lastr, "text", "") or ""),
                msg_id=str(getattr(_lastr, "msg_id", "") or ""),
                source_ref=str(getattr(_lastr, "event_id", "") or ""),
                scope={
                    "platform": platform,
                    "account_id": account_id,
                    "chat_id": str(_c.get("chat_id") or ""),
                },
                at=str(getattr(_lastr, "occurred_at", "") or ""),
            )
            if _opened.get("ok"):
                log.info("job_open_from_tick job_id=%s chat=%s",
                         _opened.get("job_id", ""), str(_c.get("chat_id") or ""))
                break
            if _opened.get("why") not in ("", "not_owner_private", "not_an_order", "no_proof"):
                log.info("job_open_refused why=%s chat=%s",
                         _opened.get("why", ""), str(_c.get("chat_id") or ""))
        for _, scope in owner_windows:
            readout = job_bridge.readout(scope.chat_id, scope=scope)
            if not readout:
                continue
            advanced = job_bridge.advance_once(scope.chat_id, scope=scope)
            if advanced.get("stop") == "goal_closed" and not advanced.get("ran"):
                continue
            job, job_line, job_scope = advanced, readout, scope.key
            if job.get("job_id"):
                break
    except Exception as _exc:  # noqa: BLE001 - 办不成事不许把主循环带崩
        log.warning(f"job_bridge_tick_failed: err={repr(_exc)[:120]}")
        job = {}
        job_line = ""
        job_scope = ()
    return job, job_line, job_scope


def main() -> int:
    # The scheduler may have inherited its environment before this flag was added.
    from dotenv import dotenv_values

    review_key = "YINYUE_GOAL_REVIEW_WAKE"
    review_setting = dotenv_values(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"), interpolate=False).get(review_key)
    if review_setting is not None:
        os.environ.setdefault(review_key, review_setting)
    try:
        from core.infra import awake as _awake
        _awake.keep_system_awake("loop.run")   # 关屏可以，别睡
        log.info("loop_keep_awake on=%s", _awake.is_on())
    except Exception as _exc:  # noqa: BLE001
        log.warning(f"loop_keep_awake_failed: err={_exc!r}")
    # 2026-11-01：本进程是服务入口，会往**账号库**写东西（媒体描述写回、媒体行解析）。
    # 不声明的话 db.write_guard 会拒绝生产路径写入，而失败只留一行 debug —— 表现是
    # "描述算出来了、行上永远 NULL、图像屏障每 15 分钟白等 20 秒再跳拍"。
    try:
        from db.write_guard import declare_writes_allowed

        declare_writes_allowed(declared_by="loop.run")
        log.info("loop_writes_declared by=loop.run")
    except Exception as _exc:  # noqa: BLE001
        log.warning(f"loop_writes_declare_failed: err={_exc!r}")
    _lock = _acquire_singleton()
    if not _lock:
        _holder = ""
        try:
            with open(os.path.join("logs", "loop_engine.lock"), encoding="utf-8") as _fh:
                _holder = _fh.read().strip()
        except OSError:
            pass
        print("[loop.run] 已有引擎在跑（127.0.0.1:39871 被占用%s），本实例退出。"
              % ("，对方 pid=%s" % _holder if _holder else ""))
        return 0
    _log()
    dry_run = "--dry" in sys.argv or "--send" not in sys.argv
    once = "--once" in sys.argv
    llm = ports.get_llm()

    if once:
        out = asyncio.run(one_tick(dry_run=dry_run, llm=llm))
        print(json.dumps(out, ensure_ascii=False, indent=2)[:4000])
        return 0

    last_fp = ""
    last_tick = 0
    last_stamp = None
    last_hb = 0
    ticks = 0
    started = duration.now_ms()
    print("[loop.run] 已启动 pid=%d dry_run=%s poll=%s" % (os.getpid(), dry_run, duration.human(POLL_MS)), flush=True)
    # 2026-10-27：启动即写一次心跳。原先只在一拍跑完才写，
    # 于是重启后到第一拍之间，面板读到的还是已经死掉的旧 pid，看起来像「引擎挂了」。
    _write_heartbeat({"pid": os.getpid(), "uptime_ms": 0,
                      "dry_run": dry_run, "ticks": 0, "last_fp": "",
                      "last_tick_at": "",
                      "poll_ms": POLL_MS, "idle": True, "waiting": "starting",
                      "at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    _hb_stop = threading.Event()
    threading.Thread(target=_hb_keepalive, args=(_hb_stop,), name="hb-keepalive", daemon=True).start()
    while True:
        _housekeep()
        if os.path.exists(STOP_FILE):
            try:
                os.remove(STOP_FILE)
            except OSError:
                warn("run:172 OSError", err=repr(__import__("sys").exc_info()[1]))
            print("[loop.run] 收到停止哨兵，退出", flush=True)
            return 0
        try:
            stamp = ports.world_stamp() + (media_ref.desc_version(),)
            # 2026-10-28（PIT-111）：这个 4 秒只决定「这一拍要不要跑」，不是发车间隔。
            # 发车间隔只有一个来源：loop/ledger.py 里的 quiet_ms；别在这里另存时间戳。
            cooled = (duration.now_ms() - last_tick) >= MIN_TICK_INTERVAL_MS
            # 2026-10-24 修的坑：以前不管冷却，先把 last_mtime 更新掉再判 cooled，
            # 于是「冷却期内到来的那条消息」被永久吞掉（用户 17:17 发的那条就这样丢了）。
            # 现在：冷却没到就不消费这次变化，下一圈继续看。
            if stamp != last_stamp and not cooled:
                if duration.now_ms() - last_hb >= _HB_EVERY_MS:
                    last_hb = duration.now_ms()
                    _write_heartbeat({"pid": os.getpid(), "uptime_ms": duration.now_ms() - started,
                                              "dry_run": dry_run, "ticks": ticks, "last_fp": last_fp,
                                              "last_tick_at": datetime.fromtimestamp(duration.epoch_s(last_tick)).strftime("%H:%M:%S") if last_tick else "",
                                              "poll_ms": POLL_MS, "idle": True, "waiting": "cooling",
                                              "at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                time.sleep(duration.sleep_s(POLL_MS))
                continue
            # 2026-10-25 心跳：世界没变，也可能因为她自己这会儿想说话而起一拍。
            # 理由在她身上（selfstate 的 social），不再只有"库被写了"这一种叫醒方式。
            # 2026-10-26 反向看护：世界可能已经停了（负责同步的那半个进程死了），
            # 而视界里"旧消息"和"没消息"长得一模一样——她看不出来。所以每拍问一次心跳：
            # 停了就先踢一脚同步（内部 3 分钟限流），并且不让她按旧世界"自发"找话题
            # （当天的原话就是这么来的：18:49 还在说"憋了快一个钟头"，越过主人 18:34 的回答）。
            health = ports.world_health()
            stale = not bool(health.get("alive"))
            pushed = ports.kick_sync() if stale else False
            _STALE_COUNT[0] = (_STALE_COUNT[0] + 1) if stale else 0
            if stale and _STALE_COUNT[0] == STALE_WAKE_LIMIT + 1:
                log.warning(f"world_stale_give_up: stale_ms={health.get('stale_ms')} "
                            f"note={str(health.get('note') or '')[:200]}")
            stale_wake = (stale and cooled and _STALE_COUNT[0] <= STALE_WAKE_LIMIT
                          and (duration.now_ms() - _STALE_WAKE[0]) >= STALE_WAKE_MS)
            # 2026-10-27：白名单里标了「可主动」的会话，允许她更愿意起一拍（阈值用 SPEAK_SOCIAL_PROACTIVE）。
            _ptg: list = []
            try:
                from loop import speak_policy as _spk
                _ptg = _spk.proactive_targets()
            except Exception:
                _ptg = []
            _floor = selfstate.SPEAK_SOCIAL_PROACTIVE if _ptg else None
            spont = cooled and selfstate.spontaneous_ready(social_floor=_floor) and not stale
            # 2026-11-04（E-51）：自发开口要过闸门。18:46 那次她在作业群自问自答三遍，
            # 群里 57 分钟没人说话、spont_streak 已 12 连 —— 判据见 loop/spont.py。
            if spont:
                try:
                    from loop import spont as _spont_gate
                    _hot = _hot_chats(duration.epoch_s(duration.now_ms()),
                                      _spont_gate.GROUP_QUIET_S)
                    _pulse = (selfstate.load() or {}).get("pulse") or {}
                    _ok_sp, _why_sp = _spont_gate.gate(
                        streak=int(_pulse.get("spont_streak") or 0),
                        last_heard=float(_pulse.get("last_heard") or 0.0),
                        now=time.time(), hot_chats=_hot)
                    if not _ok_sp:
                        spont = False
                        # 2026-11-22：这个分支在每轮轮询（400ms）都会走到，原来每次都写一条
                        # blocked.jsonl —— 实测一天 1,476 条，全是同一句 `spont:streak=7`，
                        # 把真正的拦截证据淹了。同一个停因 10 分钟记一次，变了立刻记。
                        _sig = (_why_sp, int(_pulse.get("spont_streak") or 0))
                        if _spont_block_due(_sig, time.time()):
                            log.info("spont_blocked why=%s streak=%s hot=%s", _why_sp,
                                     int(_pulse.get("spont_streak") or 0), len(_hot))
                            try:
                                ports._log_blocked("", "spont:%s" % _why_sp, "",
                                                       ctx={"gate": "spont",
                                                            "streak": _pulse.get("spont_streak"),
                                                            "last_heard": _pulse.get("last_heard"),
                                                            "hot": len(_hot)})
                            except Exception as _lexc:  # noqa: BLE001 - 旁证写不下不许影响这一拍
                                log.warning(f"blocked_log_failed: err={repr(_lexc)[:120]}")
                except Exception as _exc:  # noqa: BLE001 - 闸门坏了按放行处理，别把主循环带崩
                    log.warning(f"spont_gate_failed: err={repr(_exc)[:120]}")
            # 2026-10-28：刚说完、对方还没接 —— 放一拍让她能补一句（判据见 loop/followup.py）。
            _fu = None
            if cooled and not spont and not stale:
                try:
                    from loop import followup as _fu_mod
                    _fu = _fu_mod.due()
                except Exception as _exc:  # noqa: BLE001
                    log.warning(f"followup_check_failed: err={repr(_exc)[:160]}")
                    _fu = None
            # 2026-10-31：待办窗口（`pending`）不会自己制造新的世界写入 —— 旧写法在这里
            # 无条件 continue，于是下面 `last_fp = "__pending__"` 那条重试永远等不到比较
            # （`changed = fp != last_fp` 根本没机会跑），第 4 个该回的会话就一直悬着。
            # 现在：还欠着窗口时按退避间隔主动再拍一次，不再等她「被世界叫醒」。
            _review_db, _goal_reviews = _GOAL_REVIEWS.poll(ports.db_paths(), now=datetime.now().isoformat(sep=" ", timespec="seconds"), allowed=cooled and not stale and not selfstate.in_quiet_hours())
            if stamp == last_stamp and not spont and not stale_wake and not _pend_due() and not _goal_reviews:
                if duration.now_ms() - last_hb >= _HB_EVERY_MS:
                    last_hb = duration.now_ms()
                    _write_heartbeat({"pid": os.getpid(), "uptime_ms": duration.now_ms() - started,
                                              "dry_run": dry_run, "ticks": ticks, "last_fp": last_fp,
                                              "last_tick_at": datetime.fromtimestamp(duration.epoch_s(last_tick)).strftime("%H:%M:%S") if last_tick else "",
                                              "poll_ms": POLL_MS, "idle": True,
                                              "at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                time.sleep(duration.sleep_s(POLL_MS))
                continue
            # 2026-10-27 用户要求："等图片解析来了之后，再接着后面的信息拼上去，拼完
            # 才是模型看到的，这时候模型看完才说话"。图没看清就跳过这一拍 —— **不消耗
            # 世界指纹**，描述落库后 media_ref.desc_version() 会让指纹再变一次，那时再来。
            if not media_barrier.wait_ready(ports.pick_db()):
                time.sleep(duration.sleep_s(POLL_MS))
                continue
            last_stamp = stamp
            db = ports.pick_db()
            _t_b0 = duration.now_ms()          # 2026-10-31：建快照到底占多少（vision/DB 都在里面）
            snap = snapshot.build_snapshot(db)
            _build_ms = duration.now_ms() - _t_b0
            text = snapshot.render_text(snap)
            fp = _fingerprint(text)
            changed = fp != last_fp
            # 2026-11-04（用户选的「松档」B）：群里不许开新话题 —— 只有「这句在跟我说」
            # 才允许自发开口。判据在 loop/addressing.py（@/拍/引用/名字/接着我说/没人接的问句），
            # 不许模型自评，也不许拿相似度凑。私聊不受影响（私聊本来就是要回的）。
            _hot = max(snap.get("cells") or [], default=None,
                       key=lambda cell: str((cell.get("peer_last") or {}).get("at") or ""))
            _addr = _cell_addressing(_hot)
            if spont and _addr:
                from loop import spont as _spont_gate2
                _cid2 = str(_addr.get("chat_id") or "")
                if not _spont_gate2.addressed_allows(
                        _addr.get("kind"), is_group=("@chatroom" in _cid2 or _cid2.startswith("group:"))):
                    spont = False
                    log.info("spont_blocked why=no_addressee chat=%s", _cid2)
                    try:
                        ports._log_blocked(_cid2, "spont:no_addressee", "",
                                           ctx={"gate": "spont",
                                                "kind": (_addr or {}).get("kind"),
                                                "n": (_addr or {}).get("n"),
                                                "short": (_addr or {}).get("short")})
                    except Exception as _lexc:  # noqa: BLE001 - 旁证写不下不许影响这一拍
                        log.warning(f"blocked_log_failed: err={repr(_lexc)[:120]}")
            # 2026-11-04（用户选的「全 C」）：主人私聊里一句「往…说…」就开一件事，
            # 开着的事每一拍往前推一步。开单只认主人私聊 + 真实入站 msg_id（E-48 的教训：
            # 她自己的自述不算吩咐）；推进的每一步都真的排进 outbound_queue（E-59：台账写 done
            # 而消息没发出去，是这次要根治的那个假账）。
            _job, _job_line, _job_scope = _advance_snapshot_jobs(snap, db, dry_run=dry_run)
            cooled = (duration.now_ms() - last_tick) >= MIN_TICK_INTERVAL_MS
            _review_db, _goal_reviews = goal_review.prioritize_messages(db, _review_db, _goal_reviews, changed=changed or bool(_fu) or bool(_job))
            if (changed or spont or stale_wake or _fu or _goal_reviews) and cooled:
                wake = {"last_tick_ms": last_tick}
                if _goal_reviews:
                    wake["goal_reviews"] = _goal_reviews
                    wake["review_db"] = _review_db
                    _GOAL_REVIEWS.started(_review_db, _goal_reviews)
                if _job or _job_line:
                    wake["job"] = _job
                    wake["job_line"] = _job_line
                    wake["job_scope"] = _job_scope
                if stale_wake:
                    _STALE_WAKE[0] = duration.now_ms()
                    wake["world_stale_ms"] = health.get("stale_ms")
                    wake["world_stale_note"] = health.get("note") or ""
                    wake["world_sync_kicked"] = pushed
                if spont:
                    wake["spontaneous"] = True
                    if _ptg:
                        wake["proactive_names"] = [t.get("name") or t.get("chat_id") for t in _ptg][:8]
                if _fu:
                    wake["followup"] = _fu
                    log.info("followup_tick target=%s age_s=%s",
                             str(_fu.get("target") or ""), _fu.get("age_s"))
                selfstate.mark_spontaneous()
                t0 = duration.now_ms()
                _ALIVE.update({"pid": os.getpid(), "ticks": ticks, "dry_run": dry_run,
                               "last_fp": last_fp})
                out = asyncio.run(one_tick(dry_run=dry_run, llm=llm, wake=wake))
                last_tick = duration.now_ms()
                ticks += 1
                # 2026-10-24 分层：她自己的发送也会改库、也会改视界文本。
                # 不把它从基线里吃掉，她每发一句都会把自己再叫醒一次（用户看到的"自言自语"）。
                # 但如果这一拍里真的有人说话，就别吞——下一圈还要醒。
                if ports.peer_events_since(duration.epoch_s(t0), limit=1):
                    last_fp = out["fingerprint"]
                else:
                    last_stamp = ports.world_stamp() + (media_ref.desc_version(),)
                    last_fp = _fingerprint(snapshot.render_text(snapshot.build_snapshot(db)))
                _pend = int(out.get("pending") or 0)
                if _pend:
                    _PEND_TRIES[0] += 1
                    _PEND_LAST[0] = duration.now_ms()
                    if _PEND_TRIES[0] <= 6:  # 还有没轮到的 ★：隔一拍再来，最多连着 6 次
                        last_fp = "__pending__"
                        print("[loop.run] 还有 %d 个窗口没轮到，下一拍接着来" % _pend, flush=True)
                else:
                    _PEND_TRIES[0] = 0
                # 2026-10-27 晚：小本本不再挂在"这一拍有人说话"那支上——过了 3 点，
                # 任意一拍都试一次；maybe_write 自己会判 hour<3 / 当天已写过 / 素材不足，
                # 所以这里每拍调一次是安全的（没到点就立刻返回空串）。
                if (_HIST_DAY[0] != time.strftime("%Y-%m-%d")
                        and duration.now_ms() - _HIST_TRY[0] > _HIST_EVERY_MS):  # 最多 5 分钟试一次，别每拍都读账本
                    _HIST_TRY[0] = duration.now_ms()
                    try:
                        _wrote = asyncio.run(asyncio.wait_for(history.maybe_write(), 150))
                        if _wrote:
                            _HIST_DAY[0] = time.strftime("%Y-%m-%d")
                            log.info("self_history_written chars=%s", len(_wrote))
                    except Exception as _exc:  # noqa: BLE001
                        log.warning(f"self_history_failed: err={repr(_exc)[:200]}")
                _heard = bool(ports.peer_events_since(duration.epoch_s(t0), limit=1))
                # 2026-11-20：排队不算说过。只有本地确认发出的才按"我说了"记；
                # 还排在图队里的，既不算说过，也不算这一拍白醒。
                _send_state = ledger.send_state_of(out.get("receipts") or [])
                _spoke = bool(_send_state["confirmed"])
                selfstate.note_tick(spoke=_spoke, heard=_heard)
                # 2026-11-02：自发醒来却什么都没说 —— 也算一次「白醒」，计入退避。
                # 以前 streak 只在「说了话、没人接」时增长，所以她整天不开口时
                # 间隔永远卡在 5 分钟（10-29 实测：353 拍自发 / 0 句输出 / 1346 次调用）。
                # 有人理她那支（_heard）note_tick 已把 streak 归零，这里不再叠。
                if spont and not _spoke and not _heard and not _send_state["queued"]:
                    selfstate.note_spont_silent()
                _win_txt = " ".join("%s=%.1fs/%d" % (n, s, r) for n, s, r in (out.get("timings") or []))
                print("[loop.run] 第 %d 拍 %.1fs receipts=%d%s%s | build=%.1fs | 窗口 %s" % (
                    ticks, duration.epoch_s(duration.now_ms() - t0), len(out["receipts"]),
                    " 自发" if spont else "", (" 说了" if _spoke else (" 排进队列" if _send_state["queued"] else "")),
                    duration.epoch_s(_build_ms), _win_txt or "-"), flush=True)
            elif changed and not cooled:
                pass  # 世界还在变，但刚拍过，等冷却
            else:
                last_fp = fp
            _write_heartbeat({"pid": os.getpid(), "uptime_ms": duration.now_ms() - started,
                              "dry_run": dry_run, "ticks": ticks, "last_fp": last_fp,
                              "last_tick_at": datetime.fromtimestamp(duration.epoch_s(last_tick)).strftime("%H:%M:%S") if last_tick else "",
                              "poll_ms": POLL_MS, "at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        except Exception as exc:  # noqa: BLE001
            # 2026-10-31：只印一行 repr 的时候，OSError(22) 连"哪一行"都不知道。前 3 次
            # 印完整栈，之后退化成每 60 秒一行 —— 循环还是继续跑，但不再是瞎跑。
            _ERR_N[0] += 1
            if _ERR_N[0] <= 3 or duration.now_ms() - _ERR_LAST[0] > 60000:
                _ERR_LAST[0] = duration.now_ms()
                import traceback as _tb
                print("[loop.run] 出错（继续跑）：%r\n%s" % (exc, _tb.format_exc()), flush=True)
            else:
                print("[loop.run] 出错（继续跑）：%r" % (exc,), flush=True)
        time.sleep(duration.sleep_s(POLL_MS))


if __name__ == "__main__":
    # 2026-11-05（§35.6 第 2 条）：引擎**自己**也记一行退出账 —— 它是最常被重启的那个，
    # 之前只有监管器视角的「子进程退出 rc=X」。总闸有两个（loop.stop / all.stop），任一在即算。
    raise SystemExit(_exitlog.wrap_main(
        main, role="engine",
        detail="loop.run %s" % (" ".join(sys.argv[1:]) or "--dry"),
        stop_files=(os.path.abspath(STOP_FILE),
                    os.path.abspath(os.path.join("logs", "all.stop")))))
