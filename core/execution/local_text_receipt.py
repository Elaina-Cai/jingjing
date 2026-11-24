"""本地文字回读：精确账号路径、会话、发送前游标；不代表送达或已读。"""
import contextlib
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


def account_path(root, platform, account):
    if platform != "wechat" or not account or Path(account).name != account or any(c in account for c in "/\\:") or account in {".", ".."}:
        return None
    path = Path(root) / "data" / "platforms" / platform / account / "account.db"
    return path if path.is_file() else None


@contextlib.contextmanager
def _read(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        yield con
    finally:
        con.close()


class ObservationSourceChanged(ValueError):
    pass


def _file_identity(path):
    stat = Path(path).stat()
    if not stat.st_ino:
        raise ObservationSourceChanged("source_identity_unavailable")
    return [stat.st_dev, stat.st_ino]


def _boundary(con, after):
    rows = con.execute("select id,chat_id,content,sender_id from messages where id=? order by id", (int(after),)).fetchall()
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def capture(path):
    """发送前读取文件身份、插入游标及边界内容；不写来源库。"""
    if path is None:
        return None, ""
    identity = _file_identity(path)
    with _read(path) as con:
        con.execute("begin")
        after = int(con.execute("select coalesce(max(id),0) from messages").fetchone()[0])
        snapshot = [identity, after, _boundary(con, after)]
        columns = {row[1] for row in con.execute("pragma table_xinfo(messages)")}
        if {"created_at", "synced_at"} <= columns:
            snapshot.append((datetime.now() - timedelta(seconds=2)).isoformat(sep=" ", timespec="microseconds"))
        token = json.dumps(snapshot, ensure_ascii=False)
    if _file_identity(path) != identity:
        raise ObservationSourceChanged("source_replaced_during_capture")
    return after, token


def baseline(path):
    if path is None:
        return None
    with _read(path) as con:
        return int(con.execute("select coalesce(max(id),0) from messages").fetchone()[0])


class SenderEvidenceUnavailable(ValueError):
    pass


def _own_id(path) -> str:
    """发送账号自己的 wxid（account.db 所在目录名）。不是合法账号名就返回空。"""
    try:
        name = Path(path).parent.name
    except (TypeError, OSError):
        return ""
    if not name or len(name) > 64:
        return ""
    if not all(ch.isalnum() or ch in "_-.@" for ch in name):
        return ""
    return name


def _self_predicate(con, path=None):
    """自己的消息：历史行用 sender_id='self'，现役采集行用账号自己的 wxid。

    2026-11-24：生产 account.db 里自发消息是 `sender_id=wxid_xxx`（is_self=1），
    旧判定只认字面量 'self' → 回读五次都找不到，ack 永远 unconfirmed，她还把
    「可能没送到」转告给主人。两种都认；仍拒绝别人的消息。
    """
    columns = {row[1] for row in con.execute("pragma table_xinfo(messages)")}
    if "sender_id" not in columns:
        raise SenderEvidenceUnavailable("sender_id_unavailable")
    own = _own_id(path)
    if own:
        return "(sender_id='self' OR sender_id='%s')" % own.replace("'", "''")
    return "sender_id='self'"


def matches(path, msg_id, chat, text):
    """编号唯一、会话/正文逐字一致，并有明确自发来源。"""
    with _read(path) as con:
        _self_predicate(con, path)
        rows = con.execute("select content,sender_id from messages where cast(id as text)=? and chat_id=? limit 2",
                           (str(msg_id), str(chat))).fetchall()
    if not rows:
        return False
    if len(rows) == 1 and not str(rows[0][1] or "").strip():
        raise SenderEvidenceUnavailable("message_sender_unknown")
    own = _own_id(path)
    is_self = rows[0][1] == "self" or bool(own and str(rows[0][1] or "") == own)
    return len(rows) == 1 and str(rows[0][0] or "") == text and is_self


def observe(path, chat, text, after, *, source_token=None):
    if path is None or after is None or not chat or not text:
        return ""
    expected = None
    if source_token is not None:
        try:
            expected = json.loads(source_token)
        except (ValueError, TypeError):
            raise ObservationSourceChanged("source_snapshot_missing") from None
        if not isinstance(expected, list) or len(expected) not in (3, 4) or expected[1] != int(after) or _file_identity(path) != expected[0]:
            raise ObservationSourceChanged("source_snapshot_mismatch")
    with _read(path) as con:
        con.execute("begin")
        if expected is not None and (_boundary(con, after) != expected[2] or con.execute("select coalesce(max(id),0) from messages").fetchone()[0] < int(after)):
            raise ObservationSourceChanged("source_boundary_changed")
        predicate = _self_predicate(con, path)
        if expected is not None and len(expected) == 4:
            cutoff = expected[3]
            # 2026-11-16: backfilled rows get synced_at=epoch (db/sync_service.py:794-797),
            # so a synced_at cursor misses the message we just sent; match on message time only.
            rows = con.execute(
                "select id from messages where chat_id=? and " + predicate
                + " and content=? and created_at>=? order by created_at, id limit 2",
                (chat, text, cutoff),
            ).fetchall()
        else:
            # legacy id cursor: after backfill/cleanup max(id) can be at or below it, so the
            # cursor can never match; say "cannot verify" instead of spinning five rounds.
            if int(after) >= int(con.execute("select coalesce(max(id),0) from messages").fetchone()[0]):
                return ""
            rows = con.execute("select id from messages where id>? and chat_id=? and " + predicate + " and content=? order by id limit 2",
                               (int(after), chat, text)).fetchall()
    if expected is not None and _file_identity(path) != expected[0]:
        raise ObservationSourceChanged("source_replaced_during_observation")
    # 多个候选不选最新一条；交给后续可核对的操作关联。
    return str(rows[0][0]) if len(rows) == 1 else ""
