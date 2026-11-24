"""说话白名单：平台 → 账号 → 联系人/群。

2026-10-27 起因：`ALLOW_GROUP_SAY` 是个全局开关（默认全开），她对任何一个能 @ 到她的群
都能说话，包括里面有完全不知道她是 AI 的人。这里把"能不能在这个会话里开口"做成
可查看、可编辑、按账号分组的白名单。

只放在 `data/persona/speak_policy.json`（跟 chat_alias.json / me_names.json 同级），
因为 loop 每拍要读，走 JSON + mtime 缓存最省事；控制台写它之前会先备份。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from core.infra.envflags import flag, text
from core.infra.jsonio import atomic_write_json

PATH = Path(__file__).resolve().parent.parent / "data" / "persona" / "speak_policy.json"
_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {"mtime": 0.0, "data": None}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def load() -> dict:
    """读策略（按 mtime 缓存）。坏文件 → 空策略（等于"没限制"，绝不因为配置崩掉发送）。"""
    try:
        st = PATH.stat()
        _mt = st.st_mtime
    except OSError:
        return {"version": 1, "platforms": {}}
    with _LOCK:
        if _CACHE["data"] is not None and abs(_CACHE["mtime"] - _mt) < 1e-6:
            return _CACHE["data"]
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {"version": 1, "platforms": {}}
    except Exception:
        data = {"version": 1, "platforms": {}}
    with _LOCK:
        _CACHE["mtime"] = _mt
        _CACHE["data"] = data
    return data


def is_enabled() -> bool:
    """总开关：`YINYUE_SPEAK_POLICY=0` 可整体关掉（调试用）。"""
    return flag("YINYUE_SPEAK_POLICY", default=True)


def accounts_of(platform: str = "wechat") -> dict:
    data = load()
    plat = (data.get("platforms") or {}).get(platform) or {}
    acc = plat.get("accounts")
    return acc if isinstance(acc, dict) else {}


def find(platform: str, chat_id: str) -> tuple[str, dict]:
    """chat_id → (账号, 该会话条目)。找不到回 ("", {})。"""
    cid = str(chat_id or "").strip()
    if not cid:
        return "", {}
    for acc, box in accounts_of(platform).items():
        chats = (box or {}).get("chats") or {}
        if isinstance(chats, dict) and cid in chats:
            return acc, chats[cid] if isinstance(chats[cid], dict) else {}
    return "", {}


_OWNER: dict[str, Any] = {"at": 0.0, "map": {}}
_OWNER_TTL_S = 120.0
_OWNER_MISS: dict[tuple, float] = {}
_OWNER_MISS_TTL_S = 5.0


def _account_of(platform: str, chat_id: str) -> str:
    """这个 chat_id 属于哪个账号（从各账号库的 contacts / messages 里找）。找不到回空串。

    为什么需要它：白名单只记"哪个账号的哪些会话"，光看白名单是判不出"这个会话根本不在
    任何名单里"的。没有归属就无从判断，等于白名单形同虚设（2026-10-27 自测发现的洞）。
    """
    cid = str(chat_id or "").strip()
    if not cid:
        return ""
    now = time.time()
    with _LOCK:
        hit = (_OWNER["map"].get(platform) or {}).get(cid)
        if hit:
            return hit
        miss_at = _OWNER_MISS.get((platform, cid))
        if miss_at and (now - miss_at) < _OWNER_MISS_TTL_S:
            return ""   # 刚查过确实没有，短时间内不再扫库
    base = Path(__file__).resolve().parent.parent / "data" / "platforms" / platform
    found: dict[str, str] = {}
    try:
        accounts = [d for d in base.iterdir() if d.is_dir()] if base.is_dir() else []
    except OSError:
        accounts = []
    import sqlite3

    for acc in accounts:
        db = acc / "account.db"
        if not db.is_file():
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=2.0)
        except Exception:
            continue
        try:
            hit = False
            for sql in ("select 1 from contacts where wxid = ? limit 1",
                        "select 1 from messages where chat_id = ? limit 1"):
                try:
                    if con.execute(sql, (cid,)).fetchone():
                        hit = True
                        break
                except Exception:
                    continue
            if hit:
                found[cid] = acc.name
        finally:
            try:
                con.close()
            except Exception:
                pass
    with _LOCK:
        box = _OWNER["map"].setdefault(platform, {})
        box.update(found)
        _OWNER["at"] = now
        # 只缓存"找到了"的；找不到的不长期记忆（新群刚建、库还没同步时会误判成不存在）
        if cid in box:
            _OWNER_MISS.pop((platform, cid), None)
            return box[cid]
        _OWNER_MISS[(platform, cid)] = now
        return ""


def allowed(platform: str, chat_id: str) -> tuple[bool, str]:
    """这个会话能不能开口。返回 (能不能, 原因)。

    没配过（文件不存在 / 平台没登记）→ 放行，保持原行为；
    配过且账号是 allowlist 模式 → 只有名单里的会话放行。
    """
    if not is_enabled():
        return True, "policy_off"
    cid = str(chat_id or "").strip()
    if not cid:
        return True, "no_chat_id"
    acc, _entry = find(platform, cid)
    if not acc:
        # 名单里没有 → 看这个会话到底属于哪个账号；账号配过白名单就按"不在名单里"处理
        acc = _account_of(platform, cid)
        if not acc:
            conf = accounts_of(platform)
            if conf:
                return False, "unknown_chat"   # 平台配过白名单，但这会话不认识 → 不说
            return True, "not_configured"
        box_try = accounts_of(platform).get(acc)
        if box_try is None:
            return False, "account_not_in_policy"
        mode_try = str(box_try.get("mode") or "allowlist").lower()
        if mode_try == "all":
            return True, "account_all"
        if mode_try == "none":
            return False, "account_none"
        return False, "not_in_allowlist"
    box = accounts_of(platform).get(acc) or {}
    mode = str(box.get("mode") or "allowlist").lower()
    if mode == "all":
        return True, "account_all"
    if mode == "none":
        return False, "account_none"
    return True, "in_allowlist"


def summary() -> dict:
    data = load()
    out = {"version": int(data.get("version") or 1), "updated_at": str(data.get("updated_at") or ""),
           "enabled": is_enabled(), "path": str(PATH), "platforms": {}}
    for plat, box in (data.get("platforms") or {}).items():
        accs = (box or {}).get("accounts") or {}
        out["platforms"][plat] = {
            "accounts": {a: {"mode": str((b or {}).get("mode") or "allowlist"),
                             "n": len(((b or {}).get("chats") or {}))}
                         for a, b in accs.items()},
            "n": sum(len(((b or {}).get("chats") or {})) for b in accs.values()),
        }
    return out


def save(data: dict) -> None:
    with _LOCK:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = _now()
        atomic_write_json(PATH, data, indent=2)
        _CACHE["data"] = None


# ---------------- 主动说话（2026-10-27） ----------------
# 白名单管"准不准说"，这里管"能不能自己找话"：只有标了 proactive 的会话，
# 她才会在没人找她时主动开口；其余一律只回应。


def platform_of(chat_id: str) -> str:
    """从 chat_id 推平台（微信带 @chatroom / wxid_ / filehelper，QQ 是纯数字）。"""
    cid = str(chat_id or "").strip()
    if not cid:
        return "wechat"
    if cid == "filehelper" or cid.startswith("wxid_") or "@" in cid:
        return "wechat"
    if cid.isdigit():
        return "qq"
    return "wechat"


def _iter_chats(data=None):
    d = data if isinstance(data, dict) else load()
    for plat, box in (d.get("platforms") or {}).items():
        for aid, node in ((box or {}).get("accounts") or {}).items():
            for cid, ent in ((node or {}).get("chats") or {}).items():
                yield str(plat), str(aid), str(cid), (ent if isinstance(ent, dict) else {})


def proactive_targets():
    """可以主动开口的会话清单（引擎与面板共用）。"""
    if not is_enabled():
        return []
    out = []
    for plat, aid, cid, ent in _iter_chats():
        if not ent.get("proactive"):
            continue
        out.append({"platform": plat, "account": aid, "chat_id": cid,
                    "name": str(ent.get("name") or "")})
    return out


def proactive_ok(platform: str, chat_id: str) -> bool:
    """这个会话允许她主动开口吗（默认不允许；白名单整体关掉时一律允许）。"""
    if not is_enabled():
        return True
    cid = str(chat_id or "").strip()
    for plat, _aid, c2, ent in _iter_chats():
        if plat == platform and c2 == cid:
            return bool(ent.get("proactive"))
    return False

