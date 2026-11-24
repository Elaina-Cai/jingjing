"""desk —— 唯一的「键鼠线」执行器（2026-10-28，动作队列第 3 步）。

为什么要有它
------------
微信的发送是**抢键鼠**的：激活窗口、点输入框、粘贴、回车、读屏。
两个线程同时做这些事，就会互相把焦点顶掉 —— 今天因此丢过话、发错过会话。
所以：**所有需要键鼠的投递，都从这一条线程里出去**。别人只管往队列里放，
不放心的东西才会同时抢键盘。

职责只有四个，别把它写成第二个 brain：
  1. claim：从 outbound_queue 认领一条（CAS，天然不会重复）
  2. deliver：按 kind 调发送层（text / image / file）
  3. settle：把结果写回队列（sent / pending+退避 / failed）
  4. housekeeping：心跳、回收僵尸、TTL 作废

不做的事：不判断该不该说（那是她的事）、不生成内容（那是 brain 的事）、
不重排语义（改口是入队方的事，见 docs/动作队列-设计.md）。
"""
from __future__ import annotations

import os
import sys
import time
import atexit
import threading

# 2026-10-28：pythonw 下 sys.stdout/stderr 为 None。structlog 的 file-based logger

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# 2026-11-05（主人批的「发送工也要自己记账」）：退出时留一行给 logs/exits.jsonl。
from core.infra import exitlog as _exitlog  # noqa: E402
# __DESK_STDIO_GUARD__  pythonw 下 sys.stdout/stderr 为 None；structlog 的 PrintLogger
# 会把 None 当 key 放进 WeakValueDictionary → TypeError，任何 log.debug() 都会打断调用方。
if sys.stdout is None or sys.stderr is None:
    _desk_stream = open(os.path.join(ROOT, "logs", "desk_stdout.log"), "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _desk_stream
    if sys.stderr is None:
        sys.stderr = _desk_stream
from db import world_db as W  # noqa: E402
from loop import residue as _residue  # 2026-11-04 输入框残留计数（纯状态机）
from core.infra.envflags import flag
OWNER_PREFIX = "desk"
LOCK_PATH = os.path.join(ROOT, "logs", "desk.lock")

# 退避序列：短失败的常见原因是"焦点被抢/窗口还没就绪"，秒级重试就够
BACKOFF_S = (2.0, 5.0, 12.0)

# 一次发送超过这么久没心跳，就当进程死了（发送本身最长 70 秒左右）
STALE_S = 120.0


def _log(event: str, **kw) -> None:
    """desk 自己的账：单独一行 JSON 落到 logs/desk.log，方便与 trace 对时间。"""
    try:
        import json
        row = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
        row.update(kw)
        with open(os.path.join(ROOT, "logs", "desk.log"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _single_instance() -> bool:
    """一个进程一把锁（写 pid，pid 不在了就当锁是死的）。"""
    try:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        if os.path.exists(LOCK_PATH):
            raw = open(LOCK_PATH, "r", encoding="utf-8").read().strip()
            if raw.isdigit() and int(raw) != os.getpid():
                try:
                    import psutil
                    if psutil.pid_exists(int(raw)):
                        return False
                except Exception:
                    pass
        with open(LOCK_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(LOCK_PATH) and open(LOCK_PATH, "r", encoding="utf-8").read().strip() == str(os.getpid()) and os.remove(LOCK_PATH))
        return True
    except Exception:
        return True


# --------------------------------------------------------------------------- 环境

_BOOTED = {"done": False, "env": 0, "writes": False}


def _bootstrap() -> dict:
    """desk 启动必须做的事（幂等）。

    2026-10-28 的教训：desk 是**服务进程**，不是调试脚本。少了这两步，
    `features` 会走默认（关）分支、`write_guard` 会拒绝 meta 库，
    发送层在初始化阶段就拿到 None，最终表现成
    `TypeError: cannot create weak reference to 'NoneType' object`（1–12 ms 就返回）。
    """
    if _BOOTED["done"]:
        return _BOOTED
    n = 0
    try:
        env = os.path.join(ROOT, ".env")
        if os.path.exists(env):
            for line in open(env, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()
                    n += 1
    except Exception as exc:  # noqa: BLE001
        _log("desk_env_failed", err=repr(exc)[:160])
    try:
        from db import write_guard as _wg
        _wg.declare_writes_allowed(declared_by="loop.desk")
        _BOOTED["writes"] = True
    except Exception as exc:  # noqa: BLE001
        _log("desk_write_declare_failed", err=repr(exc)[:160])
    _BOOTED["done"] = True
    _BOOTED["env"] = n
    _log("desk_bootstrap", env_vars=n, writes=_BOOTED["writes"])
    return _BOOTED


# --------------------------------------------------------------------------- deliver


def _label(chat_id: str) -> str:
    """发送层吃的是**显示名**（微信搜索框不认 chat_id）。"""
    try:
        from loop import outbox, ports as P
        return P.label_for(chat_id) or chat_id
    except Exception:
        return chat_id


def _dry(chat_id: str) -> bool:
    try:
        from core.features import dry_run_send
        return bool(dry_run_send(chat_id))
    except Exception:
        return False


def _sender():
    from core.execution.wx_sender import WxSender
    try:
        return WxSender.get_instance()
    except Exception:
        return WxSender()



def _schedule_confirm(chat_id: str, text: str) -> None:
    # 2026-10-28：发送成功只算 sent；真正到达要靠回读追认（异步，微信写库要 1-300 秒）。
    try:
        from core.execution.gui.wechat_send import _gui_confirm_later
    except Exception:
        return
    try:
        _gui_confirm_later(chat_id, text)
    except Exception:
        pass
_CONFIRM_DELAYS = (15, 30, 60, 120, 240)
_CLAIM_LOCK = threading.Lock()


def _claimed_msg_ids() -> set:
    """已经被别的队列行认领过的 msg_id（`why` 里写着 `msg <id>`）。

    2026-11-04：同一会话连发两句一模一样的话时，两条队列行的追认线程都拿到「最新那条」，
    于是两条行记成同一个 msg_id（261/262）。认领前先看这里，别人拿走的不要。
    """
    try:
        c = W.conn()
        rows = c.execute("select why from outbound_queue where why like 'msg %'").fetchall()
    except Exception as exc:  # noqa: BLE001 - 查不到就当没人认领过
        _log("desk_claim_lookup_failed", err=repr(exc)[:80])
        return set()
    out = set()
    for r in rows or ():
        _w = str((r["why"] if isinstance(r, dict) else r[0]) or "").strip()
        if _w.startswith("msg "):
            out.add(_w[4:].strip())
    return out


#: 追认线程从「排上」到「真的开跑」允许多久（秒）。超了就当成压根没起来。
_CONFIRM_START_GRACE_S = 90.0

#: qid -> (chat_id, 排上的时间)。线程真开跑就删掉这一条。
#: Only observations still inside the confirm window are resumed after a restart;
#: older rows are settled as unconfirmed instead of re-alerting the owner every restart.
_CONFIRM_HORIZON_S: float = float(sum(_CONFIRM_DELAYS)) + 120.0
_PENDING_START: dict = {}

#: 追认线程从「开跑」到「出结果」允许多久（秒）。`_CONFIRM_DELAYS` 加起来 465 秒，再留点余量。
#: 超了就是「开跑了但卡住」——以前这种只会静默留在 pending（§29.5 第 2 条，2026-11-05）。
_CONFIRM_STUCK_S = 600.0

#: qid -> (chat_id, 开跑的时间)。出结果（ok / timeout）就删掉这一条。
_RUNNING: dict = {}


def _mark_pending_start(qid: int, chat_id: str) -> None:
    _PENDING_START[int(qid)] = (str(chat_id or ""), time.time())


def _mark_started(qid: int) -> None:
    got = _PENDING_START.pop(int(qid), None)
    _RUNNING[int(qid)] = (str(got[0]) if got else "", time.time())


def _mark_finished(qid: int) -> None:
    """追认线程出结果了（ok 或 timeout）。卡住巡检从此不再管这一条。"""
    _PENDING_START.pop(int(qid), None)
    _RUNNING.pop(int(qid), None)


def sweep_confirm_starts(*, grace_s: float = _CONFIRM_START_GRACE_S, now=None) -> int:
    """排上了却一直没开跑的追认 → 喊一声（§29，2026-11-04）。

    为什么需要它：起线程那一行原来没有 try/except，线程起不来就静默 —— 那一行的 ack 会
    永远停在 pending，而没人知道「回读核对」压根没跑；2026-11-04 那 23 条没凭据的行里
    就有一部分是这种。这里只负责**喊**（日志 + 抄送主人），不重试、不猜。
    """
    at_now = time.time() if now is None else float(now)
    late = [(q, c, t) for q, (c, t) in list(_PENDING_START.items())
            if at_now - float(t) >= float(grace_s)]
    for qid, chat, at in late:
        _PENDING_START.pop(qid, None)
        _log("desk_confirm_never_started", id=qid, waited_s=round(at_now - float(at), 1))
        try:
            from loop import owner_copy as _oc5

            _oc5.note("confirm_not_started", target=chat)
        except Exception as exc:  # noqa: BLE001 - 抄送失败不许把巡检带崩
            _log("owner_copy.confirm_not_started_failed", id=qid, err=repr(exc)[:100])
    return len(late)


def sweep_confirm_stuck(*, stuck_s: float = _CONFIRM_STUCK_S, now=None) -> int:
    """开跑了却一直没出结果的追认 → 喊一声（§29.5 第 2 条，2026-11-05）。

    为什么需要它：`sweep_confirm_starts` 只管「排上了却没开跑」。真开跑之后如果线程卡在
    `_gui_self_proof`（微信窗口卡死、SQLite 被占），那一行同样会永远停在 pending，而外面
    看到的只是「她好像说过、但群里没有」——跟没发生过一样。这里只负责**喊**（日志 + 抄送主人），
    不重试、不猜、不去动那一行的状态。

    `_CONFIRM_DELAYS` 合计 465 秒，所以默认门槛 600 秒；喊过就从 `_RUNNING` 移走，只喊一次。
    """
    at_now = time.time() if now is None else float(now)
    late = [(q, c, t0) for q, (c, t0) in list(_RUNNING.items())
            if at_now - float(t0) >= float(stuck_s)]
    for qid, chat, at in late:
        _RUNNING.pop(qid, None)
        _log("desk_confirm_stuck", id=qid, waited_s=round(at_now - float(at), 1))
        try:
            from loop import owner_copy as _oc6

            _oc6.note("confirm_stuck", target=chat)
        except Exception as exc:  # noqa: BLE001 - 抄送失败不许把巡检带崩
            _log("owner_copy.confirm_stuck_failed", id=qid, err=repr(exc)[:100])
        # 2026-11-05（§29.5 第 2 条尾巴）：喊完还得给出路。卡住 ≠ 没发出去，所以
        # `settle_stuck` 先按正文回查（查到了就只补凭据），查不到才重发一跳。
        try:
            from loop import receipts as _rc6

            _got = _rc6.settle_stuck(qid)
            _log("desk_confirm_stuck_settled", id=qid,
                 action=str(_got.get("action") or _got.get("why") or ""),
                 new_id=_got.get("new_id"))
        except Exception as exc:  # noqa: BLE001 - 兜不住就留痕，不许把巡检带崩
            _log("desk_confirm_stuck_settle_failed", id=qid, err=repr(exc)[:100])
    return len(late)


def _confirm_queue_later(qid: int, chat_id: str, text: str, *, receipt_id=0, account_id="", platform="wechat", path=None, baseline=None, attempt_no=0, source_token="") -> None:
    from core.execution.local_text_receipt import observe, ObservationSourceChanged
    if not receipt_id or not account_id or path is None or baseline is None:
        return  # 来源/发送前游标不完整，不启动无范围确认。

    def _run() -> None:
        # 2026-11-04（§24 地基②）：真库里有 23 条 sent 却 why 空、ack 还停在 pending 的行——
        # 光看结果分不清「追认从没开始」和「追认超时」。开跑先留一句。
        _mark_started(qid)
        _log("desk_confirm_started", id=qid, delays=list(_CONFIRM_DELAYS))
        for _d in _CONFIRM_DELAYS:
            time.sleep(_d)
            try:
                mid = observe(path, chat_id, text, baseline, source_token=source_token)
            except ObservationSourceChanged as exc:
                _log("desk_observation_source_changed", id=qid, attempt_no=attempt_no, err=str(exc))
                _mark_finished(qid)
                return
            except Exception as exc:
                _log("desk_observation_read_failed", id=qid, attempt_no=attempt_no, err=repr(exc)[:160])
                mid = ""
            if not mid:
                continue
            try:
                W.confirm_outbound(chat_id, text, msg_id=str(mid), receipt_id=receipt_id,
                                   platform=platform, account_id=account_id, queue_id=qid, attempt_no=attempt_no)
            except Exception as exc:
                _log("desk_confirm_writeback_failed", id=qid, err=repr(exc)[:120])
                continue
            _log("desk_confirm_ok", id=qid, msg_id=str(mid))
            _mark_finished(qid)
            return
        _log("desk_confirm_timeout", id=qid)
        # 2026-11-04 ④ 主人抄送：回读五次都没找到 = 可能没送到。以前只写 unconfirmed
        # 就结束，漏发的话就此消失没人知道。
        try:
            from loop import owner_copy as _oc4
            _oc4.note("confirm_timeout", target=str(chat_id or ""))
        except Exception as _oexc4:  # noqa: BLE001
            _log("owner_copy.confirm_timeout_failed", id=qid, err=repr(_oexc4)[:100])
        try:  # 追认失败要写回库里：ack 不许永远停在 pending（2026-10-30）
            _c2 = W.conn()
            _why2 = "回读追认 %d 次都没找到这条" % len(_CONFIRM_DELAYS)
            W.mark_confirmation_unknown(qid, _why2, attempt_no=attempt_no, connection=_c2)
        except Exception as exc:  # noqa: BLE001
            _log("desk_confirm_writeback_failed", id=qid, err=repr(exc)[:80])
        # 本地回读失败不能证明未发出；只保留未确认结果，不创建第二次发送。
        _mark_finished(qid)

    try:
        _mark_pending_start(qid, chat_id)
        threading.Thread(target=_run, name="desk-confirm-%s" % qid, daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - 起不来要留痕（否则 ack 永远 pending 且没人知道）
        _mark_finished(qid)
        _log("desk_confirm_start_failed", id=qid, err=repr(exc)[:100])
        try:
            from loop import owner_copy as _oc6

            _oc6.note("confirm_not_started", target=str(chat_id or ""))
        except Exception as exc2:  # noqa: BLE001
            _log("owner_copy.confirm_not_started_failed", id=qid, err=repr(exc2)[:100])

def recover_observations() -> dict:
    """重启后仅恢复有限只读观察，绝不认领或补发队列。"""
    from core.execution.local_text_receipt import account_path
    out = {"resumed": 0, "unavailable": 0, "expired": 0}
    for row in W.pending_outbound_observations():
        qid = int(row["queue_id"])
        if qid in _RUNNING or qid in _PENDING_START:
            continue
        if str(row.get("queue_ack") or "") in {"confirmed", "unconfirmed", "dry_run"}:
            # 已经有定论的行不再重新追认（不知道就写不知道，不反复报警）。
            out["expired"] += 1
            continue
        try:
            created_ts = float(row.get("queue_created_ts") or 0)
        except (TypeError, ValueError):
            created_ts = 0.0
        age_s = (time.time() - created_ts) if created_ts > 0 else 0.0
        if age_s > _CONFIRM_HORIZON_S:
            # 2026-11-16: stale rows (347/348/350/351 from 11-15) were re-armed on every restart,
            # timed out 465 s later and copied the owner again. Past the window: record unknown once.
            try:
                if W.mark_confirmation_unknown(qid, "重启前发送，已超过回读窗口（%d 秒），不再追认" % int(_CONFIRM_HORIZON_S), attempt_no=row["attempt_no"]):
                    out["expired"] += 1
                    _log("desk_observation_expired", id=qid, age_s=int(age_s))
            except Exception as exc:  # noqa: BLE001 - accounting failure must not break recovery
                _log("desk_confirm_writeback_failed", id=qid, err=repr(exc)[:120])
            continue
        if not row.get("source_token"):
            _log("desk_observation_source_missing", id=qid, attempt_no=row["attempt_no"])
            out["unavailable"] += 1
            continue
        path = account_path(ROOT, row["platform"], row["account_id"])
        if path is None:
            out["unavailable"] += 1
            continue
        _confirm_queue_later(qid, row["chat_id"], row["text"], receipt_id=row["receipt_id"],
            platform=row["platform"], account_id=row["account_id"], path=path,
            baseline=row["baseline"], attempt_no=row["attempt_no"], source_token=row.get("source_token", ""))
        out["resumed"] += 1
    return out


def sweep_acks(max_age_s: float = 60.0) -> int:
    """追认线程是进程内的 daemon：desk 一重启，ack 就永远停在 pending。
    这里把「已发出、但早就过了确认窗口」的那些落成 unconfirmed —— 不知道就写不知道。
    """
    try:
        c = W.conn()
        rows = c.execute("select id, attempts from outbound_queue where ack='pending' and state='sent' "
                         "and not_after < ?", (time.time() - max_age_s,)).fetchall()
        claims = [(int(r[0]), int(r[1])) if not isinstance(r, dict) else (int(r["id"]), int(r["attempts"])) for r in rows]
        ids = []
        why = "追认线程随重启中断（超过 %d 秒未确认）" % int(max_age_s)
        for qid, attempt in claims:
            if W.mark_confirmation_unknown(qid, why, attempt_no=attempt, connection=c):
                ids.append(qid)
        c.commit()
        if ids:
            _log("desk_ack_swept", ids=len(ids))
        return len(ids)
    except Exception as exc:  # noqa: BLE001
        _log("desk_ack_sweep_failed", err=repr(exc)[:80])
        return 0


def _clear_own_residue(item: dict, who: str):
    """别人不许在发送路径里清空输入框（2026-10-28「哦」事故），但**重试前**得能收拾残局。

    这里不在发送前抢焦点、不自己开搜索（那会把搜索浮层留在窗口里，反而让布局检测瞎掉），
    而是**等发送失败之后**再清：那一刻发送层刚验过标题、正对着目标会话，输入框就是它。
    只清"上一轮确实因为框里已有字而失败"的那一条，清不干净就继续 fail closed。
    """
    why = str(item.get("why") or "")
    if "输入框里已经有字" not in why:
        return False
    try:
        from core.execution import wx_chat as _wc

        _ok = bool(_wc.clear_message_box())
    except Exception as exc:  # noqa: BLE001 - 清不掉就照旧卡着，等人看一眼
        _ok = False
        _log("desk_residue_clear_failed", id=item.get("id"), err=repr(exc)[:120])
    _log("desk_residue_cleared", id=item.get("id"), who=str(who), ok=_ok)
    return _ok


_ACTIVATION_HINTS = ("无法激活", "激活微信窗口", "未能激活", "not_foreground")


def _looks_like_activation_failure(why: str) -> bool:
    """是不是"窗口没拿到前台"这一类失败（熄屏/被别的窗口占着时最常见）。"""
    _w = str(why or "")
    return any(_h in _w for _h in _ACTIVATION_HINTS)


def _post_send_call(item: dict, stage: str, callback, default=None):
    """发送已返回后的旁务失败只留痕，不能反写发送结果。"""
    try:
        return callback()
    except Exception as exc:
        _log("desk_post_send_failed", id=item.get("id"), attempt_no=item.get("attempts"),
             stage=stage, err=repr(exc)[:200])
        return default


def _finish_text_delivery(item, chat_id, text, ack, platform, account, snapshot_saved, path, baseline, source_token):
    _post_send_call(item, "residue_reset", lambda: _residue.reset(item.get("id")))
    receipt = _post_send_call(item, "record_outbound", lambda: W.record_outbound(
        chat_id, text, platform=platform, account_id=account, turn_id="queue:%s" % item.get("id"),
        seq=int(item.get("attempts") or 0), method="desk", ack=ack or "pending"), 0)
    if not receipt:
        _log("desk_post_send_receipt_missing", id=item.get("id"), attempt_no=item.get("attempts"))
        item["_post_send_ledger_missing"] = True
        return
    if not snapshot_saved:
        return

    def bind():
        W.bind_observation_receipt(int(item.get("id") or 0), receipt,
            owner=str(item.get("owner") or ""), attempt_no=int(item.get("attempts") or 0))
        return True

    if not _post_send_call(item, "bind_observation_receipt", bind, False):
        return
    _post_send_call(item, "schedule_observation", lambda: _confirm_queue_later(
        int(item.get("id") or 0), chat_id, text, receipt_id=receipt, account_id=account,
        platform=platform, path=path, baseline=baseline, attempt_no=int(item.get("attempts") or 0), source_token=source_token))


def deliver(item: dict, *, dry: bool = False) -> tuple:
    """真正动手。返回 (ok, ack, why)。

    只在这里碰键鼠；失败只描述现象，不做语义判断。
    """
    from .delivery_result import DeliveryResult
    _bootstrap()
    chat_id = str(item.get("chat_id") or "")
    kind = str(item.get("kind") or "text")
    payload = item.get("payload") or {}
    _gate = None
    who = _label(chat_id)
    if dry or _dry(chat_id):
        return True, "dry_run", "预演模式：没真发"
    _dt0 = time.time()
    try:
        s = _sender()
        _dt_sender = int((time.time() - _dt0) * 1000)
        if kind == "text":
            _dt_a = time.time()
            from core.execution.wx_sender import account_send_options
            options = account_send_options(str(item.get("account_id") or ""),
                                           platform=str(item.get("platform") or "wechat"))
            from core.execution.local_text_receipt import account_path, capture
            _account = str(item.get("account_id") or "")
            _platform = str(item.get("platform") or "wechat")
            _proof_path = account_path(ROOT, _platform, _account)
            try:
                _baseline, _source_token = capture(_proof_path)
            except Exception:
                _baseline, _source_token = None, ""
            from core import features as _send_features
            _snapshot_saved = False
            if _baseline is not None and not _send_features.dry_run_send(str(who or "")):
                W.save_outbound_observation(int(item.get("id") or 0), _baseline, owner=str(item.get("owner") or ""), attempt_no=int(item.get("attempts") or 0), source_token=_source_token)
                _snapshot_saved = True
            if str(item.get("group_id") or "").startswith("owner-trial:"):
                from scripts.observe_owner_shadow import trial_stopped, trial_window

                if trial_stopped() or not trial_window() or time.time() >= float(item.get("not_after") or 0):
                    return False, "blocked", "owner_trial_stopped_or_expired"
            from db.models import is_group_chat_id
            _gate_type = "group" if is_group_chat_id(chat_id) else "private"
            from core.speech.outbound import gate_direct_text
            _gate = gate_direct_text(str(payload.get("text") or ""), chat_type=_gate_type, chat_id=chat_id)
            if _gate.get("blocked"):
                _log("desk_output_gate_blocked", id=item.get("id"), chat=chat_id, rule=_gate.get("rule"))
                from core.speech.outbound import record_direct_gate_audit as _record_block
                _record_block(
                    _gate, platform=_platform, account_id=_account, chat_id=chat_id,
                    chat_type=_gate_type, kind="text", status="blocked",
                    error="output_gate:%s" % (_gate.get("rule") or "output_gate"),
                    content=str(payload.get("text") or ""),
                )
                return False, "blocked", "output_gate:%s" % (_gate.get("rule") or "output_gate")
            r = s.send_text(who, str(_gate.get("text") or ""), **options)
            if isinstance(r, dict) and r.get("dry_run"):
                return True, "dry_run", "预演模式：没真发"
            _dt_send = int((time.time() - _dt_a) * 1000)
            ok = bool(getattr(r, "get", lambda k, d=None: None)("ok") if isinstance(r, dict) else getattr(r, "ok", False))
            ack = str((r.get("ack") if isinstance(r, dict) else getattr(r, "ack", "")) or "")
            why = "" if ok else str((r.get("error") if isinstance(r, dict) else getattr(r, "error", "")) or "")[:200]
            if not ok and _baseline is not None and "输入框仍有文本" in why:
                # 2026-11-22：框里还有字不等于没发出去。10:16 那条告警已经在账号库里
                # 落库（messages 10:16:01），键盘却报「发送后输入框仍有文本」，账本于是
                # 把一条真发出去的话记成失败。这里用发送前绑定的游标回看一眼；确认发出
                # 就按成功走，框里的残留仍留给下一次前置校验。
                try:
                    from core.execution.local_text_receipt import observe as _observe

                    _lid = _observe(
                        _proof_path, chat_id,
                        str((_gate or {}).get("text") or payload.get("text") or ""),
                        _baseline, source_token=_source_token)
                except Exception as exc:  # noqa: BLE001 - 读不出来只降级，不改判
                    _log("desk_box_dirty_readback_failed", id=item.get("id"), err=repr(exc)[:100])
                    _lid = ""
                if _lid:
                    _log("desk_box_dirty_but_delivered", id=item.get("id"), local_id=str(_lid)[:20])
                    ok, ack, why = True, "pending", ""
            if not ok and _residue.RESIDUE_MARKER in why:
                # 上一次是我自己贴进去又没发成的残句卡住了框：收拾掉，下一次重试就能走。
                _clear_own_residue({"id": item.get("id"), "why": why}, who)
                # 但"收拾"本身也可能失败（2026-11-04：连续三次 ok=false），所以数着；
                # 到 RESIDUE_STRIKE_LIMIT 次就判死，不再换会话重试（见 loop/residue.py）。
                why, _n, _dead = _residue.note_strike(item.get("id"), why)
                if _dead:
                    _log("desk_residue_blocked", id=item.get("id"), who=str(who), strikes=_n)
            if _gate is not None:
                from core.speech.outbound import record_direct_gate_audit
                record_direct_gate_audit(
                    _gate, platform=_platform, account_id=_account, chat_id=chat_id,
                    chat_type=_gate_type, kind="text", status=("sent" if ok else "failed"),
                    error=str(why or ""), content=str(payload.get("text") or ""),
                )
            if ok and not dry:
                _finish_text_delivery(item, chat_id, str((_gate or {}).get("text") or payload.get("text") or ""), ack,
                                      _platform, _account, _snapshot_saved, _proof_path, _baseline, _source_token)
                _post_send_call(item, "phase_log", lambda: _log("desk_phases", id=item.get("id"),
                    sender_ms=_dt_sender, send_ms=_dt_send, total_ms=int((time.time() - _dt0) * 1000)))
            # 2026-11-22：把「按回车之前就失败」的证据带出去。队列只在 not_executed=True
            # 时才允许重排（见 world_db.settle）；文本这条路以前一律当「可能已发出」，
            # 一次输入框残留就把这条消息永久丢掉。
            if ok:
                return ok, ack, why
            _ne = bool((r or {}).get("not_executed")) if isinstance(r, dict) else False
            return DeliveryResult(False, ack=ack, why=why, not_executed=_ne)
        if kind in ("image", "file"):
            path = str(payload.get("path") or "")
            if not path or not os.path.isfile(path):
                return DeliveryResult(False, why="文件不存在：%s" % path, not_executed=True)
            r = s.send_image(who, path) if kind == "image" else s.send_file(who, path)
            if isinstance(r, dict) and r.get("dry_run"):
                return True, "dry_run", "预演模式：没真发"
            ok = bool(r.get("ok")) if isinstance(r, dict) else bool(getattr(r, "ok", False))
            return ok, "unconfirmed" if ok else "", "" if ok else str((r or {}).get("error") or "发送失败")[:200]
        if kind == "sticker":
            key = str(payload.get("key") or payload.get("emotion") or "").strip()
            if not key:
                return DeliveryResult(False, why="表情没写 key", not_executed=True)
            from core.execution import wx_sticker_sender as _wss
            from loop import stickers as _stk0
            # 账号要跟着走：send_sticker 靠 account_id 找这个账号的 registry，
            # 不传就退回老的 data/wechat/sticker_index.json，报"未找到表情包配置"。
            try:
                _acct0 = _stk0.account_of(key)
            except Exception:  # noqa: BLE001 - 查不到账号就按默认库试一次
                _acct0 = ""
            r = _wss.send_sticker(who, key, emotion=key, platform="wechat",
                                  account_id=_acct0, dry_run=dry)
            if isinstance(r, dict) and r.get("dry_run"):
                return True, "dry_run", "预演模式：没真发"
            ok = bool((r or {}).get("ok"))
            if ok:
                try:  # 冷却计时以"真发出去了"为准
                    _stk0.mark_sent(chat_id, key)
                except Exception as exc:  # noqa: BLE001 - 冷却记不上不影响这条已经发出的事实
                    _log("desk_sticker_mark_failed", id=item.get("id"), err=repr(exc)[:120])
            _log("desk_sticker", id=item.get("id"), key=key, chat=chat_id, ok=ok,
                 cell=(r or {}).get("cell"), score=(r or {}).get("score"),
                 ms=int((time.time() - _dt0) * 1000))
            return ok, ("unconfirmed" if ok else ""), ("" if ok else str((r or {}).get("error") or "表情没发出去")[:200])
        return DeliveryResult(False, why="不认识的 kind：%s" % kind, not_executed=True)
    except Exception as exc:  # noqa: BLE001
        import traceback
        _full = traceback.format_exc()
        if flag("YINYUE_DESK_FULLTB"):
            _log("desk_send_traceback", tb=_full[-1800:])
        tb = " | ".join(l.strip() for l in _full.strip().splitlines()[-3:])
        return False, "", "%s: %s  @%s" % (type(exc).__name__, str(exc)[:120], tb[:220])


# --------------------------------------------------------------------------- 一拍


def execute_one(*, dry: bool = False, owner: str = "") -> dict:
    """认领一条并送出去。返回执行摘要；队列空返回 {"idle": True}。"""
    owner = owner or "%s-%d" % (OWNER_PREFIX, os.getpid())
    it = W.claim_next(owner=owner)
    if not it:
        return {"idle": True}
    qid = int(it["id"])
    hb_stop = threading.Event()

    def _beat():
        while not hb_stop.wait(10.0):
            try:
                if not W.heartbeat(qid, owner, attempt_no=int(it["attempts"])):
                    return
            except Exception:
                return

    from .delivery_result import DeliveryResult
    t0 = time.time()
    delivery_started = False
    try:
        th = threading.Thread(target=_beat, daemon=True)
        th.start()
        delivery_started = True
        outcome = deliver(it, dry=dry)
        ok, ack, why = outcome
        if it.get("_post_send_ledger_missing"):
            ok, ack, why = False, "unconfirmed", "发送已发生但本地落账失败；不自动重发"
    except Exception as exc:
        _log("desk_execution_boundary_failed", id=qid, attempt_no=it.get("attempts"),
             delivery_started=delivery_started, err=repr(exc)[:200])
        outcome = DeliveryResult(False, ack="unconfirmed" if delivery_started else "",
                                 why="执行结果未知；不自动重发" if delivery_started else "心跳启动失败，未调用发送器",
                                 not_executed=not delivery_started)
        ok, ack, why = outcome
    finally:
        hb_stop.set()
    ms = int((time.time() - t0) * 1000)
    att = int(it.get("attempts") or 1)
    backoff = BACKOFF_S[min(att - 1, len(BACKOFF_S) - 1)]
    # 2026-11-04：连续清不掉输入框的那条不再排退避（否则会一直在几个会话之间跳）。
    _terminal = _residue.take_terminal(qid)
    state = W.settle(qid, ok, ack=ack, why=why, backoff_s=backoff, terminal=_terminal,
                     not_executed=getattr(outcome, "not_executed", False) is True, owner=owner, attempt_no=att)
    _log("desk_send", id=qid, kind=it.get("kind"), chat=it.get("chat_id"),
         ok=ok, ack=ack, state=state, ms=ms, attempts=att, why=why)
    if state == "failed":
        # 2026-11-05（主人「ok」）：发不出去也要有人知道 —— 265/266 就是这么静默死掉的
        # （微信窗口激活不起来，重试到 attempts 上限，落了 failed，谁也没被告知）。
        try:
            from loop import owner_copy as _oc2
            _oc2.note("send_failed", target=str(it.get("chat_id") or ""),
                      detail=str(why or "")[:120])
        except Exception as _exc2:  # noqa: BLE001 - 抄送失败不许影响队列
            _log("owner_copy.send_failed_failed", id=qid, err=repr(_exc2)[:100])
    return {"idle": False, "id": qid, "ok": ok, "ack": ack, "state": state,
            "ms": ms, "attempts": att, "why": why, "chat": it.get("chat_id")}


def housekeeping() -> dict:
    """回收僵尸 + TTL 作废。每轮先跑这个，再干活。"""
    from loop import outbox  # 2026-11-02 修 F821：TTL 过期的台账回调一直被吞
    out = {"reaped": 0, "expired": 0}
    try:
        out["reaped"] = W.reap_stale(STALE_S)
    except Exception as exc:  # noqa: BLE001
        out["reap_err"] = repr(exc)[:120]
    try:
        out["expired"] = W.expire(on_expired=outbox.note_expired)
    except Exception as exc:  # noqa: BLE001
        out["expire_err"] = repr(exc)[:120]
    if out["reaped"] or out["expired"]:
        _log("desk_housekeeping", **out)
    return out


def _prewarm() -> None:
    """2026-10-31 提速：把「第一次发送才去初始化」的东西提前付掉。

    实测（18:21 那条冷启动）：一次发送 86 秒里，RapidOCR 模型加载 + 终端层初始化
    占了大头，而它们跟"这一条说的是什么"毫无关系。队列里的消息本来就在等，
    不如在 desk 启动时后台付掉；窗口/布局探针很便宜（0.1s），同步热一下就好。
    """
    t0 = time.time()
    try:
        from core.execution import wx_vision as _v

        try:
            import numpy as _np

            _v.ocr_items(_np.zeros((32, 120, 3), dtype="uint8"))
        except Exception as exc:  # noqa: BLE001 - 预热失败就退回冷启动，别影响发送
            _log("desk_prewarm_ocr_skipped", err=repr(exc)[:100])
        try:
            from core.terminal.store_port import get_account_db_manager

            get_account_db_manager()
        except Exception as exc:  # noqa: BLE001 - 同上：终端层热不起来只是慢一点
            _log("desk_prewarm_terminal_skipped", err=repr(exc)[:100])
        # 2026-11-01 提速：把「输入框是不是空的」这一次 OCR 也提前付掉。
        # box_looks_empty() 只在 OCR 明确说"空"时才写缓存，所以这里不会把
        # 主人打了一半的草稿误记成空模板；真读不出来就什么都不做。
        try:
            from core.execution import wx_chat as _c

            _seed_t0 = time.time()
            _seed = _c.box_looks_empty()
            # 这一次就是"判空 OCR 靠不靠得住"的体检：None → 本进程不再为判空付 OCR。
            _c.mark_box_ocr_trust(_seed is not None)
            _log("desk_prewarm_box", result=str(_seed), ms=int((time.time() - _seed_t0) * 1000),
                 ocr_trusted=bool(_c._BOX_OCR_TRUST[0]))
        except Exception as exc:  # noqa: BLE001 - 预热失败只是慢一点，不影响发送
            _log("desk_prewarm_box_skipped", err=repr(exc)[:100])
        _log("desk_prewarm", ms=int((time.time() - t0) * 1000))
    except Exception as exc:  # noqa: BLE001 - 预热失败不许影响发送
        _log("desk_prewarm_failed", err=repr(exc)[:120])


#: 她自己认的停止哨兵：`all.stop` 是总闸，`desk.stop` 只关发送工。
#: 之前 desk 全文没有一处 .stop 引用，只能被 taskkill —— 于是退出账本里永远只有
#: desk_supervisor 的视角，它自己从来没说过自己是怎么停的（2026-11-05）。
STOP_FILES: tuple[str, ...] = ("all.stop", "desk.stop")


def stop_file_paths(*, logs_dir=None) -> tuple[str, ...]:
    base = str(logs_dir) if logs_dir else os.path.join(ROOT, "logs")
    return tuple(os.path.abspath(os.path.join(base, n)) for n in STOP_FILES)


def stop_requested(*, logs_dir=None) -> bool:
    """任一停止哨兵在，就该体面退出。读不到按「没被关」处理 —— 不许因为 stat 失败自杀。"""
    n_bad = 0
    for _p in stop_file_paths(logs_dir=logs_dir):
        try:
            if os.path.exists(_p):
                return True
        except OSError:
            n_bad += 1
    if n_bad:
        _log("desk_stop_check_failed", n=n_bad)
    return False


def run(interval: float = 1.0, *, dry: bool = False, once: bool = False) -> None:
    try:
        from core.infra import awake as _awake
        _awake.keep_system_awake("desk")   # 关屏可以，别睡
        _log("desk_keep_awake", on=_awake.is_on())
    except Exception as _exc:  # noqa: BLE001
        _log("desk_keep_awake_failed", err=repr(_exc))
    """主循环：一条一条送，队列空就睡 interval 秒。"""
    _bootstrap()
    if not _single_instance():
        print("[desk] 已经有一个 desk 在跑，本进程退出")
        return
    if not dry:
        try:
            _log("desk_observation_recovery", **recover_observations())
        except Exception as exc:
            _log("desk_confirm_writeback_failed", err=repr(exc)[:120])
        # 2026-11-22：重启时把「证据表明一个字都没打」的 unverified 行重排。
        # 16:06 那条隐私提醒就是这种情形——写好了、没打字、旧代码却不重试。
        try:
            _requeued = W.requeue_not_executed(since_ts=time.time() - 6 * 3600)
            if _requeued:
                _log("desk_requeued_not_executed", ids=_requeued)
        except Exception as exc:  # noqa: BLE001 - 重排失败不许挡住 desk 启动
            _log("desk_requeue_failed", err=repr(exc)[:120])
    try:  # 窗口/布局先热一下（0.1s 级），真正的重活交给后台线程
        from core.execution import wx_chat as _c
        from core.execution import wx_vision as _v

        _w = _c._wechat_window(force=True)
        if _w is not None:
            _img, _rect = _v.capture_window(_w)
            _v.detect_layout(_img)
    except Exception as exc:  # noqa: BLE001 - 窗口还没开就先跑，等第一条再热
        _log("desk_warm_window_skipped", err=repr(exc)[:100])
    threading.Thread(target=_prewarm, name="desk-prewarm", daemon=True).start()
    _log("desk_start", pid=os.getpid(), dry=dry, interval=interval)
    print("[desk] 启动 pid=%d dry=%s" % (os.getpid(), dry), flush=True)
    _last_health = 0.0
    _last_requeue = time.time()
    while True:
        if time.time() - _last_health >= 30.0:
            try:
                from core.execution import wx_health as _wxh
                _wxh.write()
            except Exception as exc:  # noqa: BLE001 - 健康探测失败不影响发送
                _log("desk_wechat_health_failed", err=repr(exc)[:100])
            _last_health = time.time()
        if stop_requested():
            _log("desk_stop_file", pid=os.getpid())
            print("[desk] 看到停止哨兵，退出", flush=True)
            return
        if time.time() - _last_requeue >= 300.0:
            # 2026-11-23：今晚一条「没打字就中止」的气泡卡成 unverified，整组后面两条跟着等了 2.5 小时。
            # 启动时重排不够——运行中每 5 分钟扫一次同样白名单，确认没执行的立刻重排。
            _last_requeue = time.time()
            try:
                _again = W.requeue_not_executed(since_ts=time.time() - 6 * 3600)
                if _again:
                    _log("desk_requeued_not_executed", ids=_again, when="periodic")
            except Exception as exc:  # noqa: BLE001 - 扫不动也不许挡住发送
                _log("desk_requeue_failed", err=repr(exc)[:120], when="periodic")
        hk = housekeeping()
        sweep_acks()
        sweep_confirm_starts()
        sweep_confirm_stuck()
        if hk.get("reaped") or hk.get("expired"):
            print("[desk] 清理", hk, flush=True)
        r = execute_one(dry=dry)
        if r.get("idle"):
            if once:
                print("[desk] 队列空，退出", flush=True)
                return
            time.sleep(interval)
        else:
            print("[desk] %s" % r, flush=True)


def status() -> dict:
    st = W.queue_stats(5)
    st["owner"] = "%s-%d" % (OWNER_PREFIX, os.getpid())
    st["lock"] = os.path.exists(LOCK_PATH)
    try:
        from core.execution import wx_health as _wxh

        st["wechat"] = _wxh.read(fresh_s=120.0)
    except Exception:
        st["wechat"] = {"state": "unknown"}
    return st


if __name__ == "__main__":
    args = set(sys.argv[1:])

    def _entry() -> None:
        run(interval=float(os.environ.get("DESK_INTERVAL", "1.0") or 1.0),
            dry=("--dry" in args), once=("--once" in args))

    _exitlog.wrap_main(_entry, role="desk",
                      detail="loop.desk %s" % (" ".join(sorted(args)) or "--watch"),
                      stop_files=stop_file_paths())
