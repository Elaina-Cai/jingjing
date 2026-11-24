"""看：把图片里的字读出来。

她以前只看到 `[图片]`，看不见图里的字——群里最后一句常是图，等于什么都读不到。
这里用和发送校验同一套 OCR（RapidOCR onnxruntime），但只做"读"，不碰窗口。

三条纪律：
1. 一张图只 OCR 一次（`data/loop/image_text.json` 缓存），读不到也缓存成空串。
2. 每拍最多新读 3 张（`MAX_NEW_PER_BUILD`）。宁可慢一点，不许把一拍拖成十几秒。
3. 永不抛：OCR 挂了就当她没看见，返回空串。
"""
from __future__ import annotations

import json
import os

from core.infra.jsonio import atomic_write_json
import threading
import time

from . import ports
from core.infra.envflags import text

CACHE = os.path.join(ports.ROOT, "data", "loop", "image_text.json")
DESC_CACHE = os.path.join(ports.ROOT, "data", "loop", "image_desc.json")
DESC_CHARS = 60
# 2026-10-27：描述缓存加两件事 —— ①同时按 md5 存一份（文件名会变、md5 不会）；
# ②"看不清/问不到"也记一笔，TTL 内不再重复花钱（负面缓存）。
NEG_TTL_S = float(text("YINYUE_VISION_NEG_TTL_H", "24") or 24) * 3600.0
_MD5_MEMO: dict[str, list] = {}   # path -> [size, mtime_ns, md5]
_DCACHE: dict[str, dict] | None = None
MAX_NEW_PER_BUILD = 3
MAX_FILE_MB = 8.0
_CHARS = 24
_LOCK = threading.Lock()
_CACHE: dict[str, dict] | None = None
_ENGINE = None
_NEW = 0          # 本次进程内已经新读了几张（每次 build 由 reset_budget 归零）
_COUNT = {"n": 0}


def _load() -> dict[str, dict]:
    global _CACHE
    if _CACHE is None:
        try:
            with open(CACHE, encoding="utf-8") as fh:
                _CACHE = json.load(fh)
            if not isinstance(_CACHE, dict):
                _CACHE = {}
        except Exception:  # noqa: BLE001
            _CACHE = {}
    return _CACHE


def _save() -> None:
    # 落盘只走 jsonio（唯一临时名 + rename + 退避重试），本模块不自己拼 .tmp。
    try:
        atomic_write_json(CACHE, _load())
    except Exception:  # noqa: BLE001
        pass


def reset_budget() -> None:
    """每拍开始前调一次：允许再新读几张。"""
    _COUNT["n"] = 0


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def _resolve(path: str) -> str:
    p = str(path or "").strip().replace("\\\\", "\\")
    if not p:
        return ""
    if not os.path.isabs(p):
        p = os.path.join(ports.ROOT, p)
    return p


def _flat(s: str) -> str:
    return " ".join(str(s or "").split())


def _file_md5(path: str) -> str:
    """文件的 md5（按 size+mtime 记忆，同一张图不会算两遍）。算不出来返回空串。"""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    memo = _MD5_MEMO.get(path)
    if memo and memo[0] == st.st_size and memo[1] == st.st_mtime_ns:
        return str(memo[2])
    try:
        import hashlib

        h = hashlib.md5()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(262144)
                if not chunk:
                    break
                h.update(chunk)
        digest = h.hexdigest()
    except Exception:  # noqa: BLE001
        return ""
    _MD5_MEMO[path] = [st.st_size, st.st_mtime_ns, digest]
    return digest


def _neg_fresh(hit: dict) -> bool:
    """这条记录是不是"最近问过、没问出来"，还在冷却里。"""
    if not isinstance(hit, dict) or not hit.get("neg"):
        return False
    try:
        asked = float(hit.get("ts") or 0.0)
    except (TypeError, ValueError):
        return False
    return (time.time() - asked) < NEG_TTL_S


def image_text(path: str, *, max_chars: int = _CHARS) -> str:
    """图片里读得到的字（最多 max_chars 个字）；读不到/读失败都返回空串。"""
    fp = _resolve(path)
    if not fp or not os.path.isfile(fp):
        return ""
    key = os.path.basename(fp)
    try:
        sz = os.path.getsize(fp)
    except Exception:  # noqa: BLE001
        return ""
    if sz / (1024 * 1024) > MAX_FILE_MB:
        return ""
    with _LOCK:
        cache = _load()
        hit = cache.get(key)
        if isinstance(hit, dict) and hit.get("sz") == sz:
            return str(hit.get("t") or "")[:max_chars]
        if _COUNT["n"] >= MAX_NEW_PER_BUILD:
            return ""
        _COUNT["n"] += 1
        try:
            res = _engine()(fp)
        except Exception:  # noqa: BLE001
            return ""
        items = res[0] if isinstance(res, tuple) else res
        text = ""
        if items:
            text = _flat(" ".join(str(it[1]) for it in items if len(it) > 1))
        cache[key] = {"t": text, "sz": sz, "at": time.strftime("%Y-%m-%d %H:%M")}
        _save()
        return text[:max_chars]


# --------------------------------------------------------------- 看见（不是读字）

def _load_desc() -> dict[str, dict]:
    global _DCACHE
    if _DCACHE is None:
        try:
            with open(DESC_CACHE, encoding="utf-8") as fh:
                _DCACHE = json.load(fh)
            if not isinstance(_DCACHE, dict):
                _DCACHE = {}
        except Exception:  # noqa: BLE001
            _DCACHE = {}
    return _DCACHE


def _save_desc() -> None:
    # 落盘只走 jsonio（唯一临时名 + rename + 退避重试）。_LOCK 只护住本进程内的缓存字典，
    # 跨进程那一层交给「唯一临时名」，两边不重叠。
    try:
        with _LOCK:
            atomic_write_json(DESC_CACHE, _load_desc())
    except Exception:  # noqa: BLE001
        pass


def describe(path: str, *, max_chars: int = DESC_CHARS, mode: str = "wait") -> str:
    """这张图她**看见了什么**（视觉描述，不是 OCR）。

    2026-10-27：loop 以前只有 OCR —— 一眼就懂的图（表情包、照片、聊天截图）对她等于
    空白，于是她说"图我这边只是个空框"。这里复用入站那条视觉管线
    （``core.conversation.media.ensure_image_description``），结果落
    ``data/loop/image_desc.json``，按文件名**和** md5 前缀两个键都存一份。

    每拍最多新问一张（预算在 ``core.conversation.media_ref`` 里，``reset_budget`` 归零），
    ``mode="prefetch"`` 只让后台去问、本拍不等。永不抛：问不到就当没看见。
    """
    key = os.path.basename(str(path or "").replace("\\", "/")).strip().lower()
    if not key:
        return ""
    stem = os.path.splitext(key)[0]
    _md5 = _file_md5(_resolve(path)) if path else ""
    keys = [k for k in (stem, _md5, key) if k]
    cache = _load_desc()
    for k in keys:
        hit = cache.get(k) or {}
        if str(hit.get("d") or ""):
            return str(hit["d"])[:max_chars]
    for k in keys:
        if _neg_fresh(cache.get(k) or {}):
            return ""          # 最近问过、确实没问出来，冷却期内不再花这一次钱
    try:
        from core.conversation.media_ref import describe_md5

        desc = describe_md5(stem, path=path, mode=mode)
    except Exception:  # noqa: BLE001 - 视觉链路坏了就当没看见
        return ""
    at = time.strftime("%Y-%m-%d %H:%M")
    if not desc:
        # 只在"真去问了"的模式下记负面：prefetch 是异步的，此时空不代表问不到，
        # 记下来会让她 24 小时都看不见这张图。
        if mode != "prefetch":
            for k in keys:
                old = cache.get(k) or {}
                cache[k] = {"d": "", "neg": 1, "tries": int(old.get("tries") or 0) + 1,
                            "ts": time.time(), "at": at}
            _save_desc()
        return ""
    for k in keys:
        cache[k] = {"d": desc, "at": at, "md5": _md5}
    _save_desc()
    return desc[:max_chars]
