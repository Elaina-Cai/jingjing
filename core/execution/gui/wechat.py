"""微信：读 DB、写 UI——这一片只到「填进输入框，不按发送」。

两条通道，别混：

* **读** —— 解密后的本地库（`data/decrypted/wechat/<account>/merge_msg.db`）。
  全量、可回溯、不需要窗口在前台；解密与合并由 `db/decrypt.py` 负责，
  这里只读它吐出来的明文 SQLite。
* **写** —— 真实 Windows 输入队列（`core/execution/wx_human.py` 的 ``SendInput``）。
  要窗口在前台，而且只能靠它：微信 4.x 不是可调试的 Chromium，CDP 够不着。

这一片是**半程**：切到会话 -> 聚焦输入框 -> 把字填进去 -> **不按发送**。
发送是另一片的事，必须过三级授权与出口（`core/execution/wx_executor.py`
里 ``SEND_TEXT 必须经由 OutboundGateway 发送`` 的纪律不改，这里一个字节都发不出去）。

为什么回执不靠像素差：微信的库里有 ``local_id`` 与 ``msg_svr_id``（服务器消息号），
真发出去之后回读一行就能对账——比截图猜强得多。这一片先不做回读，只把
「填进去了没有」（``message_box_has_text``）作为半程证据。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence

from core.execution.gui.base import (
    SNAPSHOT_MAX_CHARS,
    GuiError,
    GuiSnapshot,
    count_changed_pixels,
    normalize_text,
    window_title_matches,
)
from core import features
from core.execution.gui import capability as capability_probe
from core.execution.gui import lease as device_lease_mod
from core.execution.gui import metrics as device_metrics
from safelog import warn

log = logging.getLogger(__name__)

#: 微信窗口标题里一定有的字（简繁/中英各一）。
WECHAT_TITLE_HINTS: tuple[str, ...] = ("微信", "WeChat", "Weixin")

#: 一屏最多回读多少条（和世界窗口的 40 条无关：这是「当前会话」的取样）。
RECENT_LIMIT = 20

#: 填进输入框的字数上限（再多就不是「一句话」了）。
FILL_MAX_CHARS = 2000

#: 单条正文渲染上限（世界窗口的规则是正文不折叠，这里取样件只留一斤）。
LINE_MAX_CHARS = 300

#: 4.x 的库：**每个会话一张表**，表名 = ``Msg_<md5(会话 id)>``；没有 ``str_talker`` 列。
#: 3.x 的库：一张大表 + ``str_talker`` 列。两种都认（见 ``read_recent``）。
_TABLE_PREFIXES = ("Msg_", "MSG_", "msg_")
_LEGACY_TABLE_LIKE = ("Msg%", "MSG%", "ChatMsg%", "message%")
_LEGACY_TALKER_COLUMNS = ("str_talker", "StrTalker", "talker")
_LEGACY_CONTENT_COLUMNS = ("message_content", "str_content", "StrContent", "content")
_LEGACY_SENDER_COLUMNS = ("is_sender", "IsSender", "sender")
_TIME_COLUMNS = ("create_time", "CreateTime", "createtime")

#: 不能当正文念的类型（微信 4.x 的 ``local_type``）——渲染成方括号占位，别把二进制塞进 prompt。
_TYPE_PLACEHOLDER = {
    3: "[图片]",
    34: "[语音]",
    43: "[视频]",
    47: "[表情]",
    48: "[位置]",
    49: "[链接/文件]",
    50: "[通话]",
    62: "[小视频]",
    10000: "[系统]",
    10002: "[撤回]",
}


def _mode() -> str:
    """唯一读点：``WECHAT_UI``（off / shadow / on）。"""
    return features.mode(features.WECHAT_UI, features.MODE_OFF)


def _pick(columns: Iterable[str], wanted: Sequence[str]) -> str:
    lower = {str(c).lower(): str(c) for c in columns}
    for name in wanted:
        if name.lower() in lower:
            return lower[name.lower()]
    return ""


def _open_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=3.0)
    except sqlite3.Error as exc:
        raise GuiError("not_connected", f"打不开解密后的库 {db_path}: {exc}") from exc


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _chat_table(conn: sqlite3.Connection, chat: str) -> str:
    """4.x：表名是会话 id 的 md5。返回空串表示「这张库不是 4.x 的形状」。"""
    digest = hashlib.md5(chat.encode("utf-8")).hexdigest()
    for prefix in _TABLE_PREFIXES:
        if _has_table(conn, f"{prefix}{digest}"):
            return f"{prefix}{digest}"
    return ""


def _resolve_chat(conn: sqlite3.Connection, chat: str) -> str:
    """会话名 → 会话 id。

    先当 id 认（``filehelper`` / ``xx@chatroom`` 这种），认不出再当**显示名**去
    ``contact.db`` 反查——只认唯一命中，0 个或多个都返回空串（宁缺勿猜：猜错就是
    把 A 群的话当 B 群发出去）。
    """
    if not chat:
        return ""
    if _chat_table(conn, chat):
        return chat
    db_dir = _db_dir(conn)
    contacts = _ContactBook((db_dir / "contact.db") if db_dir else None)
    hits = [name for name in set(_name_map(conn).values()) if contacts.display(name) == chat]
    return hits[0] if len(hits) == 1 else ""


def _self_rowid(conn: sqlite3.Connection, own: str) -> int:
    """``Name2Id.rowid`` 里自己那一行——``real_sender_id`` 等于它就是「我发的」。"""
    if not own or not _has_table(conn, "Name2Id"):
        return 0
    row = conn.execute(
        "SELECT rowid FROM Name2Id WHERE user_name = ? LIMIT 1", (own,)
    ).fetchone()
    return int(row[0]) if row else 0


def _chat_ids(conn: sqlite3.Connection) -> list[str]:
    """所有有消息表的会话 id（4.x）；3.x 返回空列表。"""
    if not _has_table(conn, "Name2Id"):
        return []
    names = [str(r[0]) for r in conn.execute("SELECT user_name FROM Name2Id")]
    out: list[str] = []
    for name in names:
        if name and _chat_table(conn, name):
            out.append(name)
    return out


_XML_HINTS = ("<msg", "<appmsg", "<sysmsg", "<msgsource", "<revokemsg", "<?xml")


def _looks_like_xml(text: str) -> bool:
    """卡片/链接消息正文是一段 XML（还可能是 zstd 压过的）；不许念进 prompt。"""
    head = (text or "").lstrip("\ufeff\u200b \t\r\n")[:400]
    return head.startswith("<") and any(hint in head for hint in _XML_HINTS)


def _looks_like_id(name: str) -> bool:
    return name.startswith("wxid_") or "@" in name or (len(name) > 24 and " " not in name)


def _placeholder(local_type: int) -> str:
    return _TYPE_PLACEHOLDER.get(int(local_type or 0), "")


_ID_PREFIX_RE = re.compile(
    r"^(wxid_[A-Za-z0-9_\-]{4,}|[A-Za-z0-9_\-]{4,}@(?:chatroom|openim|im\.wechat))\s*:\s*"
)


def recent_rows(
    chat: str,
    *,
    db_path: str | Path | None = None,
    account_id: str = "",
    limit: int = 600,
) -> list[dict[str, Any]]:
    """某个会话最近 ``limit`` 条的**有序链路**：``[{local_id, create_time, text, mine}]``（升序）。

    用途只有一个是干净的：**给撤回做锚点定位**（屏幕上的行 → 库里第几条 → 目标在我上方第几条），
    所以这里不渲染、不带显示名、不进 prompt。读不到库就返回空表，调用方退回「盲翻」。
    """
    path = Path(db_path) if db_path is not None else _decrypted_db()
    if not path.exists():
        return []
    own = str(account_id or path.parent.name or "")
    conn = _open_ro(path)
    try:
        self_rowid = _self_rowid(conn, own)
        table = _chat_table(conn, _resolve_chat(conn, chat))
        if not table:
            return []
        cols = _columns(conn, table)
        content_col = (
            "message_content" if "message_content" in cols else _pick(cols, _LEGACY_CONTENT_COLUMNS)
        )
        id_col = "local_id" if "local_id" in cols else _pick(cols, ("msgId", "MsgId", "localId"))
        time_col = "create_time" if "create_time" in cols else _pick(cols, _TIME_COLUMNS)
        sender_col = "real_sender_id" if "real_sender_id" in cols else "0"
        if not (content_col and id_col and time_col):
            return []
        rows = conn.execute(
            f'SELECT "{id_col}", "{content_col}", "{time_col}", {sender_col} FROM "{table}" '
            f'ORDER BY "{id_col}" DESC LIMIT ?',
            (max(1, int(limit)),),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for local_id, content, ts, sender in reversed(rows):
            body = _as_text(content)
            _guessed, body = _split_sender_prefix(body)
            if _looks_like_xml(body):
                body = ""  # 系统/卡片消息对锚点没用，留空即跳过
            out.append(
                {
                    "local_id": int(local_id or 0),
                    "create_time": int(ts or 0),
                    "text": body,
                    "mine": int(sender or 0) == int(self_rowid or -1),
                }
            )
        return out
    except Exception:  # noqa: BLE001 - 读不了就当没有链路，调用方退回盲翻
        warn("core/execution/gui/wechat.py:239 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
    finally:
        conn.close()


def message_epoch(
    chat: str,
    text: str,
    *,
    db_path: str | Path | None = None,
    account_id: str = "",
    limit: int = 500,
) -> int | None:
    """库里那条「就是这句」的消息的 ``create_time``（秒）；找不到或不唯一就 None。

    用途只有一个：撤回时判断该往屏幕上方还是下方翻（见 ``wechat_revoke._direction_for``）。
    不返回正文、不进渲染、不进判的输入——库只回答「这话有多旧」。
    """
    want = " ".join(str(text or "").split())
    if not want:
        return None
    path = Path(db_path) if db_path is not None else _decrypted_db()
    if not path.exists():
        return None
    conn = _open_ro(path)
    try:
        table = _chat_table(conn, _resolve_chat(conn, chat))
        if not table:
            return None
        cols = _columns(conn, table)
        content_col = (
            "message_content" if "message_content" in cols else _pick(cols, _LEGACY_CONTENT_COLUMNS)
        )
        time_col = "create_time" if "create_time" in cols else _pick(cols, _TIME_COLUMNS)
        if not (content_col and time_col):
            return None
        rows = conn.execute(
            f'SELECT "{content_col}", "{time_col}" FROM "{table}" '
            f'ORDER BY "{time_col}" DESC LIMIT ?',
            (max(1, int(limit)),),
        ).fetchall()
        hits: list[int] = []
        for content, ts in rows:
            body = _as_text(content)
            _guessed, body = _split_sender_prefix(body)
            if " ".join(body.split()) == want:
                hits.append(int(ts or 0))
        if len(hits) != 1 or not hits[0]:
            return None
        return hits[0]
    except Exception:  # noqa: BLE001 - 库读不了不影响撤回，调用方按默认方向翻
        warn("core/execution/gui/wechat.py:290 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    finally:
        conn.close()


def _as_text(value: Any) -> str:
    """正文可能是 str，也可能是 zstd 压过的 bytes（``WCDB_CT_message_content`` 标记）。"""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        try:
            import zstandard

            raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=1 << 20)
        except Exception:
            warn("core/execution/gui/wechat.py:304 Exception", err=repr(__import__("sys").exc_info()[1]))
            return ""
        return raw.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
    return str(value or "").strip()


def _split_sender_prefix(text: str) -> tuple[str, str]:
    """群消息正文常以 ``wxid_xxx: `` 开头——把发送者与正文分开。"""
    match = _ID_PREFIX_RE.match(text or "")
    if not match:
        return "", text
    return match.group(1), text[match.end():].strip()


def _name_map(conn: sqlite3.Connection) -> dict[int, str]:
    if not _has_table(conn, "Name2Id"):
        return {}
    try:
        return {int(r[0]): str(r[1]) for r in conn.execute("SELECT rowid, user_name FROM Name2Id")}
    except sqlite3.Error:
        warn("core/execution/gui/wechat.py:323 sqlite3.Error", err=repr(__import__("sys").exc_info()[1]))
        return {}


def _db_dir(conn: sqlite3.Connection) -> Path | None:
    """库文件所在目录——``contact.db`` 是它的兄弟。"""
    try:
        for row in conn.execute("PRAGMA database_list"):
            if str(row[1]) == "main" and row[2]:
                return Path(str(row[2])).parent
    except sqlite3.Error:
        warn("core/execution/gui/wechat.py:333 sqlite3.Error", err=repr(__import__("sys").exc_info()[1]))
        return None
    return None


class _ContactBook:
    """昵称/备注来自 ``contact.db``（与消息库同级）；读不到就退回 wxid。"""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._cache: dict[str, str] = {}

    def display(self, username: str) -> str:
        if not username:
            return ""
        if username in self._cache:
            return self._cache[username]
        name = ""
        if self.path is not None and self.path.exists():
            try:
                conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=3.0)
                try:
                    row = conn.execute(
                        "SELECT remark, nick_name FROM contact WHERE username = ? LIMIT 1",
                        (username,),
                    ).fetchone()
                finally:
                    conn.close()
                if row:
                    name = str(row[0] or "").strip() or str(row[1] or "").strip()
            except (sqlite3.Error, OSError):
                warn("core/execution/gui/wechat.py:363 (sqlite3.Error, OSError)", err=repr(__import__("sys").exc_info()[1]))
                name = ""
        self._cache[username] = name or username
        return self._cache[username]


def _rows_v4(conn: sqlite3.Connection, table: str, chat: str, *, limit: int,
             self_rowid: int) -> list[dict[str, Any]]:
    cols = _columns(conn, table)
    content_col = "message_content" if "message_content" in cols else _pick(cols, _LEGACY_CONTENT_COLUMNS)
    if not content_col:
        return []
    sender_col = "real_sender_id" if "real_sender_id" in cols else "0"
    type_col = "local_type" if "local_type" in cols else "0"
    time_col = "create_time" if "create_time" in cols else _pick(cols, _TIME_COLUMNS)
    if not time_col:
        return []
    sql = (
        f'SELECT "{content_col}", {sender_col}, {type_col}, "{time_col}" '
        f'FROM "{table}" ORDER BY "{time_col}" DESC LIMIT ?'
    )
    names = _name_map(conn)
    db_dir = _db_dir(conn)
    contacts = _ContactBook((db_dir / "contact.db") if db_dir else None)
    rows = conn.execute(sql, (max(1, int(limit)),)).fetchall()
    out: list[dict[str, Any]] = []
    for content, sender, local_type, ts in rows:
        kind = int(local_type or 0)
        fallback = _placeholder(kind)
        text = "" if (fallback and kind != 10000) else _as_text(content)
        # 先剥 ``wxid_xxx:`` / ``xxxx@chatroom:`` 前缀，再嗅 XML——
        # 顺序反了的话，形如 ``<群 id>: <xml>`` 的正文会绕过嗅探。
        guessed, text = _split_sender_prefix(text)
        if not text or _looks_like_xml(text):
            text = fallback or ("[卡片]" if _looks_like_xml(text) else "")
        sender_name = names.get(int(sender or 0), "") or guessed
        mine = bool(self_rowid and int(sender or 0) == self_rowid)
        shown = contacts.display(sender_name) if sender_name else ""
        who = "我" if mine else ("" if _looks_like_id(shown) else shown)
        out.append(
            {
                "chat": chat,
                "text": text or fallback,
                "mine": mine,
                "time": int(ts or 0),
                "who": who,
            }
        )
    out.reverse()
    return out


def _rows_legacy(conn: sqlite3.Connection, chat: str, *, limit: int,
                 self_rowid: int) -> list[dict[str, Any]]:
    """3.x：一张大表 + ``str_talker``。找不到就返回空（宁缺勿猜）。"""
    where = " OR ".join("name LIKE ?" for _ in _LEGACY_TABLE_LIKE)
    tables = [str(r[0]) for r in conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND ({where})",
        tuple(_LEGACY_TABLE_LIKE),
    )]
    found: list[dict[str, Any]] = []
    for table in tables:
        cols = _columns(conn, table)
        talker_col = _pick(cols, _LEGACY_TALKER_COLUMNS)
        content_col = _pick(cols, _LEGACY_CONTENT_COLUMNS)
        time_col = _pick(cols, _TIME_COLUMNS)
        if not (talker_col and content_col and time_col):
            continue
        sender_col = _pick(cols, _LEGACY_SENDER_COLUMNS)
        type_col = _pick(cols, ("local_type", "type", "Type"))
        select = [f'"{talker_col}"', f'"{content_col}"']
        select.append(f'"{sender_col}"' if sender_col else "0")
        select.append(f'"{type_col}"' if type_col else "0")
        select.append(f'"{time_col}"')
        sql = f'SELECT {", ".join(select)} FROM "{table}"'
        params: tuple[Any, ...] = ()
        if chat:
            sql += f' WHERE "{talker_col}" = ?'
            params = (chat,)
        sql += " ORDER BY 5 DESC LIMIT ?"
        try:
            rows = conn.execute(sql, (*params, max(1, int(limit)))).fetchall()
        except sqlite3.Error as exc:
            log.debug("wechat_table_skip table=%s error=%s", table, exc)
            continue
        for talker, content, sender, local_type, ts in rows:
            text = _as_text(content) or _placeholder(int(local_type or 0))
            if _looks_like_xml(text):
                text = _placeholder(int(local_type or 0)) or "[卡片]"
            found.append(
                {
                    "chat": str(talker or ""),
                    "text": text,
                    "mine": bool(sender),
                    "time": int(ts or 0),
                }
            )
    found.sort(key=lambda r: r["time"])
    return found[-max(1, int(limit)):]


def read_recent(
    db_path: str | Path,
    chat: str = "",
    *,
    limit: int = RECENT_LIMIT,
    account_id: str = "",
) -> list[dict[str, Any]]:
    """从解密后的微信库里取某个会话最近 ``limit`` 条（按时间升序）。

    4.x 形状：每个会话一张 ``Msg_<md5(会话 id)>`` 表，正文列是 ``message_content``，
    「是不是我」看 ``real_sender_id`` 是否等于 ``Name2Id`` 里自己那一行的 rowid。
    3.x 形状：一张大表 + ``str_talker`` 列。两种都认；都对不上就返回空（宁缺勿猜）。
    ``chat`` 为空时取所有会话里最近的 ``limit`` 条。
    """
    path = Path(db_path)
    if not path.exists():
        raise GuiError("not_connected", f"没有解密后的库：{path}")
    own = str(account_id or path.parent.name or "")
    conn = _open_ro(path)
    try:
        self_rowid = _self_rowid(conn, own)
        if chat:
            table = _chat_table(conn, _resolve_chat(conn, chat))
            if table:
                return _rows_v4(conn, table, chat, limit=limit, self_rowid=self_rowid)
            return _rows_legacy(conn, chat, limit=limit, self_rowid=self_rowid)[-limit:]
        merged: list[dict[str, Any]] = []
        for name in _chat_ids(conn):
            table = _chat_table(conn, name)
            if not table:
                continue
            merged.extend(_rows_v4(conn, table, name, limit=limit, self_rowid=self_rowid))
        if merged:
            merged.sort(key=lambda r: r["time"])
            return merged[-max(1, int(limit)):]
        return _rows_legacy(conn, "", limit=limit, self_rowid=self_rowid)[-limit:]
    finally:
        conn.close()


def _id_columns(cols: Sequence[str]) -> tuple[str, str]:
    """取（行号列, 服务器消息号列）。4.x 是 local_id / server_id，3.x 是 msgId / msgSvrId。"""
    id_col = "local_id" if "local_id" in cols else _pick(cols, ("msgId", "MsgId", "id"))
    sid_col = "server_id" if "server_id" in cols else _pick(
        cols, ("serverId", "msgSvrId", "MsgSvrId", "serverSeq")
    )
    return id_col, sid_col


def latest_row(
    db_path: str | Path,
    *,
    account_id: str = "",
    max_sessions: int = 8,
) -> dict[str, Any]:
    """全库最新的一行（跨会话）——给自检算「落盘延迟」用，不做业务判断。

    4.x 每个会话一张表，所以只能逐表取 ``MAX(create_time)`` 再比大小；
    3.x 走大表。读不到就返回空字典（自检要的是「读不读得到」，不是抛错）。
    """
    from pathlib import Path as _P

    path = _P(db_path)
    if not path.exists():
        return {}
    try:
        conn = _open_ro(path)
    except GuiError:
        warn("core/execution/gui/wechat.py:531 GuiError", err=repr(__import__("sys").exc_info()[1]))
        return {}
    try:
        best: dict[str, Any] = {}
        best_ts = 0
        if _has_table(conn, "Name2Id"):
            for chat in _chat_ids(conn)[:max(1, int(max_sessions))]:
                table = _chat_table(conn, chat)
                if not table:
                    continue
                cols = _columns(conn, table)
                if "create_time" not in cols:
                    continue
                row = conn.execute(
                    f'SELECT create_time FROM "{table}" ORDER BY create_time DESC LIMIT 1'
                ).fetchone()
                if row and int(row[0] or 0) > best_ts:
                    best_ts = int(row[0] or 0)
                    best = {"chat": chat, "create_time": best_ts, "table": table}
        else:
            where = " OR ".join("name LIKE ?" for _ in _LEGACY_TABLE_LIKE)
            tables = [str(r[0]) for r in conn.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND ({where})",
                tuple(_LEGACY_TABLE_LIKE),
            )]
            for table in tables:
                cols = _columns(conn, table)
                col = _pick(cols, _TIME_COLUMNS)
                if not col:
                    continue
                row = conn.execute(
                    f'SELECT "{col}" FROM "{table}" ORDER BY "{col}" DESC LIMIT 1'
                ).fetchone()
                if row and int(row[0] or 0) > best_ts:
                    best_ts = int(row[0] or 0)
                    best = {"create_time": best_ts, "table": table}
        return best
    except sqlite3.Error:
        warn("core/execution/gui/wechat.py:568 sqlite3.Error", err=repr(__import__("sys").exc_info()[1]))
        return {}
    finally:
        conn.close()


def max_local_id(
    db_path: str | Path,
    chat: str,
    *,
    account_id: str = "",
) -> int:
    """某会话现有最大行号——**发送前记下它**，回读只认比它大的那几行。"""
    path = Path(db_path)
    if not path.exists():
        raise GuiError("not_connected", f"没有解密后的库：{path}")
    conn = _open_ro(path)
    try:
        table = _chat_table(conn, _resolve_chat(conn, chat))
        if not table:
            return 0
        id_col, _ = _id_columns(_columns(conn, table))
        if not id_col:
            return 0
        row = conn.execute(f'SELECT MAX("{id_col}") FROM "{table}"').fetchone()
        return int((row[0] if row else 0) or 0)
    finally:
        conn.close()


def rows_after(
    db_path: str | Path,
    chat: str,
    *,
    after_local_id: int = 0,
    limit: int = 20,
    account_id: str = "",
) -> list[dict[str, Any]]:
    """取某会话 ``行号 > after_local_id`` 的新行（按行号升序）。

    发送后对账专用：只看「按回车之后新增的那几行」，绝不把历史里某句相同的话
    当成我们刚发出去的那一条。
    """
    path = Path(db_path)
    if not path.exists():
        raise GuiError("not_connected", f"没有解密后的库：{path}")
    conn = _open_ro(path)
    try:
        table = _chat_table(conn, _resolve_chat(conn, chat))
        if not table:
            return []
        cols = _columns(conn, table)
        id_col, sid_col = _id_columns(cols)
        content_col = "message_content" if "message_content" in cols else _pick(
            cols, _LEGACY_CONTENT_COLUMNS
        )
        if not id_col or not content_col:
            return []
        sender_col = "real_sender_id" if "real_sender_id" in cols else (
            _pick(cols, _LEGACY_SENDER_COLUMNS) or "0"
        )
        type_col = "local_type" if "local_type" in cols else "0"
        time_col = "create_time" if "create_time" in cols else (_pick(cols, _TIME_COLUMNS) or "0")
        sql = (
            f'SELECT "{id_col}", "{content_col}", {sid_col if sid_col else 0}, '
            f'{sender_col}, {type_col}, {time_col} '
            f'FROM "{table}" WHERE "{id_col}" > ? ORDER BY "{id_col}" ASC LIMIT ?'
        )
        own = str(account_id or path.parent.name or "")
        self_rowid = _self_rowid(conn, own)
        out: list[dict[str, Any]] = []
        for local_id, content, server_id, sender, local_type, ts in conn.execute(
            sql, (int(after_local_id), max(1, int(limit)))
        ):
            kind = int(local_type or 0) if str(type_col) != "0" else 0
            guessed, text = _split_sender_prefix(_as_text(content))
            if _looks_like_xml(text):
                text = ""
            out.append(
                {
                    "local_id": int(local_id or 0),
                    "server_id": int(server_id or 0),
                    "text": text,
                    "guessed_sender": guessed,
                    "mine": bool(self_rowid and int(sender or 0) == self_rowid),
                    "kind": kind,
                    "time": int(ts or 0),
                }
            )
        return out
    finally:
        conn.close()


def render_lines(rows: Sequence[Mapping[str, Any]]) -> str:
    """把最近消息渲染成 ``HH:MM 我/对方: 正文``；纯函数，不碰时钟。"""
    lines: list[str] = []
    for row in rows:
        text = str(row.get("text") or "").replace("\r", " ").strip()
        text = text.replace("\n", " ")
        if len(text) > LINE_MAX_CHARS:
            text = text[:LINE_MAX_CHARS] + "…"
        stamp = ""
        try:
            stamp = datetime.fromtimestamp(int(row.get("time") or 0)).strftime("%H:%M")
        except (OverflowError, OSError, ValueError):
            warn("core/execution/gui/wechat.py:673 (OverflowError, OSError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            stamp = "--:--"
        who = str(row.get("who") or "").strip() or ("我" if row.get("mine") else "对方")
        lines.append(f"{stamp} {who}: {text}")
    return "\n".join(lines)


def _decrypted_db(account_id: str = "") -> Path:
    """解密后的合并库在哪：先按配置里的微信账号找，找不到就挑目录里最新的那个。

    2026-10-21 修：这里原来调的是不存在的 ``config.get_accounts()``，异常被吞掉后
    ``account`` 恒为空，于是算出来的路径是 ``data/decrypted/wechat/merge_msg.db``——
    而实际库在 ``data/decrypted/wechat/<wxid>/merge_msg.db``。结果「读侧对真库跑过」
    在默认参数下会直接 `not_connected`。方法名是 ``get_all_accounts()``。
    """
    from core.config.settings import config

    account = account_id
    if not account:
        try:
            accounts = config.get_all_accounts()
        except Exception:  # pragma: no cover - 配置缺失时走空
            warn("core/execution/gui/wechat.py:694 Exception", err=repr(__import__("sys").exc_info()[1]))
            accounts = {}
        for platform, acc in accounts.items():
            if str(platform) == "wechat" and acc:
                account = str(acc)
                break
    root = Path(config.paths.get_account_decrypted_dir("wechat", account or ""))
    direct = root / "merge_msg.db"
    if direct.is_file():
        return direct
    # 账号换了 / 配置里没有 wechat 项时，别急着报「没有解密后的库」：
    # 目录里只要有一份就用最新的那份（在改账号或重登之后这是常态）。
    candidates = sorted(
        root.glob(f"{os.sep}*{os.sep}merge_msg.db") if False else root.glob("*/merge_msg.db"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else direct


def refresh_db(account_id: str = "", *, full_fallback: bool = True) -> Path:
    """增量重新解密一次，让读侧看见刚刚发出的那一行。

    读侧读的是解密后的快照；微信每写一行都进它自己的库，我们的快照不动，
    所以「发完立刻回读」如果只读旧快照会永远读不到——那不是没发出去，
    是我们没去看。密钥用配置里存的（内存扫描在客户端重启后常扫到垃圾）。
    """
    from core.config.settings import config
    from db.decrypt import decrypt_and_merge

    target = _decrypted_db(account_id)
    wx_path = str(getattr(getattr(config, "wechat", None), "db_path", "") or "")
    key = str(getattr(getattr(config, "wechat", None), "key", "") or "")
    if not wx_path or not key:
        return target
    version = int(getattr(getattr(config, "wechat", None), "version", 4) or 4)
    before = target.stat().st_mtime if target.exists() else 0.0
    try:
        decrypt_and_merge(key, wx_path, str(target.parent), version=version, incremental=True)
    except Exception:  # pragma: no cover - 解密失败只影响回读
        log.warning("wechat_db_refresh_failed")
        return target
    fresh = _decrypted_db(account_id)
    if full_fallback and fresh.exists() and fresh.stat().st_mtime <= before:
        # 增量档有时会因签名缓存判「没变化」直接跳过；回读要真看到新行，
        # 这时候全量再解一次（几秒钟，只发生在等待回读的那几十秒里）。
        try:
            decrypt_and_merge(key, wx_path, str(fresh.parent), version=version, incremental=False)
        except Exception:  # pragma: no cover
            log.warning("wechat_db_refresh_full_failed")
    return _decrypted_db(account_id)


#: 「哪个会话」的四个来源，按可信度从高到低。数据库只排最后——
#: 它只知道「最近有消息」，不知道你屏幕上开着哪个。
SOURCE_EXPLICIT = "explicit"
SOURCE_ANCHOR = "anchor"
SOURCE_OCR = "ocr"
SOURCE_RECENT_DB = "recent_db"
SOURCE_AMBIGUOUS = "ambiguous"
SOURCE_NONE = "none"

SOURCE_CONFIDENCE = {
    SOURCE_EXPLICIT: 1.0,
    SOURCE_ANCHOR: 0.9,
    SOURCE_OCR: 0.7,
    SOURCE_AMBIGUOUS: 0.5,
    SOURCE_RECENT_DB: 0.3,
    SOURCE_NONE: 0.0,
}


@dataclass(frozen=True)
class CurrentChat:
    """「现在屏幕上开着哪个会话」的结论，连它有多可信一起给你。"""

    chat_id: str = ""
    source: str = SOURCE_NONE
    confidence: float = 0.0
    display: str = ""

    @property
    def known(self) -> bool:
        return bool(self.chat_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "source": self.source,
            "confidence": self.confidence,
            "display": self.display,
        }


def _reverse_names(db_path: str | Path) -> dict[str, list[str]]:
    """显示名 → [会话 id]。一个名字可能对多个会话——那种时候要弃权，不能猜。"""
    path = Path(db_path)
    if not path.exists():
        return {}
    conn = _open_ro(path)
    try:
        contacts = _ContactBook(path.parent / "contact.db")
        out: dict[str, list[str]] = {}
        for chat in _chat_ids(conn):
            name = contacts.display(chat)
            out.setdefault(name or chat, []).append(chat)
        return out
    finally:
        conn.close()


def recent_chat(db_path: str | Path, *, account_id: str = "") -> CurrentChat:
    """数据库能给的最低一档：最近有消息的那个会话（**不是**屏幕上那个）。"""
    path = Path(db_path)
    if not path.exists():
        return CurrentChat()
    conn = _open_ro(path)
    try:
        best_chat, best_ts = "", -1
        for chat in _chat_ids(conn):
            table = _chat_table(conn, chat)
            if not table:
                continue
            time_col = "create_time" if "create_time" in _columns(conn, table) else _pick(
                _columns(conn, table), _TIME_COLUMNS
            )
            if not time_col:
                continue
            try:
                row = conn.execute(f'SELECT MAX("{time_col}") FROM "{table}"').fetchone()
            except sqlite3.Error:
                warn("core/execution/gui/wechat.py:824 sqlite3.Error", err=repr(__import__("sys").exc_info()[1]))
                continue
            ts = int(row[0] or 0) if row else 0
            if ts > best_ts:
                best_chat, best_ts = chat, ts
    finally:
        conn.close()
    if not best_chat:
        return CurrentChat()
    return CurrentChat(
        chat_id=best_chat,
        source=SOURCE_RECENT_DB,
        confidence=SOURCE_CONFIDENCE[SOURCE_RECENT_DB],
    )


def resolve_current_chat(
    *,
    explicit: str = "",
    anchor: Callable[[], str] | None = None,
    ocr: Callable[[], str] | None = None,
    db_path: str | Path | None = None,
    account_id: str = "",
) -> CurrentChat:
    """按可信度排队问「现在开着哪个会话」；**认不准就返回空，不猜**。

    ``ocr`` 返回的是屏幕上读到的显示名，按 ``contact.db`` 反查会话 id；
    同名会话多于一个时返回 ``ambiguous``（置信度 0.5、chat_id 空）——错会话比没会话更坏。
    """
    if explicit:
        return CurrentChat(
            chat_id=explicit,
            source=SOURCE_EXPLICIT,
            confidence=SOURCE_CONFIDENCE[SOURCE_EXPLICIT],
        )
    if anchor is not None:
        try:
            got = str(anchor() or "")
        except Exception as exc:  # 锚点探针不许把回合打挂
            log.debug("wechat_anchor_failed error=%s", exc)
            got = ""
        if got:
            return CurrentChat(chat_id=got, source=SOURCE_ANCHOR, confidence=SOURCE_CONFIDENCE[SOURCE_ANCHOR])
    if ocr is not None:
        try:
            shown = str(ocr() or "").strip()
        except Exception as exc:
            log.debug("wechat_ocr_failed error=%s", exc)
            shown = ""
        if shown:
            if db_path is None:
                return CurrentChat(display=shown, source=SOURCE_AMBIGUOUS, confidence=SOURCE_CONFIDENCE[SOURCE_AMBIGUOUS])
            table = _reverse_names(db_path)
            hits = table.get(shown, [])
            if len(hits) == 1:
                return CurrentChat(
                    chat_id=hits[0],
                    source=SOURCE_OCR,
                    confidence=SOURCE_CONFIDENCE[SOURCE_OCR],
                    display=shown,
                )
            return CurrentChat(display=shown, source=SOURCE_AMBIGUOUS, confidence=SOURCE_CONFIDENCE[SOURCE_AMBIGUOUS])
    if db_path is not None:
        return recent_chat(db_path, account_id=account_id)
    return CurrentChat()


_EVIDENCE_SUFFIX = {
    SOURCE_RECENT_DB: "（最近活跃，不是屏幕上的会话）",
    SOURCE_AMBIGUOUS: "（重名，未确定）",
    SOURCE_ANCHOR: "（锚点确认）",
    SOURCE_OCR: "（顶栏读到）",
}


def _title_with_evidence(shown: str, count: int, source: str) -> str:
    """标题里必须写出「这个会话是怎么认出来的」——数据库那档不许冒充屏幕。"""
    return f"微信 · {shown} · {count} 条{_EVIDENCE_SUFFIX.get(source, '')}"


def _anchor_chat() -> str:
    """0.9 档证据：我们自己最近打开/确认过的那个会话（旧 UI 通道记的锚点）。

    只有旧通道 ``core/execution/wx_chat.py`` 记得住这个；没跑过就是空串。
    延迟 import——那个模块拖着 OCR / 截图依赖，不该在读库时被拉起来。
    """
    try:
        from core.execution import wx_chat

        getter = getattr(wx_chat, "active_chat_id", None)
        if getter is None:
            return ""
        return str(getter() or "")
    except Exception as exc:  # pragma: no cover - 纯兜底
        log.debug("wechat_anchor_probe_failed error=%s", exc)
        return ""


def current_chat(
    *,
    anchor: Callable[[], str] | None = None,
    ocr: Callable[[], str] | None = None,
    db_path: str | Path | None = None,
    account_id: str = "",
) -> CurrentChat:
    """真实环境下的「现在开着哪个会话」：锚点 → （可选）顶栏 OCR → 数据库最近活跃。"""
    path = Path(db_path) if db_path else _decrypted_db(account_id)
    return resolve_current_chat(
        anchor=anchor if anchor is not None else _anchor_chat,
        ocr=ocr,
        db_path=path,
        account_id=account_id,
    )


def snapshot(
    chat: str = "",
    *,
    db_path: str | Path | None = None,
    account_id: str = "",
    limit: int = RECENT_LIMIT,
    max_chars: int = SNAPSHOT_MAX_CHARS,
    taken_at: str = "",
) -> GuiSnapshot:
    """读微信（读的是库，不是界面）：当前会话最近几条。"""
    if _mode() == features.MODE_OFF:
        raise GuiError("blocked", "WECHAT_UI=off：微信读侧没开")
    path = Path(db_path) if db_path else _decrypted_db(account_id)
    contacts = _ContactBook(path.parent / "contact.db")
    picked = CurrentChat(chat_id=chat, source=SOURCE_EXPLICIT, confidence=1.0) if chat else (
        resolve_current_chat(anchor=_anchor_chat, db_path=path, account_id=account_id)
    )
    if not picked.known:
        reason = "会话名有重名，我不猜" if picked.source == SOURCE_AMBIGUOUS else "不知道现在是哪个会话"
        raise GuiError("no_target", f"{reason}（证据档={picked.source}）")
    chat = picked.chat_id
    shown = contacts.display(chat)
    rows = read_recent(path, chat, limit=limit, account_id=account_id)
    text, truncated = normalize_text(render_lines(rows), max_chars=max_chars)
    stamp = taken_at or datetime.now().isoformat(sep=" ", timespec="seconds")
    return GuiSnapshot(
        target=chat or "(最近)",
        url=f"wechat://{chat or 'recent'}",
        title=_title_with_evidence(shown or chat, len(rows), picked.source),
        text=text,
        taken_at=stamp,
        truncated=truncated,
    )


class WechatUiDeps(Protocol):
    """填字要用的四件事——测试里换成假的，真跑时是 ``wx_chat`` / ``wx_human``。"""

    def window_title(self) -> str: ...

    def foreground_title(self) -> str: ...

    def wake(self) -> bool:
        """把窗口唤到前台；唤不出来返回 False（调用方据此 fail closed）。"""

    def open_chat(self, chat: str) -> Mapping[str, Any]: ...

    def focus_message_box(self) -> bool: ...

    def paste(self, text: str) -> bool: ...

    def press_enter(self) -> bool: ...

    def message_box_has_text(self) -> bool | None: ...

    def message_box_text(self) -> str | None: ...

    def clear_message_box(self) -> bool: ...

    def mention(self, name: str) -> bool: ...

    def screenshot(self) -> bytes | None: ...


def _looks_like_wechat(title: str) -> bool:
    return any(window_title_matches(hint, title or "") for hint in WECHAT_TITLE_HINTS)


def _assert_composer_clean(impl: Any, *, chat: str) -> bool:
    """拿到手之后先看一眼输入框：有残留就**停手报错**，绝不清空。

    2026-10-28 事故「哦」之后改的：这个函数原来叫 `_clear_dirty_composer`，
    会把框里的残留擦掉。擦掉是错的——残留可能是上一次没发出去的一句真话，
    也可能是主人自己打了一半的字。「读得到残留」本身已经够用来叫停，
    不需要再补一个破坏动作。人工清理走 `scripts/wx_clear_box.py`。
    """
    reader = getattr(impl, "message_box_text", None)
    if not callable(reader):
        return False
    try:
        text = reader()
    except Exception as exc:  # noqa: BLE001 - 读不到就当作「不知道」
        log.warning("dirty_composer_read_failed error=%r", exc)
        return False
    if not str(text or "").strip():
        return False

    device_metrics.bump(
        "dirty_composer_blocked",
        chat=str(chat)[:40],
        chars=len(str(text or "")),
    )
    raise GuiError(
        "blocked",
        "输入框里有残留（我不清空、也不按回车）；要清理请人工跑 scripts/wx_clear_box.py",
    )


@dataclass(frozen=True)
class FillResult:
    """半程回执：字填进去了，但**没有发**。"""

    target: str
    chat: str
    text_sha256: str
    foreground_ok: bool
    title_ok: bool
    focus_ok: bool
    filled_verified: bool
    pixels_changed: int
    evidence_before: str = ""
    evidence_after: str = ""
    at: str = ""
    sent: bool = False

    def as_receipt(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "chat": self.chat,
            "text_sha256": self.text_sha256,
            "foreground_ok": self.foreground_ok,
            "title_ok": self.title_ok,
            "focus_ok": self.focus_ok,
            "filled_verified": self.filled_verified,
            "pixels_changed": self.pixels_changed,
            "evidence_before": self.evidence_before,
            "evidence_after": self.evidence_after,
            "at": self.at,
            "sent": False,
        }


def _write_evidence(png: bytes | None, evidence_dir: str | Path, turn_id: str, label: str) -> str:
    if not png:
        return ""
    root = Path(evidence_dir)
    root.mkdir(parents=True, exist_ok=True)
    stem = f"{turn_id or 'wechat'}_wechat_fill_{label}"
    target = root / f"{stem}.png"
    index = 2
    while target.exists():
        target = root / f"{stem}-{index}.png"
        index += 1
    try:
        target.write_bytes(png)
    except OSError as exc:  # 证据写不下去不算动作失败，但要留话
        log.warning("wechat_evidence_write_failed path=%s error=%s", target, exc)
        return ""
    return str(target)


def display_name_for(chat: str) -> str:
    """把会话地址（``wxid_…`` / ``…@chatroom``）翻成微信里显示的名字。

    为什么必须有这一层：出口传进来的是**地址**（``chat_id``），而微信搜索框只认
    **名字**。2026-10-21 第一次开新出口真机就撞上了——搜索框里被打进
    ``wxid_seed01``，搜索无结果，`open_chat_strict` 顶栏验收失败，
    消息一条没发出去（fail-closed 是对的，但名字该我们自己翻）。
    查不到就原样返回：宁可让`open_chat_strict`去判失败，也不猜一个名字。
    """
    text = str(chat or "").strip()
    if not text:
        return ""
    try:
        from core.execution import wx_chat

        got = str(wx_chat.resolve_nickname(text) or "").strip()
        if got and got != text:
            log.info("wechat_display_name_resolved chat=%s name=%s", text, got)
        return got or text
    except Exception:  # noqa: BLE001 - 查不到名字就原样试
        warn("core/execution/gui/wechat.py:1109 Exception", err=repr(__import__("sys").exc_info()[1]))
        return text


def _already_there(chat: str, *, verify=None) -> bool:
    """顶栏就是目标会话吗？读不到就算没到（退回搜索）。"""
    fn = verify
    if fn is None:
        from core.execution import wx_chat

        fn = wx_chat.verify_active_chat_title
    try:
        return bool(fn(chat)[0])
    except Exception as exc:
        from safelog import warn

        warn("gui_reuse_check_failed", chat=chat, error=repr(exc)[:80])
        return False


def _default_deps() -> WechatUiDeps:  # pragma: no cover - 真机路径
    from core.execution import wx_chat
    from core.execution import wx_vision

    class _Deps:
        def window_title(self) -> str:
            win = wx_vision.find_wechat_window()
            return getattr(win, "title", "") if win else ""

        def foreground_title(self) -> str:
            win = wx_vision.find_wechat_window()
            if win and win.is_foreground():
                return getattr(win, "title", "")
            return ""

        def wake(self) -> bool:
            return wake_window()

        def open_chat(self, chat: str) -> Mapping[str, Any]:
            # 用「顶栏验收」的那一版：搜索列表的 OCR 会截断名字，
            # 而顶栏才是消息真正要落进去的地方。
            if _already_there(chat):
                return {"ok": True, "chat": chat, "reused": True}
            return wx_chat.open_chat_strict(chat) or {}

        def focus_message_box(self) -> bool:
            return bool(wx_chat.focus_message_box())

        def paste(self, text: str) -> bool:
            # 2026-10-28（SEC-006）：打字只走 wx_chat.type_text 这一个入口，
            # 别再直连 wx_human.paste_text（同一件事两个入口，改一处漏一处）。
            return bool(wx_chat.type_text(text))

        def press_enter(self) -> bool:
            try:
                wx_chat.press_enter()
                return True
            except Exception:  # pragma: no cover - 驱动异常
                warn("core/execution/gui/wechat.py:1146 Exception", err=repr(__import__("sys").exc_info()[1]))
                return False

        def mention(self, name: str) -> bool:
            # 群里 @ 一个人：名单里唯一匹配才选，@ 错人比不发更糟。
            return bool(wx_chat.mention_member(name).get("ok"))

        def message_box_has_text(self) -> bool | None:
            try:
                return bool(wx_chat.message_box_has_text())
            except Exception:  # pragma: no cover - OCR 不可用
                warn("core/execution/gui/wechat.py:1156 Exception", err=repr(__import__("sys").exc_info()[1]))
                return None

        def message_box_text(self) -> str | None:
            try:
                return wx_chat.message_box_text()
            except Exception:  # pragma: no cover - OCR 不可用
                warn("core/execution/gui/wechat.py:1162 Exception", err=repr(__import__("sys").exc_info()[1]))
                return None

        def clear_message_box(self) -> bool:
            try:
                return bool(wx_chat.clear_message_box())
            except Exception:  # pragma: no cover - 驱动异常
                warn("core/execution/gui/wechat.py:1168 Exception", err=repr(__import__("sys").exc_info()[1]))
                return False

        def screenshot(self) -> bytes | None:
            return None  # 由调用方（工具层）决定要不要留证据

    return _Deps()


def wake_window(title_hint: str = "") -> bool:
    """把微信主窗口唤到前台（托盘里 / 最小化都能救），并**验一次**它真的在前台。

    为什么不能只调一次 `ShowWindow`：微信 4.x「关窗口」= 隐藏到托盘，
    `IsWindowVisible` 可能是 False；而在**低完整性**进程里 `ShowWindow` 还会 err=5、
    `SetForegroundWindow` 全失败（踩过：`.venv_it` 被打成 Low 标签）。所以分三段：
    UIA `Show/SetFocus` → `wx_vision.activate_window` → 最后用 `GetForegroundWindow`
    与 `IsIconic` 各验一次；验不过就 False，调用方 fail closed，不许猜。
    """
    try:
        import win32gui
    except Exception:  # noqa: BLE001 - 没有 pywin32 就当唤不出来
        warn("core/execution/gui/wechat.py:1188 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False

    hwnd = 0
    try:
        from core.execution import wx_vision

        win = wx_vision.find_wechat_window()
        hwnd = int(getattr(win, "hwnd", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("wechat_wake_find_failed error=%r", exc)

    try:
        from core.execution.gui.uia_win import AutomationBackend

        hint = str(title_hint or (WECHAT_TITLE_HINTS[0] if WECHAT_TITLE_HINTS else "微信"))
        AutomationBackend().show(hint)
    except Exception as exc:  # noqa: BLE001
        log.warning("wechat_wake_uia_failed error=%r", exc)

    if hwnd:
        try:
            from core.execution import wx_vision

            wx_vision.activate_window(wx_vision.find_wechat_window())
        except Exception as exc:  # noqa: BLE001
            log.warning("wechat_wake_activate_failed error=%r", exc)
        try:
            import ctypes

            foreground = int(win32gui.GetForegroundWindow() or 0)
            minimized = bool(ctypes.windll.user32.IsIconic(hwnd))
            if foreground == hwnd and not minimized:
                return True
        except Exception:  # noqa: BLE001
            warn("core/execution/gui/wechat.py:1222 Exception", err=repr(__import__("sys").exc_info()[1]))
            return False

    try:
        text = str(win32gui.GetWindowText(win32gui.GetForegroundWindow()) or "")
    except Exception:  # noqa: BLE001
        warn("core/execution/gui/wechat.py:1227 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False
    return _looks_like_wechat(text)


DEVICE_RESOURCE = device_lease_mod.RESOURCE_INPUT
"""本写路径独占的资源名。**设备级**命名，不叫 ``wechat_ui``——接第二个桌面平台时
两者必须撞在同一把锁上，而不是互不知道（这是设计文档 §4.1 的硬要求）。"""


_GUARD_STATE = threading.local()


def _guard_depth() -> int:
    return int(getattr(_GUARD_STATE, "depth", 0))


def device_lease_mode() -> str:
    """``DEVICE_LEASE`` 的三档。唯一读点。"""
    return features.mode(features.DEVICE_LEASE, features.MODE_OFF)


# 心跳超过 2×TTL 才算「卡死」：TTL 只表示「这段时间内它应该在动」，
# 一个心跳还能更新但动作很久没返回的持有者要先怀疑是慢，不是死。
STUCK_FACTOR = 2.0


def _note_busy(resource: str, busy: Any) -> None:
    """拿不到手时把「谁占着、心跳还新不新」记下来。

    为什么要分开看：**进程死了**由 OS 立刻回收锁（真机实测 0.05 秒），所以等不到手
    通常意味着**持有者还活着**——要么它正干着（正常排队），要么它卡住了（事故）。
    两者靠心跳年龄区分：心跳新 → 排队，心跳老 → 事故，后者才该报警。
    """
    try:
        info = device_lease_mod.describe_holder(resource)
    except Exception as exc:  # noqa: BLE001 - 排障信息拿不到不该改变失败方式
        log.warning("device_lease_busy_state_failed resource=%s error=%r", resource, exc)
        info = {}
    age = float(info.get("heartbeat_age_s", -1.0) or -1.0)
    ttl = float(getattr(device_lease_mod, "DEFAULT_TTL_S", 60.0) or 60.0)
    stuck = age > ttl * STUCK_FACTOR
    log.warning(
        "device_lease_busy resource=%s holder=%s pid=%s heartbeat_age_s=%s age_s=%s stuck=%s",
        resource,
        info.get("holder", ""),
        info.get("pid", 0),
        age,
        info.get("age_s", -1.0),
        stuck,
    )
    if not stuck:
        return
    try:
        device_metrics.bump(
            "lease_stuck",
            holder=str(info.get("holder") or ""),
            pid=int(info.get("pid") or 0),
            heartbeat_age_s=age,
        )
    except Exception as exc:  # noqa: BLE001 - 量具坏了也不许改变失败方式
        log.warning("device_lease_stuck_metric_failed error=%r", exc)


@contextlib.contextmanager
def device_guard(owner: str, *, resource: str = DEVICE_RESOURCE) -> Iterator[Any]:
    """动手前唯一的一道门：能力自检 + 输入租约。

    三档：

    * ``off``（默认）—— **一行都不执行**，行为与接线前逐字相同；
    * ``shadow`` —— 探一次能力、记一行日志，不拿锁也不拦（用来攒量具）；
    * ``on`` —— 能力不过抛 ``CapabilityMissing``；拿不到租约抛 ``UiBusy``；
      正常或异常退出都释放。

    租约只包住**物理动作**这一段（切会话 → 聚焦 → 填字）。发送后的落盘回读
    （15–30 秒）不在门内——持锁等平台会把所有人的手堵住。
    """
    mode = device_lease_mode()
    if mode == features.MODE_OFF:
        yield None
        return
    if _guard_depth() > 0:
        # 已经在门里（send_and_verify 把「填字 + 回车」包成一整段）：
        # 文件锁不可重入，重复加锁只会把自己挡住。
        yield None
        return

    report: Any = None
    try:
        report = capability_probe.probe()
    except Exception as exc:  # noqa: BLE001 - 自检本身崩了也只能降级
        log.warning("device_guard_probe_failed error=%r", exc)

    if mode == features.MODE_SHADOW:
        log.info(
            "device_guard_shadow caps=%s",
            report.one_line() if report is not None else "probe_failed",
        )
        yield None
        return

    if report is None:
        raise capability_probe.CapabilityMissing("probe_missing")
    if not report.ok and set(report.failures()) <= {"writable", "identity"}:
        # 2026-10-24：这两项不过几乎总是「微信被最小化 / 收进托盘」。以前这里直接
        # 抛 CapabilityMissing，整条回复被静默丢掉（send_log 里那 166 次
        # blocked:composer_unwritable 全是这么来的）。先唤一次窗口再探一次：
        # wake_window() 本来就是为「托盘里 / 最小化」写的，救得回来就照常发。
        try:
            if wake_window():
                report = capability_probe.probe()
                log.info("device_guard_rewoke caps=%s", report.one_line())
        except Exception as exc:  # noqa: BLE001 - 唤窗失败就按原样拦
            log.warning("device_guard_wake_failed error=%r", exc)
    capability_probe.require(report)

    started = time.monotonic()
    try:
        held = device_lease_mod.device_lease(resource, owner or "wechat")
    except device_lease_mod.UiBusy as busy:
        _note_busy(resource, busy)
        raise
    device_metrics.bump(
        "input_wait_ms",
        value=int((time.monotonic() - started) * 1000),
        resource=resource,
    )
    _GUARD_STATE.depth = _guard_depth() + 1
    try:
        yield held
    finally:
        _GUARD_STATE.depth = max(0, _guard_depth() - 1)
        try:
            held.release()
        except Exception as exc:  # noqa: BLE001 - 释放失败只记，不盖住原异常
            log.warning("device_lease_release_failed error=%r", exc)


def fill_message(
    chat: str,
    text: str,
    *,
    mention: str = "",
    deps: WechatUiDeps | None = None,
    turn_id: str = "",
    evidence_dir: str | Path = "data/evidence",
) -> FillResult:
    """公开入口：先过设备租约（``DEVICE_LEASE``），再填字。"""
    with device_guard(turn_id or "wechat"):
        return _fill_message(
            chat,
            text,
            mention=mention,
            deps=deps,
            turn_id=turn_id,
            evidence_dir=evidence_dir,
        )


def _fill_message(
    chat: str,
    text: str,
    *,
    mention: str = "",
    deps: WechatUiDeps | None = None,
    turn_id: str = "",
    evidence_dir: str | Path = "data/evidence",
) -> FillResult:
    """切到会话、聚焦输入框、把字填进去——**永不按发送**。"""
    if _mode() == features.MODE_OFF:
        raise GuiError("blocked", "WECHAT_UI=off：微信写侧没开")
    body = str(text or "").strip()
    if not body:
        raise GuiError("unsupported", "空文本不填")
    body = body[:FILL_MAX_CHARS]
    if not str(chat or "").strip():
        raise GuiError("no_target", "没给会话名")
    impl = deps or _default_deps()

    title = str(impl.window_title() or "")
    foreground = str(impl.foreground_title() or "")
    foreground_ok = _looks_like_wechat(foreground)
    title_ok = _looks_like_wechat(title)
    if not title_ok:
        raise GuiError("not_connected", f"没找到微信窗口（标题={title!r}）")

    if not foreground_ok:
        # 托盘里 / 最小化时先唤一次；唤不出来就不许往下走——否则会在**别的窗口**上
        # 敲字，然后按一个回车把半句话发给当时开着的那个会话（出过事）。
        wake = getattr(impl, "wake", None)
        woke = bool(wake()) if callable(wake) else False
        if not woke:
            raise GuiError(
                "no_target",
                f"微信窗口唤不出来（前台={foreground!r}，窗口={title!r}）",
            )

    # 在目标会话里就直接用（`_Deps.open_chat` 里先问顶栏，问不到才搜索）。
    opened = impl.open_chat(chat)
    if isinstance(opened, Mapping) and opened.get("ok") is False:
        # 切不过去就不许往下走：搜索验证失败时 open_chat 会 fail-closed，
        # 但这里原来把返回值丢了，于是「没切到」被当成「切到了」继续填字，
        # 下一个回车就会发给当时开着的那个会话。
        raise GuiError(
            "no_target",
            "没切到会话（%s）：%s" % (chat, opened.get("error") or "未知原因"),
        )
    if not impl.focus_message_box():
        raise GuiError("no_target", f"输入框没聚焦（会话={chat!r}）")

    _assert_composer_clean(impl, chat=chat)
    if str(mention or "").strip():
        # @ 必须落在正文之前：微信的成员名单是靠「@ + 前缀」筛出来的，
        # 先把正文打进去就会把名单筛空。
        try:
            mentioned = bool(impl.mention(str(mention).strip().lstrip("@")))
        except Exception as exc:  # pragma: no cover - 驱动异常
            raise GuiError("unsupported", f"mention_failed：{type(exc).__name__}: {exc}")
        if not mentioned:
            raise GuiError("unsupported", f"mention_failed：没 @ 上 {mention}")

    before = impl.screenshot()
    pasted = bool(impl.paste(body))
    after = impl.screenshot()
    verified = impl.message_box_has_text()
    pixels = count_changed_pixels(before, after) if before and after else -1
    stamp = datetime.now().isoformat(sep=" ", timespec="seconds")
    return FillResult(
        target="wechat",
        chat=str(chat),
        text_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
        foreground_ok=foreground_ok,
        title_ok=title_ok,
        focus_ok=pasted,
        filled_verified=bool(verified) if verified is not None else pasted,
        pixels_changed=pixels,
        evidence_before=_write_evidence(before, evidence_dir, turn_id, "before"),
        evidence_after=_write_evidence(after, evidence_dir, turn_id, "after"),
        at=stamp,
    )
