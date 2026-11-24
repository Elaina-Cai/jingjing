"""[world] 同步哨兵：回答"是不是有人写了、我们还没解出来"。

2026-10-28 加的。起因：P6那条消息因为解密指纹只看文件大小被判"没变化"，
在库里卡了 11 分钟，而面板顶栏的"同步 1s"显示一切正常（它比的是解密库和她的
视界，两边一起停时差值是 0）。

这里的判据不看"多久没人说话"（夜里没人说话是正常的，会天天误报），只看
**源库比解密副本新** —— 那才叫"有人写了但没解出来"。

用法：
    python -m db.sync_sentinel once      # 打印对账三条线，异常写日志
    python -m db.sync_sentinel watch 60  # 每 60 秒查一次并写日志（常驻）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from core.infra.envflags import text

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = "wxid_seed02"
SRC_MSG = Path(os.path.expanduser("~")) / "Documents/xwechat_files" / (ACCOUNT + "_eae0") / "db_storage/message/message_0.db"
DEC_DIR = ROOT / "data/decrypted/wechat" / ACCOUNT
DEC_MSG = DEC_DIR / "msg_message_0.db"
ACC_DB = ROOT / "data/platforms/wechat" / ACCOUNT / "account.db"
LAG_WARN_S = float(text("YINYUE_SYNC_LAG_WARN_S", "90") or 90)
ALL_STOP = ROOT / "logs/all.stop"  # 全关总闸：它在的时候别报同步故障

_log = None
try:
    from core.infra.logging import init_logging
    import logging
    init_logging(level=(text("YINYUE_LOG_LEVEL") or "INFO"), log_dir=str(ROOT / "logs"))
    _log = logging.getLogger("db.sync_sentinel")
except Exception as exc:  # pragma: no cover
    print("[warn] 日志初始化失败：%r" % (exc,))


def _mt(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _newest_at(db: Path) -> str:
    if not db.exists():
        return ""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True, timeout=3)
        try:
            names = [r[0] for r in con.execute(
                "select name from sqlite_master where type='table'").fetchall()]
            for t in ("messages", "message", "msg", "Message", "MSG"):
                if t in names:
                    row = con.execute("select max(created_at) from %s" % t).fetchone()
                    return str(row[0] or "") if row else ""
            return ""
        finally:
            con.close()
    except Exception:
        return ""


def _newest_wcdb(db: Path) -> str:
    """WCDB 分表格式：消息散在 Msg_<hash> 里，没有 messages 表。

    2026-10-28 补：_newest_at 只认 messages/message 这类表名，
    而微信 4.x 的库是 Msg_xxx 分片，所以这里按分表取最大时间。
    """
    best = ""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
        try:
            tabs = [r[0] for r in con.execute(
                "select name from sqlite_master where type='table' and (name like 'Msg_%' or name like 'msg_%')")]
            for t in tabs:
                cols = [r[1] for r in con.execute("pragma table_info(\"%s\")" % t)]
                col = next((c for c in ("create_time", "created_at", "timestamp", "sort_seq")
                            if c in cols), "")
                if not col:
                    continue
                row = con.execute("select max(%s) from \"%s\"" % (col, t)).fetchone()
                if row and row[0]:
                    v = int(row[0])
                    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
                    if ts > best:
                        best = ts
        finally:
            con.close()
    except Exception:
        best = ""
    return best


def _newest_merged() -> str:
    """解密侧最新一条：优先读合并库。

    2026-10-28 补：DEC_MSG 指向的 message_*.db 里主表不叫 messages，
    之前这里恒为空串，日志里 dec_newest 一直是 "-"。
    """
    best = ""
    try:
        for p in sorted((ROOT / "data" / "decrypted").glob("**/merge_msg.db")):
            v = _newest_at(p) or _newest_wcdb(p)
            if v > best:
                best = v
    except Exception:
        best = ""
    return best


def check() -> dict:
    # 2026-10-28：用户按「停止全部」时没人解密是正常的，源库必然领先，那不是故障。
    if ALL_STOP.exists():
        if _log:
            _log.debug("sync_sentinel_paused all_stop_present")
        return {"paused": True, "now": time.strftime("%Y-%m-%d %H:%M:%S")}
    src_m = _mt(SRC_MSG)
    dec_m = _mt(DEC_MSG)
    acc_newest = _newest_at(ACC_DB)
    dec_newest = _newest_at(DEC_MSG)
    if not dec_newest:
        dec_newest = _newest_merged()
    pending_s = max(0.0, src_m - dec_m) if (src_m and dec_m) else -1.0
    out = {
        "src_mtime": src_m,
        "dec_mtime": dec_m,
        "src_ahead_s": round(pending_s, 1),
        "dec_newest": dec_newest,
        "ours_newest": acc_newest,
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if pending_s > LAG_WARN_S:
        if _log:
            _log.warning("sync_lag_detected src_ahead_s=%.1f src=%s dec=%s",
                         pending_s,
                         time.strftime("%H:%M:%S", time.localtime(src_m)) if src_m else "-",
                         time.strftime("%H:%M:%S", time.localtime(dec_m)) if dec_m else "-")
        out["warn"] = True
    elif dec_newest and acc_newest and dec_newest[:19] != acc_newest[:19]:
        if _log:
            _log.info("sync_ingest_pending dec_newest=%s ours_newest=%s", dec_newest[:19], acc_newest[:19])
    return out


def once() -> None:
    # 2026-11-02 修 F821：原来先 d.get("paused") 后 d = check() —— 顺序反了，
    # once 路径一进来就 NameError（从来没工作过）。
    d = check()
    if d.get("paused"):
        print("当前是「停止全部」状态（logs/all.stop 在）：哨兵暂停判断，不报警。")
        return
    print("源库 message_0.db : mtime=%s" % time.strftime("%H:%M:%S", time.localtime(d["src_mtime"])))
    print("解密副本          : mtime=%s  最新消息=%s" % (
        time.strftime("%H:%M:%S", time.localtime(d["dec_mtime"])), d["dec_newest"][:19] or "-"))
    print("我们入库          : 最新消息=%s" % (d["ours_newest"][:19] or "-"))
    print("源库领先解密副本  : %.1f 秒（> %.0f 秒算异常）%s" % (
        d["src_ahead_s"], LAG_WARN_S, "  ← 异常" if d.get("warn") else ""))


def watch(interval: float = 60.0) -> None:
    if _log:
        _log.info("sync_sentinel_start interval_s=%s lag_warn_s=%s", interval, LAG_WARN_S)
    while True:
        try:
            check()
        except Exception as exc:  # 哨兵自己绝不能把别人搞崩
            if _log:
                _log.warning("sync_sentinel_error err=%r", exc)
        time.sleep(max(10.0, interval))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    if cmd == "watch":
        watch(float(sys.argv[2]) if len(sys.argv) > 2 else 60.0)
    else:
        once()
