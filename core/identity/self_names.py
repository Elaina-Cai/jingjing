"""她在平台上的名字（"这段话是不是在叫我"的唯一来源）。

人设卡名（``data/persona/active.json``）**不参与**这里：那是她怎么说话，
这里是别人怎么叫她。名单按优先级合并去重：

1. 用户手填：``data/persona/me_names.json`` → ``{"names": ["鲸鲸", "月月"]}``

**只有这一层，没有兜底、也不自动从 contacts 补**（2026-10-27 用户要求：
"全部统一没有兜底，全部由用户手写输入"）。手写文件不存在/写空 → 名单就是空，
"是不是在叫我"全部认不出来（群里不亮 ★），这是有意为之：宁可安静，也不要
程序自己猜一个名字。``_contact_names`` 留着但暂不参与，想恢复只需在
``self_names`` 里把它加回去一行。

60 秒缓存；任何异常都返回空元组，**绝不抛**（寻址不许把入站打断）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

__all__ = ["self_names", "invalidate", "FALLBACK"]

FALLBACK: tuple[str, ...] = ()   # 2026-10-27：不再有兜底名字
_TTL_S = 60.0
_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_ROOT = Path(__file__).resolve().parents[2]
_MANUAL = _ROOT / "data" / "persona" / "me_names.json"


def _clean(names: Any) -> list[str]:
    out: list[str] = []
    for raw in names or ():
        name = str(raw or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def _manual_names() -> list[str]:
    """
    读手填名单。文件不存在/坏了都当没有，不抛。
    """
    try:
        if not _MANUAL.exists():
            return []
        data = json.loads(_MANUAL.read_text(encoding="utf-8") or "{}")
        if isinstance(data, dict):
            return _clean(data.get("names"))
        if isinstance(data, list):
            return _clean(data)
    except Exception:  # noqa: BLE001 - 手填文件坏了不配打断寻址
        return []
    return []


def _contact_names(db_path: str | Path | None, account_id: str) -> list[str]:
    """
    从 ``contacts`` 里自己那一行取昵称/备注（微信里就是「鲸鲸」这种）。
    """
    path = str(db_path or "").strip()
    if not path or not os.path.exists(path):
        return []
    acct = str(account_id or "").strip() or Path(path).parent.name
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=1.0)
        try:
            cur = con.cursor()
            cur.execute(
                "select nickname, remark from contacts where wxid = ? limit 1", (acct,)
            )
            row = cur.fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - 库在读/不存在都退回上一层
        return []
    if not row:
        return []
    return _clean([row[0], row[1]])


def self_names(db_path: str | Path | None = None, *, account_id: str = "") -> tuple[str, ...]:
    """她在平台上的名字。只认手写名单；没写就是空元组（不猜、不兜底）。"""
    key = "%s|%s" % (str(db_path or ""), str(account_id or ""))
    now = time.time()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < _TTL_S:
        return hit[1]

    # 2026-10-27 用户要求：名字全部由用户手写，不再从 contacts 自动补、不再兜底。
    out = tuple(_clean(_manual_names()))
    _CACHE[key] = (now, out)
    return out


def invalidate() -> None:
    """改了 ``me_names.json`` 或换了账号后，立刻让缓存失效。"""
    _CACHE.clear()
