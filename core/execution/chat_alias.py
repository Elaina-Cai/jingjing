"""会话别名簿：名字会变，chat_id 不会。

为什么需要它
------------
微信里一个会话的"名字"是显示用的，随时会变（群改名、好友改昵称、备注被改）。
而发送链里有两处依赖名字：

1. **找人**：搜索框只认名字，改名后拿旧名字搜不到；
2. **认人**：``verify_active_chat_title()`` 读到顶栏和期望名字不符就判定"不许发"。

2026-10-27 的设计约定：
    **名字只负责找人，认人靠别名集合（以及将来的头像指纹）。**

所以这里给每个 ``chat_id`` 挂一串"它曾经叫过的名字"：
- 数据来自 ``contacts`` 表（每次同步刷新）＋ 每次发送成功后从顶栏 OCR 到的真名；
- 任何一处只要给出"这个名字"，就能反查出它的全部别名，逐个去搜索，任一成功即算找到；
- 顶栏校验也从"等于这一个名字"放宽成"命中任一别名"，并把这次读到的名字记下来（自愈）。

存储
----
``data/persona/chat_alias.json``：
    {"version": 1,
     "chats": {"10000000002@chatroom": {"aliases": [{"n": "示例宿舍群", "src": "db", "at": "…"}]}}}

单进程内用 RLock 保护，落盘走 临时文件 + os.replace（原子替换）。任何异常都
只降级为"没有别名"，绝不让发送链因为别名簿出问题而失败。
"""

from __future__ import annotations

import json

from core.infra import skips
import os
import threading
import time
from typing import Iterable
from core.infra.jsonio import atomic_write_json

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(_ROOT, "data", "persona", "chat_alias.json")

_MAX_ALIASES = 8          # 每个会话最多记 8 个名字，够用且不会被垃圾撑大
_MAX_CHATS = 3000         # 上限，防止异常数据把文件写爆
_MAX_LEN = 64             # 单个名字最长 64 字，超了直接丢

_LOCK = threading.RLock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _norm(text: str) -> str:
    """名字归一：去空白（含全角空格）、去零宽字符。大小写保留（中文无关，英文群名要留）。"""
    t = str(text or "")
    for ch in ("\u3000", "\u200b", "\ufeff", "\n", "\r", "\t"):
        t = t.replace(ch, " ")
    return " ".join(t.split())


def looks_like_id(text: str) -> bool:
    """能不能当"搜索词"用。chat_id 不能。"""
    t = _norm(text)
    if not t:
        return True
    if t.endswith("@chatroom") or t.startswith("wxid_") or t.startswith("gh_"):
        return True
    if t in ("filehelper", "weixin", "fmessage", "medianote"):
        return True
    return t.isdigit()


def _load() -> dict:
    try:
        with io_open(PATH) as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("chats"), dict):
            return data
    except Exception as exc:
        # 别名表读坏了：她认人靠这张表，静默变空会让「群名匹配」悄悄失效
        skips.skip("chat_alias._load", "malformed", "%s: %s" % (type(exc).__name__, exc))
    return {"version": 1, "chats": {}}


def io_open(path: str):
    return open(path, "r", encoding="utf-8")


def _save(data: dict) -> bool:
    try:
        return atomic_write_json(PATH, data, indent=1, sort_keys=True)
    except Exception:
        return False


def learn(key: str, name: str, source: str = "title") -> bool:
    """把 ``name`` 记到 ``key`` 名下。返回是否真的写入了新名字。"""
    k = _norm(key)
    n = _norm(name)
    if not k or not n or len(n) > _MAX_LEN:
        return False
    if k == n:                      # 名字本身就是 key（无 chat_id 时），不自己记自己
        return False
    with _LOCK:
        data = _load()
        chats = data["chats"]
        entry = chats.get(k)
        if not isinstance(entry, dict):
            entry = {"aliases": []}
        aliases = entry.get("aliases")
        if not isinstance(aliases, list):
            aliases = []
        for item in aliases:
            if isinstance(item, dict) and _norm(item.get("n", "")) == n:
                item["at"] = _now()
                entry["aliases"] = aliases
                chats[k] = entry
                return _save(data)
        aliases.insert(0, {"n": n, "src": str(source or "title")[:12], "at": _now()})
        entry["aliases"] = aliases[:_MAX_ALIASES]
        chats[k] = entry
        while len(chats) > _MAX_CHATS:          # 丢最早的会话，别让文件无限长
            oldest = min(chats.items(), key=lambda kv: str(
                (kv[1].get("aliases") or [{}])[0].get("at", "")))[0]
            chats.pop(oldest, None)
        return _save(data)


def learn_many(items, source: str = "db") -> int:
    """批量记名字，只落盘一次。``items`` 是 (key, name) 的迭代器。

    ``contacts`` 一次刷新出三百多个名字，逐个 ``learn()`` 就是三百次读写；
    这里合并成一次，写进去多少条返回多少条（去重后）。
    """
    pairs = []
    for key, name in items or []:
        k = _norm(key)
        n = _norm(name)
        if not k or not n or k == n or len(n) > _MAX_LEN:
            continue
        pairs.append((k, n))
    if not pairs:
        return 0
    added = 0
    with _LOCK:
        data = _load()
        chats = data.setdefault("chats", {})
        for k, n in pairs:
            entry = chats.get(k)
            if not isinstance(entry, dict):
                entry = {"aliases": []}
            aliases = entry.get("aliases")
            if not isinstance(aliases, list):
                aliases = []
            known = {_norm(it.get("n", "")) for it in aliases if isinstance(it, dict)}
            if n in known:
                continue
            aliases.insert(0, {"n": n, "src": str(source or "db")[:12], "at": _now()})
            entry["aliases"] = aliases[:_MAX_ALIASES]
            chats[k] = entry
            added += 1
        while len(chats) > _MAX_CHATS:
            oldest = min(chats.items(), key=lambda kv: str(
                (kv[1].get("aliases") or [{}])[0].get("at", "")))[0]
            chats.pop(oldest, None)
        if added:
            _save(data)
    return added


def aliases_for(key: str) -> list[str]:
    """``key`` 名下所有别名（最新在前）。key 本身不在返回值里。"""
    k = _norm(key)
    if not k:
        return []
    with _LOCK:
        data = _load()
    entry = (data.get("chats") or {}).get(k) or {}
    out = []
    for item in entry.get("aliases") or []:
        if isinstance(item, dict):
            n = _norm(item.get("n", ""))
            if n and n not in out:
                out.append(n)
    return out


def key_of(name: str) -> str:
    """反查：这个"名字"属于哪个 chat_id（找不到返回 ""）。"""
    n = _norm(name)
    if not n:
        return ""
    with _LOCK:
        data = _load()
    chats = data.get("chats") or {}
    if n in chats:
        return n
    for k, entry in chats.items():
        for item in (entry or {}).get("aliases") or []:
            if isinstance(item, dict) and _norm(item.get("n", "")) == n:
                return k
    return ""


def candidates_for(*names: str) -> list[str]:
    """把一堆"可能是名字也可能是 id"的输入，展开成有序候选搜索词。

    顺序：先给的原名 → 它的别名（新→旧）→ 下一个输入的同理。去重、
    过滤掉 chat_id（搜索框不认 id）。
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        v = _norm(value)
        if not v or v in seen or looks_like_id(v):
            return
        seen.add(v)
        out.append(v)

    for raw in names:
        n = _norm(raw)
        if not n:
            continue
        add(n)
        for alias in aliases_for(n):
            add(alias)
        k = key_of(n)
        if k:
            for alias in aliases_for(k):
                add(alias)
    if not out:
        for raw in names:               # 全是 id 时，至少把 id 原样回给调用方
            v = _norm(raw)
            if v and v not in seen:
                seen.add(v)
                out.append(v)
    return out


def remember_db_name(chat_id: str, label: str) -> bool:
    """``contacts`` 表刷新出来的名字，顺手记一笔（供改名后反查）。"""
    if looks_like_id(chat_id) and not looks_like_id(label):
        return learn(chat_id, label, source="db")
    return False


def stats() -> dict:
    with _LOCK:
        data = _load()
    chats = data.get("chats") or {}
    return {"chats": len(chats),
            "aliases": sum(len((v or {}).get("aliases") or []) for v in chats.values()),
            "path": PATH}
