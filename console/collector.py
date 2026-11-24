# -*- coding: utf-8 -*-
"""控制台采集器（只读）。

职责：把散在 logs/ data/ configs/ 里的真相，整形成为一份 state.json，给「鲸鲸面板.html」看。
规矩：
  * 只读，绝不写业务数据（唯一写的是 data/console/ 下的派生文件）。
  * 任何一个来源坏了，都填 null 并给出 note，绝不让整份快照失败。
  * 窗口关掉（找不到标题含「鲸鲸 · 控制台」的窗口）三轮之后自己退出，不留孤儿进程。
"""
from __future__ import annotations

import ctypes
import io
import logging
import json
import queue
import re
import os
import sys
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sqlite3
import time
from datetime import datetime
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if ROOT not in sys.path:   # 作为脚本直接跑时，sys.path[0] 是 console/，得把项目根补回来
    sys.path.insert(0, ROOT)

from core.infra import liveness
from core.infra import duration  # 时长只有一个单位：毫秒（UI-066）
from core.infra.duration import human, to_ms  # 心跳文件名与判死口径只有一处
from core.infra.envflags import text


# pythonw 启动时 sys.stdout/stderr 是 None：structlog 的 PrintLogger 会对 None 建弱引用而崩
# （TypeError: cannot create weak reference to 'NoneType' object）。
# 只要有一个真文件对象，日志链就正常，也顺便给采集器自己留一份输出。
if sys.stdout is None or sys.stderr is None:
    try:
        _out_dir = os.path.join(ROOT, "data", "console")
        os.makedirs(_out_dir, exist_ok=True)
        _fh = open(os.path.join(_out_dir, "collector.out"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or _fh
        sys.stderr = sys.stderr or _fh
    except Exception:  # noqa: BLE001
        pass

LOGS = os.path.join(ROOT, "logs")

# 2026-10-27 晚：统一日志——面板采集器也用 core.infra.logging，
# 关键事件跟调度器/引擎写进同一个 logs/yinyue.log（原先只有 collector.out / .err）。
try:
    from core.infra.logging import init_logging as _init_logging

    _init_logging(level=(text("YINYUE_LOG_LEVEL") or "INFO"), log_dir=LOGS)
except Exception:  # noqa: BLE001
    pass
_log = logging.getLogger("console.collector")
DATA = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(DATA, "console")
STATE_OUT = os.path.join(OUT_DIR, "state.json")
PAGE = os.path.join(ROOT, "console", "panel.html")
ICON = os.path.join(ROOT, "console", "icon.ico")

# 体检：重算比较贵（要扫库、扫日志），15 秒内复用
_CHECKUP_TTL_S = 2.0     # 2026-11-02：15 秒 → 2 秒。整轮实测 687ms/16 项，
                         # 服务是 ThreadingHTTPServer，缩短不会互相卡。
_CHECKUP_CACHE: dict = {"at": 0.0, "data": None}
HISTORY = os.path.join(OUT_DIR, "history.jsonl")
TICK_S = 2.0
VIEW_EVERY_S = 10.0
WINDOW_TITLE = "鲸鲸 · 控制台"
WINDOW_GRACE_S = 90.0  # 窗口可能比采集器晚起来一分钟，别急着退

_cache: dict = {"view": "", "view_at": 0.0}
_MUTEX_H: list = []   # 占住命名内核对象；进程活着就占着，退出由系统释放


def _single_instance() -> bool:
    """真单实例锁：端口 bind 在 Windows 上靠不住（SO_REUSEADDR 允许重复 bind）。

    2026-11-01 实测：第二个 ThreadingHTTPServer 绑 39872 直接成功，于是两个采集器
    并存、各自 2 秒跑一遍 build_state，面板连到哪个不确定（数据时新时旧）。
    main() 里那句 `except OSError: return 0` 因此从来没被触发过。
    """
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        h = k32.CreateMutexW(None, False, "Local\\yinyue_console_collector")
        if not h:
            return True                      # 建不出锁就当没锁，别拦住启动
        if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False
        _MUTEX_H.append(h)
        return True
    except Exception:  # noqa: BLE001 - 锁机制自己坏了不该让面板起不来
        return True


from core.infra import egress


def _now() -> float:
    return time.time()


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _age_s(v) -> float | None:
    """把 ts / at / created_at / last_tick_at 之类的值换算成"多少秒之前"。"""
    if isinstance(v, (int, float)) and v > 1e9:
        return max(0.0, _now() - float(v))
    if not isinstance(v, str) or not v:
        return None
    s = v.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return max(0.0, _now() - time.mktime(time.strptime(s[:26] if "%f" in fmt else s[:19], fmt)))
        except Exception:
            continue
    try:  # 只有时分秒（引擎心跳就是这么写的）
        t = time.strptime(s[:8], "%H:%M:%S")
        now = time.localtime()
        return max(0.0, _now() - time.mktime((now.tm_year, now.tm_mon, now.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, 0, 0, -1)))
    except Exception:
        return None


def _connect(path: str):
    # 2026-10-28：只读契约统一到 db/read_gate.py（含 WAL 库的退路）。
    from db import read_gate as _rg
    return _rg.open_ro(path)

_LABELS: dict = {"at": 0.0, "map": {}}


def _labels() -> dict:
    """wxid / 群 id → 人话名字（读口 db/read_gate.labels，60 秒缓存）。"""
    if _LABELS["map"] and _now() - _LABELS["at"] < 60:
        return _LABELS["map"]
    try:
        from db import read_gate as _rg
        out = _rg.labels()
    except Exception:  # noqa: BLE001
        out = {}
    _LABELS["map"] = out
    _LABELS["at"] = _now()
    return out

def _newest_of(path: str) -> str:
    from db import read_gate as _rg
    return _rg.newest_stamp(path)

def _live_account() -> str:
    """消息最新的那个 account.db（读口提供）。"""
    from db import read_gate as _rg
    return _rg.live_account()

def _candidates() -> list[str]:
    from db import read_gate as _rg
    return _rg.candidates()

_DESC_CACHE = {"mtime": None, "map": {}}


def _image_desc_map() -> dict:
    """读引擎写的图片描述缓存（data/loop/image_desc.json，键是文件名或去后缀名）。"""
    path = os.path.join(ROOT, "data", "loop", "image_desc.json")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    if _DESC_CACHE["mtime"] != mt:
        try:
            raw = json.load(io.open(path, encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存坏了就当没有，不影响面板
            raw = {}
        out = {}
        for k, v in (raw or {}).items():
            d = v.get("d") if isinstance(v, dict) else v
            if d:
                out[str(k)] = str(d)
        _DESC_CACHE["map"] = out
        _DESC_CACHE["mtime"] = mt
    return _DESC_CACHE["map"]


def _desc_for(media_path: str) -> str:
    mp = str(media_path or "")
    if not mp:
        return ""
    name = os.path.basename(mp.replace("\\", "/"))
    stem = os.path.splitext(name)[0]
    alt = name.replace("_t.", ".")
    cands = [name, stem, alt, os.path.splitext(alt)[0], mp]
    m = _image_desc_map()
    for k in cands:
        if m.get(k):
            return m[k]
    return ""


_OCR_LOCK = threading.Lock()
_OCR_MEM: dict = {"loaded": False, "map": {}}
_OCR_QUEUE: "queue.Queue[str]" = queue.Queue()
_OCR_WORKER: dict = {"thread": None}


def _ocr_cache_path() -> str:
    return os.path.join(OUT_DIR, "image_ocr.json")


def _ocr_load() -> dict:
    """把 OCR 抓字缓存读进内存（只读一次）。"""
    with _OCR_LOCK:
        if not _OCR_MEM["loaded"]:
            try:
                _OCR_MEM["map"] = json.load(io.open(_ocr_cache_path(), encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                _OCR_MEM["map"] = {}
            _OCR_MEM["loaded"] = True
        return _OCR_MEM["map"]


def _ocr_save() -> None:
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        tmp = _ocr_cache_path() + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_OCR_MEM["map"], fh, ensure_ascii=False)
        os.replace(tmp, _ocr_cache_path())
    except Exception:  # noqa: BLE001 - 缓存写不进去不影响面板
        pass


def _ocr_worker() -> None:
    """后台串行把图片 OCR 成文字（RapidOCR，一张约 0.6~1 s）。"""
    while True:
        rel = _OCR_QUEUE.get()
        try:
            path = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
            if not os.path.exists(path):
                continue
            from loop import vision as _vision
            txt = _vision.image_text(path, max_chars=160)
            with _OCR_LOCK:
                _OCR_MEM["map"][os.path.basename(path)] = {
                    "t": str(txt or ""), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                _OCR_MEM["loaded"] = True
            _ocr_save()
        except Exception:  # noqa: BLE001
            pass
        finally:
            _OCR_QUEUE.task_done()


def _ocr_for(media_path: str) -> str:
    """返回这张图的 OCR 抓字（没算过就排队，本次先返回空）。"""
    mp = str(media_path or "")
    if not mp:
        return ""
    key = os.path.basename(mp.replace("\\", "/"))
    m = _ocr_load()
    got = m.get(key)
    if got is not None:
        return str(got.get("t") or "")
    if _OCR_WORKER["thread"] is None:
        th = threading.Thread(target=_ocr_worker, name="ocr-worker", daemon=True)
        th.start()
        _OCR_WORKER["thread"] = th
    _OCR_QUEUE.put(mp)
    with _OCR_LOCK:
        _OCR_MEM["map"].setdefault(key, {"t": "", "at": ""})   # 占位，避免重复排队
    return ""


def _world(db_path: str) -> dict:
    """她的世界：最近的消息流（按会话分组），以及新鲜度。"""
    info = {"db": db_path, "note": "", "chats": [], "max_at": None, "lag_s": None, "people": 0}
    if not db_path:
        info["note"] = "没找到 account.db"
        return info
    try:
        con = _connect(db_path)
        rows = con.execute(
            "select chat_id, is_self, sender_name, content, created_at, media_path, "
                "(select mm.description from message_media mm where mm.message_id = messages.id "
                "and ifnull(mm.description,'')<> '' order by mm.id desc limit 1) as media_desc "
            "from messages order by created_at desc limit 400"
        ).fetchall()
        con.close()
    except Exception as exc:  # noqa: BLE001
        info["note"] = "读 account.db 失败：%r" % (exc,)
        return info
    chats: dict[str, dict] = {}
    for r in rows:
        cid = str(r["chat_id"] or "")
        c = chats.get(cid)
        if c is None:
            c = chats[cid] = {"chat_id": cid, "lines": [], "last_at": None, "who": ""}
        if len(c["lines"]) >= 6:
            continue
        _raw = str(r["content"] or "")
        # 2026-11-01：`_is_sys` 原先只在 `_world_multi` 里定义，这里漏了 —— 下面
        # 两处却要用它。兜底路径（找不到账号库时 `build_state` 走 `_world("")`）
        # 一进来就 NameError，被外层 except 吞成一句"读不出来"。
        # 判据与 `_world_multi` 保持同一条。
        _is_sys = bool("<sysmsg" in _raw or _raw.lstrip().startswith("<?xml"))
        # 2026-10-27：撤回/拍一拍/改群名这类系统消息（msg_type=10000）在库里是原始
        # XML，面板直接显示会糊一屏。清洗规则与引擎侧 loop/snapshot.py 的
        # human_sysmsg 是同一份实现（直接 import，避免两边规则漂移）。
        if _is_sys:
            try:
                from loop.snapshot import human_sysmsg as _hs
                _raw = _hs(_raw)
            except Exception:  # noqa: BLE001 - 面板宁可显示占位符也不能整块崩
                _raw = "（系统消息）"
        try:
            _mp = str(r["media_path"] or "")
        except Exception:  # noqa: BLE001 - 老库没有这列就当作没图
            _mp = ""
        if _raw.startswith("[图片"):
            # 2026-10-27：她视界里图片本来是「[图片: 描述]」（loop/snapshot.py 里
            # vision.describe 拼的），面板却把描述削掉只留 [图片]。这里用同一份
            # 缓存 data/loop/image_desc.json 还原，没描述就显式标出来。
            try:
                _desc = str(r["media_desc"] or "").strip()
            except Exception:  # noqa: BLE001
                _desc = ""
            if not _desc:
                _desc = _desc_for(_mp)
            txt = ("[图片: %s]" % _desc) if _desc else "[图片·未描述]"
        else:
            txt = re.sub(r"\[([^\[\]=:]{1,10})[:=][^\]]*\]", r"[\1]", _raw)
        txt = txt.replace("\n", " / ")[:220]
        c["lines"].append({"at": str(r["created_at"]), "self": bool(r["is_self"]),
                           "who": ("系统" if _is_sys else str(r["sender_name"] or "")), "text": txt, "ocr": _ocr_for(_mp)})
        if c["last_at"] is None:
            c["last_at"] = str(r["created_at"])
            c["who"] = ("系统" if _is_sys else str(r["sender_name"] or ""))
    labels = _labels()
    _parts = db_path.replace("\\", "/").split("/")
    _plat = ""
    if "platforms" in _parts:
        _i = _parts.index("platforms")
        if _i + 1 < len(_parts):
            _plat = _parts[_i + 1]
    _plat_label = {"wechat": "微信", "qq": "QQ"}.get(_plat, _plat)
    info["platform"] = _plat
    info["chats"] = [dict(v, name=labels.get(v["chat_id"], ""), platform=_plat_label)
                     for v in chats.values()]
    if rows:
        info["max_at"] = str(rows[0]["created_at"])
        a = _age_s(info["max_at"])
        info["lag_s"] = a
    return info


_QQ_PROBE: dict = {"at": 0.0, "open": None, "url": ""}


def _qq_ws_probe() -> dict:
    """QQ 的 OneBot WebSocket 到底通不通（NapCat 活着才有 3001）。15 秒缓存。

    2026-10-27：面板上 QQ 一直显示「最新 10-15」，但看不出是"群里没人说话"
    还是"NapCat 根本没跑"。这里直接 tcP 探一下，答得干脆。
    """
    if _QQ_PROBE["at"] and _now() - _QQ_PROBE["at"] < 15:
        return dict(_QQ_PROBE)
    url = (os.environ.get("QQ_WS_URL") or "ws://127.0.0.1:3001").strip()
    host, port = "127.0.0.1", 3001
    m = re.match(r"^wss?://([^/:]+)(?::(\d+))?", url)
    if m:
        host = m.group(1) or host
        if m.group(2):
            port = int(m.group(2))
    opened = False
    try:
        with egress.connect(host, port, timeout_ms=350):
            opened = True
    except Exception:  # noqa: BLE001
        opened = False
    _QQ_PROBE.update({"at": _now(), "open": opened, "url": url})
    return dict(_QQ_PROBE)


_WORLD_WINDOW_H: float = float(text("YINYUE_CONSOLE_WINDOW_H", "48") or 48.0)


def _plat_of(path: str) -> tuple[str, str]:
    """data/platforms/<平台>/<账号>/account.db → (平台, 账号)。"""
    parts = path.replace("\\", "/").split("/")
    if "platforms" in parts:
        i = parts.index("platforms")
        return (parts[i + 1] if i + 1 < len(parts) else "",
                parts[i + 2] if i + 2 < len(parts) else "")
    return "", ""


def _world_multi(window_h: float | None = None) -> dict:
    """她的世界：把各平台、各账号的消息合成一条时间线（默认只看最近 48 小时）。

    2026-10-27 之前只读"最新那一个账号库"，于是 QQ 那边永远不出现在面板上；
    现在按平台合并，并在 sources 里逐条说明每个库的最新时间与是否有新消息。
    """
    window_h = _WORLD_WINDOW_H if window_h is None else window_h
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.localtime(time.time() - window_h * 3600.0))
    info = {"db": "", "note": "", "chats": [], "max_at": None, "lag_s": None,
            "people": 0, "platform": "multi", "window_h": window_h,
            "sources": [], "accounts": []}
    merged: list = []
    for p in _candidates():
        plat, acc = _plat_of(p)
        label = {"wechat": "微信", "qq": "QQ"}.get(plat, plat)
        newest = _newest_of(p)
        info["accounts"].append({"platform": label, "account": acc,
                                 "newest": newest, "db": p})
        try:
            con = _connect(p)
            rows = con.execute(
                "select chat_id, is_self, sender_name, content, created_at, media_path, "
                "(select mm.description from message_media mm where mm.message_id = messages.id "
                "and ifnull(mm.description,'')<> '' order by mm.id desc limit 1) as media_desc "
                "from messages where created_at >= ? order by created_at desc limit 400",
                (cutoff,),
            ).fetchall()
            con.close()
        except Exception as exc:  # noqa: BLE001
            info["sources"].append("%s %s 读失败：%r" % (label, acc, exc))
            continue
        # 2026-11-01：`newest` 是数据库原始时间（2026-11-01 18:29:05.000000），
        # 直接丢到面板上等于把微秒糊在主人脸上。这里只留到分钟。
        _nw = str(newest or "")
        _m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2})", _nw)
        if _m:
            _nw = (_m.group(4) if _m.group(1) == time.strftime("%Y") and
                   _m.group(2) == time.strftime("%m") and _m.group(3) == time.strftime("%d")
                   else "%s-%s %s" % (_m.group(2), _m.group(3), _m.group(4)))
        info["sources"].append("%s %s %s%s" % (
            label, acc, _nw or "无消息",
            "" if rows else "（近 %gh 没新消息）" % window_h))
        merged.extend((plat, label, r) for r in rows)
    merged.sort(key=lambda x: str(x[2]["created_at"]), reverse=True)
    merged = merged[:400]
    chats: dict = {}
    for plat, label, r in merged:
        cid = str(r["chat_id"] or "")
        c = chats.get(cid)
        if c is None:
            c = chats[cid] = {"chat_id": cid, "lines": [], "last_at": None,
                              "who": "", "platform": label, "plat_key": plat}
        if len(c["lines"]) >= 6:
            continue
        _raw = str(r["content"] or "")
        _is_sys = bool("<sysmsg" in _raw or _raw.lstrip().startswith("<?xml"))
        if _is_sys:
            try:
                from loop.snapshot import human_sysmsg as _hs
                _raw = _hs(_raw)
            except Exception:  # noqa: BLE001
                _raw = "（系统消息）"
        try:
            _mp = str(r["media_path"] or "")
        except Exception:  # noqa: BLE001 - 老库没有这列就当作没图
            _mp = ""
        if _raw.startswith("[图片"):
            # 2026-10-27：她视界里图片本来是「[图片: 描述]」（loop/snapshot.py 里
            # vision.describe 拼的），面板却把描述削掉只留 [图片]。这里用同一份
            # 缓存 data/loop/image_desc.json 还原，没描述就显式标出来。
            try:
                _desc = str(r["media_desc"] or "").strip()
            except Exception:  # noqa: BLE001
                _desc = ""
            if not _desc:
                _desc = _desc_for(_mp)
            txt = ("[图片: %s]" % _desc) if _desc else "[图片·未描述]"
        else:
            txt = re.sub(r"\[([^\[\]=:]{1,10})[:=][^\]]*\]", r"[\1]", _raw)
        txt = txt.replace("\n", " / ")[:220]
        c["lines"].append({"at": str(r["created_at"]), "self": bool(r["is_self"]),
                           "who": ("系统" if _is_sys else str(r["sender_name"] or "")), "text": txt, "ocr": _ocr_for(_mp)})
        if c["last_at"] is None:
            c["last_at"] = str(r["created_at"])
            c["who"] = ("系统" if _is_sys else str(r["sender_name"] or ""))
    labels = _labels()
    info["chats"] = [dict(v, name=labels.get(v["chat_id"], "")) for v in chats.values()]
    if merged:
        info["max_at"] = str(merged[0][2]["created_at"])
        info["lag_s"] = _age_s(info["max_at"])
    if not merged:
        info["note"] = ("最近 %g 小时里各平台都没有新消息" % window_h)
    info["qq"] = _qq_ws_probe()
    return info


def _stamp(v) -> float | None:
    if not v:
        return None
    try:
        return time.mktime(time.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


_DEC_CACHE = {"at": 0.0, "newest": 0}
_DEC_TTL_S = 15.0


def _decrypted_lag() -> dict:
    """解密库里最新一条 vs 她看到的最新一条 —— 差多少就是"她还没吃到的世界"。

    扫一遍要开 6 个 106MB 的库（实测 152 ms），而「最新消息时间」分钟级才变一次，
    所以 15 秒内复用上次结果；lag_s 仍用当下时间算，差多少秒依旧是实时的。
    """
    out = {"newest": None, "lag_s": None, "note": ""}
    now = _now()
    if now - float(_DEC_CACHE["at"] or 0) > _DEC_TTL_S:
        base = os.path.join(DATA, "decrypted")
        newest = 0
        for r, _d, files in os.walk(base):
            for f in files:
                if not (f.startswith("msg_message") and f.endswith(".db")) or "_fts" in f or "resource" in f:
                    continue
                p = os.path.join(r, f)
                try:
                    con = _connect(p)
                    tables = [x[0] for x in con.execute("select name from sqlite_master where type='table'")]
                    for t in tables:
                        if not t.lower().startswith("msg"):
                            continue
                        try:
                            row = con.execute('select max(create_time) as t from "%s"' % t).fetchone()
                            newest = max(newest, int(row["t"] or 0))
                        except Exception:  # noqa: BLE001
                            continue
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
        _DEC_CACHE["newest"] = newest
        _DEC_CACHE["at"] = now
    newest = int(_DEC_CACHE["newest"] or 0)
    if newest:
        out["newest"] = datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M:%S")
        out["lag_s"] = max(0.0, now - newest)
    return out


def _vitals() -> dict:
    d = _read_json(os.path.join(DATA, "loop", "self_state.json"))
    p = d.get("pulse") or {}
    last_spoke = p.get("last_spoke") or 0.0
    stances = [{"who": v.get("who", ""), "text": v.get("text", "")}
               for v in (d.get("scoped_stance") or {}).values()]
    stances.extend({"who": k, "text": (v or {}).get("text", "")} for k, v in (d.get("stance") or {}).items())
    return {
        "social": round(float(p.get("social") or 0.0), 3),
        "arousal": round(float(p.get("arousal") or 0.0), 3),
        "spont_streak": int(p.get("spont_streak") or 0),
        "last_spoke_age_text": duration.human_s(_age_s(last_spoke) or 0.0),
        "mood": (d.get("mood") or {}).get("text") or "",
        "doing": (d.get("doing") or {}).get("text") or "",
        "stance": stances[:6],
    }


def _ledger() -> dict:
    from core.infra import actions as _actions

    day = datetime.now().strftime("%Y-%m-%d")
    path = _actions.path(day)
    # 2026-11-01（切换即删）：这份里原本还发一份开口列表和一份回执数 ——
    # 开口列表面板早就改读 outbound.rows（同样的开口，那份还带 stage/who/why）；
    # 回执数恒为 0（actions 日志里没有这类行），账页却照着它显示"已证实 0 / 未证实 0"，
    # 真数在 outbound.counts 里。留着只是每 2 秒多传 7KB、还给人看错数字。
    out = {"date": day, "acts": {}, "silents": 0, "promises": [], "promise_open": 0, "note": ""}
    if not os.path.exists(path):
        out["note"] = "今天还没有账本文件"
        return out
    closed = set()
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as exc:  # noqa: BLE001
        out["note"] = "读账本失败：%r" % (exc,)
        return out
    for r in rows:
        kind = r.get("kind")
        if kind == "delivery":
            continue      # 回执口径已统一到 outbound.counts，这里不再另算一份
        if kind == "promise_closed":
            closed.add(str(r.get("ref")))
            continue
        act = r.get("act")
        if not act:
            continue
        out["acts"][act] = out["acts"].get(act, 0) + 1
        if act == "silent":
            out["silents"] += 1
    for r in rows:
        if r.get("kind") == "promise":
            pid = str(r.get("id"))
            out["promises"].append({"at": r.get("at"), "who": r.get("target") or r.get("who") or "",
                                    "what": r.get("what") or "", "due": r.get("due") or "",
                                    "open": pid not in closed})
    out["promise_open"] = sum(1 for p in out["promises"] if p["open"])
    return out


def _engine() -> dict:
    """引擎是懒的：世界不变它可以二十分钟不落盘，所以"活着"看进程，不看心跳时间。"""
    d = liveness.read("engine")
    pid = d.get("pid")
    alive = liveness.pid_alive(pid) if pid else False
    age = liveness.age_ms("engine") if d else None
    return {"state": "ok" if alive else ("missing" if not d else "bad"),
            "alive": bool(alive), "pid": pid, "ticks": d.get("ticks"),
            "last_tick_at": d.get("last_tick_at"), "idle": d.get("idle"),
            "dry_run": d.get("dry_run"),
            "age_ms": age, "age_text": duration.human(age) if age is not None else ""}


def _main_proc() -> dict:
    d = liveness.read("scheduler")
    age = liveness.age_ms("scheduler") if d else None
    state = "missing" if not d else (
        "stale" if (age is None or age > to_ms("60s")) else "ok")
    return {"state": state, "pid": d.get("pid"), "ticks": d.get("main_loop_ticks"),
            "job": d.get("job") or "", "uptime_ms": d.get("uptime_ms"),
            "age_ms": age, "age_text": duration.human(age) if age is not None else ""}


def _notebook() -> dict:
    """她的小本本：notes.jsonl 是现役写口；self_history/memo 只读兼容。

    旧版这里直接读 self_history.jsonl + memo.jsonl。2026-11-08 统一后，
    日记和常驻记忆都在 notes.jsonl，按 note_id/revision 折叠并过滤撤销版。
    """
    out = {"days": [], "memo": [], "n_days": 0, "n_memo": 0,
           "files": {"days": "data/loop/notes.jsonl", "memo": "data/loop/notes.jsonl",
                     "legacy": ["data/loop/self_history.jsonl", "data/loop/memo.jsonl"]}}

    def _rows(name: str) -> list:
        rows = []
        try:
            with open(os.path.join(ROOT, "data", "loop", name), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(d, dict) and d.get("text"):
                        rows.append(d)
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            pass
        return rows

    def _latest(rows: list) -> list:
        latest, passthrough = {}, []
        for pos, row in enumerate(rows):
            nid = str(row.get("note_id") or "").strip()
            if not nid:
                passthrough.append(row)
                continue
            try:
                rev = max(1, int(row.get("revision") or 1))
            except (TypeError, ValueError):
                rev = 1
            prev = latest.get(nid)
            if prev is None or rev >= prev[0]:
                latest[nid] = (rev, pos, row)
        merged = [row for _, _, row in latest.values()] + passthrough
        return [row for row in merged if str(row.get("status") or "active").lower() != "revoked"]

    kinds = {"promise": "答应过", "fact": "事实", "rule": "规矩", "note": "记着"}
    from loop import notes

    journal = notes._journal_path()
    if journal:
        notes_rows = _latest(notes._journal_rows(journal))
        out["files"].update(days=journal, memo=journal)
    else:
        notes_rows = _latest(_rows("notes.jsonl"))
    legacy_days = _rows("self_history.jsonl")
    legacy_memo = _rows("memo.jsonl")

    days = [row for row in notes_rows if str(row.get("kind") or "") == "diary"]
    for row in legacy_days:
        days.append({"day": str(row.get("day") or ""), "at": str(row.get("at") or ""),
                     "text": str(row.get("text") or "")})
    seen_days, unique_days = set(), []
    for row in sorted(days, key=lambda item: str(item.get("at") or item.get("day") or "")):
        key = (str(row.get("day") or str(row.get("at") or "")[:10]), str(row.get("text") or ""))
        if key not in seen_days:
            seen_days.add(key)
            unique_days.append(row)
    out["n_days"] = len(unique_days)
    for d in reversed(unique_days[-40:]):
        body = str(d.get("text") or "")
        day = str(d.get("day") or str(d.get("at") or "")[:10])
        out["days"].append({"day": day, "at": str(d.get("at") or ""),
                            "chars": len(body), "text": _clip(body, 900)})

    memo = [row for row in notes_rows if str(row.get("kind") or "") != "diary"]
    for row in legacy_memo:
        memo.append({"at": str(row.get("at") or ""), "kind": str(row.get("kind") or ""),
                     "target": str(row.get("target") or ""), "text": str(row.get("text") or "")})
    seen_memo, unique_memo = set(), []
    for row in sorted(memo, key=lambda item: str(item.get("at") or "")):
        text = str(row.get("text") or "")
        if text and text not in seen_memo:
            seen_memo.add(text)
            unique_memo.append(row)
    out["n_memo"] = len(unique_memo)
    for d in reversed(unique_memo[-60:]):
        out["memo"].append({"at": str(d.get("at") or ""),
                            "kind": str(d.get("kind") or ""),
                            "kind_label": kinds.get(str(d.get("kind") or ""), "记着"),
                            "target": str(d.get("target") or ""),
                            "text": _clip(str(d.get("text") or ""), 240)})

    if not out["days"] and not out["memo"]:
        out["note"] = "小本本还是空的：日记每天凌晨 3 点写，常驻记忆是她用 remember 记的。"
    return out


def _config() -> dict:
    cfg = _read_json(os.path.join(ROOT, "configs", "runtime.json"))
    return {"engine_port": cfg.get("engine_port"), "engine_poll_s": cfg.get("engine_poll_s"),
            "quiet_hours": cfg.get("quiet_hours"), "watchdog_interval_s": cfg.get("watchdog_interval_s"),
            "sentinels": cfg.get("sentinels")}


def _persona() -> dict:
    base = os.path.join(DATA, "persona", "cards")
    out = {"name": "", "version": None, "identity": "", "values": [], "redlines": [], "note": ""}
    try:
        act = _read_json(os.path.join(DATA, "persona", "active.json"))
        name = act.get("card") or act.get("name") or act.get("active") or ""
        card = _read_json(os.path.join(base, "%s.json" % name)) if name else {}
        if not card:
            for f in os.listdir(base):
                if f.endswith(".json") and f != "active.json":
                    card = _read_json(os.path.join(base, f))
                    name = name or f[:-5]
                    break
        out["name"] = name
        out["identity"] = str(card.get("identity") or "")[:400]
        out["values"] = [str(x)[:120] for x in (card.get("values") or [])][:8]
        out["redlines"] = [str(x)[:120] for x in (card.get("redlines") or [])][:8]
    except Exception as exc:  # noqa: BLE001
        out["note"] = "读人格卡失败：%r" % (exc,)
    return out


def _is_panel_title(title: str) -> bool:
    """窗口标题是不是「鲸鲸面板」？

    2026-10-30：这里原来整串比 ``WINDOW_TITLE``（"鲸鲸 · 控制台"），而面板页的
    标题带版本号（"鲸鲸_v2 · 控制台"），于是一个也匹配不上 —— 采集器以为窗口
    关着，90 秒后自杀，面板就变成 "Failed to fetch"。改成按词判：两个词都在就算。
    """
    t = str(title or "")
    return "鲸鲸" in t and "控制台" in t


_NOTE_AT: dict[str, float] = {}


def _note(kind: str, detail: str = "") -> None:
    """把采集器的处境落一行到 collector.err；同类 5 分钟只写一次，免得刷满。"""
    now = _now()
    if now - _NOTE_AT.get(kind, 0.0) < 300.0:
        return
    _NOTE_AT[kind] = now
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        line = "[%s] %s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, detail[:300])
        with open(os.path.join(OUT_DIR, "collector.err"), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:  # noqa: BLE001 - 记不下来也不能把采集器带走
        # 2026-11-01：这里是最后一道（连 .err 都写不了了），走 stdlib logging
        # 经 lastResort 落到 stderr，与原来 print 的落点一致，但不再触发 T201。
        logging.getLogger("console.collector").warning("collector note failed: %r", exc)


def _window_alive() -> bool | None:
    """面板窗口还在吗？

    True / False 是「枚举成功，看到 / 没看到」；None 是「压根没枚举成」。
    2026-10-30：以前「读不到」和「关了」是同一个返回值，采集器会因此退出——
    读不到不该等于关掉了（踩坑 #42/#53 的家法）。
    """
    found = [False]

    def cb(hwnd, _lp):
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
            if _is_panel_title(buf.value):   # 标题里的版本号（_v2 等）会变，不能整串比
                found[0] = True
                return False
        return True

    try:
        ctypes.windll.user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(cb), 0)
    except Exception as exc:  # noqa: BLE001 - 枚举失败不等于窗口关了
        _note("window_enum_failed", repr(exc)[:200])
        return None
    return found[0]


def _append_history(v: dict) -> list[dict]:
    # 2026-11-01（第 4 批·有界化）：这里原来每 2 秒把整个 history.jsonl 读进内存、
    # 只用最后 240 行 —— 文件已经长到 7MB 且没有任何轮转策略（~2.6MB/天，无限涨）。
    # 两步收紧：
    #   ① 读：只读尾部（256KB 上限，240 行用不到这么多）；
    #   ② 写：按 logs 那套同一条轮转策略切分（10MB × 3，见 log_rotate.DEFAULT_POLICIES，
    #      env YINYUE_LOG_ROTATE_DISABLE=1 可整体关掉）。这里没有常驻句柄，
    #      rename 路径是干净的；失败也不影响采集（下次再试）。
    try:
        from core.infra.log_rotate import rotate_if_needed

        rotate_if_needed(HISTORY)
    except Exception as exc:  # noqa: BLE001 - 轮转失败不能把体检历史写断
        from safelog import warn

        warn("history_rotate_failed", err=repr(exc)[:120])
    try:
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": _now(), "social": v["social"], "arousal": v["arousal"]}) + "\n")
    except Exception:
        pass
    pts: list[dict] = []
    try:
        for line in _tail_lines(HISTORY, 256 * 1024)[-240:]:
            try:
                pts.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return pts


# ---------------------------------------------------------------- P1 记录层 --
TRACE_DIR = os.path.join(ROOT, "data", "loop") if "ROOT" in dir() else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "loop")
DETAIL_TURNS = 6


def _tail_lines(path: str, max_bytes: int = 768 * 1024) -> list[str]:
    """只读文件尾部，避免一天几十 MB 的记录把采集器拖慢。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()          # 丢掉被截断的半行
            data = fh.read()
        return data.decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def _trace_files(limit: int = 2) -> list[str]:
    try:
        names = sorted(n for n in os.listdir(TRACE_DIR) if n.startswith("trace-") and n.endswith(".jsonl"))
    except Exception:
        return []
    return [os.path.join(TRACE_DIR, n) for n in names[-limit:]]


def _clip(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 24] + "\n…[截断]…\n" + s[-16:]


def _compact(rec: dict) -> dict:
    out = rec.get("outcome") or {}
    steps = rec.get("steps") or []
    calls = rec.get("calls") or []
    said = out.get("said") or []
    _first = (said[0] if said else {}) or {}
    return {
        "turn_id": rec.get("turn_id") or "",
        "at": rec.get("at") or "",
        "scene": rec.get("scene") or "",
        "dry_run": bool(rec.get("dry_run")),
        "silent": bool(out.get("silent")),
        "said_n": len(said),
        "said_first": _clip(_first.get("text") or out.get("silent_why") or "", 90),
        "why": _clip(_first.get("why") or out.get("silent_why") or "", 160),
        "steps": [{"tool": s.get("tool") or "", "result": (s.get("receipt") or {}).get("result") or ""}
                  for s in steps],
        "ms": out.get("ms") or 0,
        "tokens": sum(int(c.get("tokens") or 0) for c in calls),
        "model": (calls[0].get("model") if calls else "") or "",
        "prompt_chars": (rec.get("prompt") or {}).get("chars") or 0,
    }


def _detail(rec: dict) -> dict:
    pr = rec.get("prompt") or {}
    return {
        "turn_id": rec.get("turn_id") or "",
        "at": rec.get("at") or "",
        "scene": rec.get("scene") or "",
        "db": rec.get("db") or "",
        "dry_run": bool(rec.get("dry_run")),
        "wake": rec.get("wake") or {},
        "trigger_message": rec.get("trigger_message") or "",
        "system": _clip(pr.get("system") or "", 2600),
        "parts": [{"name": p.get("name") or "", "chars": p.get("chars") or 0,
                   "text": _clip(p.get("text") or "", 2600)} for p in (pr.get("parts") or [])],
        "calls": [{"i": c.get("i"), "ms": c.get("ms"), "model": c.get("model") or "",
                   "tokens": c.get("tokens") or 0,
                   "content": _clip(c.get("content") or "", 1400),
                   "reasoning": _clip(c.get("reasoning") or "", 2600),
                   "tools": [t.get("name") for t in (c.get("tool_calls") or [])],
                   "error": c.get("error") or ""} for c in (rec.get("calls") or [])],
        "steps": [{"i": s.get("i"), "tool": s.get("tool") or "", "ms": s.get("ms"),
                   "args": _clip(s.get("args") or "", 500),
                   "obs": _clip(s.get("obs") or "", 700),
                   "receipt": s.get("receipt") or {}} for s in (rec.get("steps") or [])],
        "outcome": rec.get("outcome") or {},
    }


def _trace() -> dict:
    """最近若干拍的"她想了什么 / 说出去什么"。"""
    recs: list[dict] = []
    for path in _trace_files(2):
        for line in _tail_lines(path):
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
    recs = recs[-24:]
    turns = [_compact(r) for r in reversed(recs)]
    details = {}
    for r in recs[-DETAIL_TURNS:]:
        d = _detail(r)
        details[d["turn_id"]] = d
    # 口：今天她说出去的每一段（带原文、气泡数、回执）
    said = []
    for r in recs:
        if r.get("dry_run"):
            continue
        out = r.get("outcome") or {}
        for s in (out.get("said") or []):
            said.append({
                "at": r.get("at") or "", "turn_id": r.get("turn_id") or "",
                "target": s.get("target") or "", "ack": s.get("ack") or "",
                "text": _clip(s.get("text") or "", 600),
                "why": _clip(s.get("why") or "", 300),
                "chars": len(s.get("text") or ""),
            })
        for b in (out.get("blocked") or []):
            said.append({
                "at": r.get("at") or "", "turn_id": r.get("turn_id") or "",
                "target": b.get("target") or "", "ack": "blocked",
                "text": "", "why": _clip(b.get("reason") or "", 300), "chars": 0,
            })
    return {
        "turns": turns,
        "detail": details,
        "said": said[-12:],
        "n_turns_today": len(recs),
        "note": "" if recs else "还没有记录：引擎下一拍就会写 data/loop/trace-YYYY-MM-DD.jsonl",
    }


_LOG_TS = re.compile(r'"timestamp":\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d)')
_LOG_LV = re.compile(r'"level":\s*"([a-zA-Z]+)"')


def _logs_tail(n: int = 400) -> dict:
    # 2026-11-01：「志」页一直是空的 —— 面板读 s.logs.events，可后端从来没人写这一段。
    # logs/yinyue.log 其实是**结构化 JSON 行**（structlog），不是富文本控制台。
    # 2026-11-01 晚：密度图要覆盖 512KB 的全部行，但整行 json.loads 太贵（3701 行 ≈ 90 ms）。
    # 直方图只用到「哪一分钟 / 什么级别」，用正则扫；只有最后 n 条才完整解析。
    fp = os.path.join(LOGS, "yinyue.log")
    if not os.path.exists(fp):
        return {"files": [], "events": []}
    try:
        lines = _tail_lines(fp, 512 * 1024)
    except Exception as exc:  # noqa: BLE001
        return {"files": [os.path.basename(fp)], "events": [], "error": repr(exc)[:200]}
    ev = []
    hist: dict = {}   # "HH:MM" → [总条数, error, warning]
    total = len(lines)
    keep = max(0, total - int(n))
    for idx, raw in enumerate(lines):
        ln = re.sub(r"\x1b\[[0-9;]*m", "", raw).strip()
        if not ln:
            continue
        m_ts = _LOG_TS.search(ln)
        if m_ts:
            b = hist.setdefault(m_ts.group(1)[11:16], [0, 0, 0])
            b[0] += 1
            m_lv = _LOG_LV.search(ln)
            lv = (m_lv.group(1).lower() if m_lv else "")
            if lv in ("error", "critical"):
                b[1] += 1
            elif lv == "warning":
                b[2] += 1
        if idx < keep:
            continue
        try:
            import json as _json
            rec = _json.loads(ln)
        except Exception:
            rec = None
        if isinstance(rec, dict) and rec.get("event"):
            ts = str(rec.get("timestamp") or "")
            extra = {k: v for k, v in rec.items()
                     if k not in ("event", "logger", "level", "timestamp", "seconds")}
            bits = []
            for k, v in extra.items():
                bits.append("%s=%s" % (k, v if not isinstance(v, (dict, list)) else "…"))
            ev.append({"t": ts[11:19] if len(ts) >= 19 else "",
                       "level": str(rec.get("level") or ""),
                       "src": str(rec.get("logger") or ""),
                       "text": (str(rec.get("event")) + (" " + " ".join(bits) if bits else ""))[:400]})
            continue
        ev.append({"t": "", "level": "", "src": "", "text": ln[:400]})
    return {"files": [os.path.basename(fp)], "events": ev,
            "hist": [{"t": k, "n": v[0], "bad": v[1], "warn": v[2]} for k, v in hist.items()]}


def build_state() -> dict:
    dbs = _candidates()
    world = _world_multi() if dbs else _world("")
    vit = _vitals()
    led = _ledger()
    dec = _decrypted_lag()
    return {
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "engine": _engine(),
        "main": _main_proc(),
        "world": dict(world, **{
            "sync_gap_s": (round(max(0.0, (_stamp(dec.get("newest")) or 0.0) - (_stamp(world.get("max_at")) or 0.0)), 1)
                           if dec.get("newest") and world.get("max_at") else None),
        }),
        "decrypted": dec,
        "vitals": vit,
        "history": _append_history(vit),
        "ledger": led,
        "notebook": __import__("collector_ext").notebook2(),
        "config": _config(),
        "persona": _persona(),
        "trace": _trace(), "say_stats": __import__("collector_ext").say_stats2(), "caps": __import__("collector_ext").caps_text(),
        "outbound": __import__("collector_ext").outbound_view(24),
        # 2026-11-01（切换即删）：`delivery` 与 `outbound` 是同一份数据的两次取用
        # （同一个 15 秒缓存），前者每轮多传 20KB；删掉后对账只读 outbound。
        # `queue` 保留：它问的是"此刻队列里压着什么"，无时间窗、带 attempts/age_s，
        # 和 24 小时逐条对账不是一回事，删了会丢东西。
        "queue": __import__("collector_ext").queue_state(),
        "logs": _logs_tail(),
        "rooms": {"eyes": True, "vitals": True, "ledger": True, "config": True, "mind": True, "voice": True, "try": True, "check": True, "notebook": True},
    }


_STATE_BYTES = b"{}"
_STATE_LOCK = threading.Lock()


def _state_bytes() -> bytes:
    with _STATE_LOCK:
        return _STATE_BYTES


def write_state() -> None:
    """落一份 state.json，并把同一份字节留在内存里给 /state 用。

    2026-10-27：以前 /state 是每次请求都 open(state.json) 现场读，
    而写端用 os.replace 换名 —— Windows 上"有人正读着"会让 replace 直接
    PermissionError: [WinError 5]（data/console/collector.err 里一堆），
    于是状态停在旧值（面板上"世界 同步 165s"、右栏停在上一句）。
    现在读端走内存，写端失败也不影响面板；文件只留给人工排查。
    """
    global _STATE_BYTES
    st = build_state()
    raw = json.dumps(st, ensure_ascii=False).encode("utf-8")
    with _STATE_LOCK:
        _STATE_BYTES = raw
    tmp = STATE_OUT + ".tmp"
    for attempt in range(3):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(raw.decode("utf-8"))
            os.replace(tmp, STATE_OUT)
            return
        except OSError:
            # 文件只是给人看的排查副本；面板走内存。写不进去就算了，别再往
            # collector.err 里每秒刷一条 PermissionError。
            if attempt == 2:
                return
            time.sleep(0.05)


def _health_payload() -> dict:
    """探活只承诺第 1+2 层：进程在、accept 循环在转。

    第 3 层（业务还能正确回答）不在这里答 —— 那是 `/state` 的事（看它的 age 字段）。
    所以这里只回 `ok` / `at`：
    - 不带 pid：本机谁能连谁就能读，这个数字没有用处，只增加信息面；
    - 不碰 executor、文件、DB：探活一旦有副作用，它就不叫探活了。
    """
    return {"ok": True, "at": _now()}


_MAX_BODY = 1024 * 1024 * 1024   # 2026-10-31 取消上限：1GB（本机回环，只防手滑）
_READ_TIMEOUT = 30           # 秒：慢慢喂字节的连接不配占住线程


def _read_body(req):
    """从入站请求里读出一个 JSON 对象。回 (body | None, (状态码, 响应体) | None)。

    四道闸，顺序就是防线：大小判在读 socket 之前，读之前先设读超时，
    读出来必须能解析成 dict。缺 Content-Length 按 411 拒（本仓不接分块传输）——
    以前这里是「等于 0」，于是没带长度的请求会被当成空对象静默吞掉。
    """
    raw_cl = req.headers.get("Content-Length")
    if raw_cl is None:
        return None, (411, {"ok": False, "error": "缺 Content-Length（不支持分块传输）"})
    try:
        n = int(raw_cl)
    except (TypeError, ValueError):
        return None, (413, {"ok": False, "error": "Content-Length 不是整数",
                            "limit": _MAX_BODY, "content_length": raw_cl})
    if n < 0 or n > _MAX_BODY:
        return None, (413, {"ok": False, "error": "请求体过大",
                            "limit": _MAX_BODY, "content_length": raw_cl})
    try:
        req.connection.settimeout(_READ_TIMEOUT)
        raw = req.rfile.read(n) if n else b"{}"
    except Exception as exc:  # noqa: BLE001
        return None, (400, {"ok": False,
                            "error": "读请求体失败：%s: %s" % (type(exc).__name__, exc)})
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001
        return None, (400, {"ok": False,
                            "error": "请求不是 JSON：%s: %s" % (type(exc).__name__, exc)})
    if not isinstance(body, dict):
        return None, (400, {"ok": False, "error": "请求体必须是 JSON 对象"})
    return body, None


def _make_server():
    """本地只读+一条写通道。它同时是"只允许一个采集器"的那把锁。"""
    import executor  # console/executor.py（同目录，脚本方式启动时在 sys.path 里）

    class Handler(BaseHTTPRequestHandler):
        server_version = "yinyue-console/1.0"

        def _cors(self):
            """只放行本机面板：file:// 打开时 Origin 是 null，http 打开是同源（浏览器不查 CORS）。

            2026-11-01：以前这里是 `*`，任何网页的 JS 都能跨域读她的私聊原文；
            而真正需要跨域的 /state 走 _send_bytes，反而一个 CORS 头都没发 ——
            open_window 的 file:// 兜底就是这样变成一个永远空壳的面板。
            """
            origin = self.headers.get("Origin") or ""
            if origin in ("null", "http://127.0.0.1:39872", "http://localhost:39872"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Private-Network", "true")

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def do_OPTIONS(self):
            self._send(200, {"ok": True})

        def _send_bytes(self, code, body, ctype, max_age: int = 0):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self._cors()
            self.send_header("Cache-Control",
                             ("max-age=%d" % max_age) if max_age else "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def do_GET(self):
            path = (self.path or "/").split("?")[0]
            try:
                if path in ("/app", "/app/"):
                    with open(PAGE, "rb") as f:
                        self._send_bytes(200, f.read(), "text/html; charset=utf-8")
                elif path.startswith("/looks/"):
                    # 她的参考图（律 → 外貌 · 我的样子）：只认 faces/gallery 下白名单扩展名
                    rel = path[len("/looks/"):]
                    parts = [p for p in rel.split("/") if p and p not in (".", "..")]
                    if len(parts) != 2 or os.path.splitext(parts[1])[1].lower() not in (
                            ".jpg", ".jpeg", ".png", ".webp", ".bmp"):
                        self._send(404, {"ok": False, "error": "bad path"})
                    else:
                        fp = os.path.join(ROOT, "data", "persona", "assets", "private",
                                          parts[0], "faces", "gallery", parts[1])
                        if os.path.exists(fp):
                            ctype = "image/png" if parts[1].lower().endswith(".png") else "image/jpeg"
                            with open(fp, "rb") as f:
                                self._send_bytes(200, f.read(), ctype)
                        else:
                            self._send(404, {"ok": False, "error": "no such file"})
                elif path.startswith("/thumb/"):
                    # 表情缩略图：一张一个请求，后端落盘缓存。不再把 887 张塞进一次 JSON。
                    import executor as _ex
                    png = _ex.sticker_thumb_bytes(path[len("/thumb/"):])
                    if png:
                        self._send_bytes(200, png, "image/png", max_age=604800)
                    else:
                        self._send(404, {"ok": False, "error": "没有这张图"})
                elif path == "/qr":
                    # NapCat 自己把登录二维码写到 cache/qrcode.png，这里直接吐给面板
                    qp = os.path.join(ROOT, "napcat", "napcat", "cache", "qrcode.png")
                    if os.path.exists(qp):
                        with open(qp, "rb") as f:
                            self._send_bytes(200, f.read(), "image/png")
                    else:
                        self._send(404, {"ok": False, "error": "还没有二维码：NapCat 没起或已登录"})
                elif path == "/icon.ico":
                    with open(ICON, "rb") as f:
                        self._send_bytes(200, f.read(), "image/x-icon")
                elif path in ("/health", "/"):
                    self._send(200, _health_payload())
                elif path == "/law":
                    self._send(200, executor.state())
                elif path == "/checkup":
                    from console import checkup as _cu
                    force = "fresh=1" in (self.path or "")   # 面板「重新体检」带 ?fresh=1
                    fresh = (not force) and (
                        time.time() - float(_CHECKUP_CACHE["at"] or 0)) < _CHECKUP_TTL_S
                    if not fresh or _CHECKUP_CACHE["data"] is None:
                        _CHECKUP_CACHE["data"] = _cu.run()
                        _CHECKUP_CACHE["at"] = time.time()
                    self._send(200, _CHECKUP_CACHE["data"])
                elif path == "/models":
                    # 试衣间可选的 provider/model（只读；含本机 ollama 探测）
                    from loop import rehearse as _rh
                    self._send(200, _rh.models())
                elif path == "/rehearsal":
                    q = (self.path or "").split("turn_id=", 1)
                    tid = q[1].split("&")[0] if len(q) > 1 else ""
                    from loop import rehearse as _rh
                    self._send(200, _rh.last(tid))
                elif path == "/voice":
                    # 试听：合成一段样音（只合成，不发送）。查询串必须自己切。
                    q = (self.path or "").split("slug=", 1)
                    slug = q[1].split("&")[0] if len(q) > 1 else ""
                    try:
                        from urllib.parse import unquote
                        slug = unquote(slug)
                    except Exception:  # noqa: BLE001
                        pass
                    import executor as _ex
                    got = _ex.voice_preview(slug)
                    if not got.get("ok"):
                        self._send(200, got)
                    else:
                        with open(str(got.get("abs") or ""), "rb") as fh:
                            self._send_bytes(200, fh.read(), "audio/wav")
                elif path == "/state":
                    # 走内存快照：不再和写端抢 state.json 的文件句柄
                    self._send_bytes(200, _state_bytes(), "application/json; charset=utf-8")
                else:
                    self._send(404, {"ok": False, "error": "没有这个路径"})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})

        def do_POST(self):
            path = (self.path or "/").split("?")[0]
            body, err = _read_body(self)
            if err is not None:
                self._send(err[0], err[1])
                return
            if path == "/rehearse":
                # 试衣间：只读重跑，不发送、不记账。跑得慢（每档一次真实调用），所以放在线程里。
                try:
                    from loop import rehearse as _rh
                    out = _rh.run(str(body.get("turn_id") or ""),
                                  addendum=str(body.get("addendum") or ""),
                                  temperatures=body.get("temperatures") or [0.1],
                                  repeats=int(body.get("repeats") or 1),
                                  by=str(body.get("by") or "panel"),
                                  models=body.get("models") or None)
                except Exception as exc:  # noqa: BLE001
                    import traceback as _tb
                    out = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                           "traceback": _tb.format_exc()[-3000:]}
                self._send(200, out)
                return
            if path != "/cmd":
                self._send(404, {"ok": False, "error": "没有这个路径"})
                return
            self._send(200, executor.apply(body))

        def log_message(self, *args):  # 别往 stderr 刷访问日志
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 39872), Handler)
    srv.daemon_threads = True
    return srv


def main() -> int:
    try:
        os.chdir(ROOT)   # 配置是相对路径，cwd 漂了会静默读错
    except Exception:  # noqa: BLE001
        pass
    os.makedirs(OUT_DIR, exist_ok=True)
    if not _single_instance():
        return 0   # 已经有一个采集器在跑（端口 bind 不可靠，见 _single_instance）
    try:
        srv = _make_server()
    except OSError:
        return 0  # 已经有一个采集器在跑
    # 2026-10-27：先把 state.json 落一次再开端口。以前是"先 serve 再写"，
    # 采集器刚起的那几秒 /state 会 500（FileNotFoundError），面板拿到
    # {"ok":false,...} 直接 render，第一处裸访问就抛
    # "Cannot read properties of undefined"，而且那句话写上去就不会自己消。
    try:
        write_state()
    except Exception:  # noqa: BLE001 - 写不出来也要先把端口开起来
        pass
    threading.Thread(target=srv.serve_forever, name="console-api", daemon=True).start()
    started = _now()
    miss = 0
    while True:
        try:
            write_state()
        except Exception:
            import traceback
            try:
                with open(os.path.join(OUT_DIR, "collector.err"), "a", encoding="utf-8") as fh:
                    fh.write("[%s]\n%s\n" % (datetime.now().strftime("%H:%M:%S"), traceback.format_exc()))
            except Exception:
                pass
        if _now() - started > WINDOW_GRACE_S:
            alive = _window_alive()
            if alive is False:
                miss += 1
            elif alive is True:
                miss = 0
            # None（枚举失败）既不算「关了」也不清零：原因已由 _note 记下
            if miss >= 3:
                _note("window_gone", "连续 %d 次确认面板窗口不在，采集器退出" % miss)
                return 0
        time.sleep(TICK_S)


if __name__ == "__main__":
    raise SystemExit(main())
