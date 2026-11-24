"""SQLite-backed append-only terminal event stream (Phase 2).

The in-memory/JSONL debug store remains for the Phase 0
path.  This store is the formal fact layer: every message, reply, action,
tool call and owner-visible thought is appended once to ``terminal_events`` and
read back through the same deterministic visibility rules.

Design constraints:

* append-only; no update/delete API for callers (``clear`` exists for tests and
  requires an explicit ``allow_clear=True`` -- U-00).
  The single controlled exception is :meth:`TerminalEventStore.link_event`: after
  a command is recorded the audit link columns (``command_id`` /
  ``authorization_id`` / ``turn_id`` / ``parent_event_id``) are filled in on the
  row that was already appended.  Content, ``sequence`` and visibility are never
  rewritten;
* ``sequence`` 由**库内原子取号**（同一条 INSERT 里算 ``MAX(sequence)+1``）：没有
  进程内计数器，多个 store 实例 / 多个进程也不会重号（B6，2026-09-30）；
* JSON columns are for payload shape stability, not query hot paths;
* visibility filtering always happens through ``PrincipalContext``;
* a failed append must be caught by the caller -- the live chain treats event
  recording as best-effort and never blocks a reply on it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

import structlog
from sqlalchemy import and_, delete, func, insert, or_, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from core import features
from core.terminal.event_identity import content_sha256
from core.terminal.event_model import Event, EventType, Sensitivity
from core.terminal.visibility import event_visible
from core.terminal.principal import PrincipalContext, VisibilityScope
from safelog import warn


log = structlog.get_logger()


# ---------------------------------------------------------------------------
# task-49：``actor_person_id`` 的身份归一（唯一写入口 = :func:`_event_to_values`）
# ---------------------------------------------------------------------------
# 两列在 on 档第一次分叉，语义从此固定：
#   * ``actor_person_id``  —— meta 库 person id（"谁"）；解析不出时**保持原值**；
#   * ``actor_account_id`` —— 平台原始账号标识（"从哪个号来"）：wxid_* / QQ uin /
#     *@chatroom，任何档位都不改写，绝不允许被 person id 顶掉。
# off/shadow 档两列仍逐行相等（=旧行为），所以"相等"只在 off 下成立，别当不变量。
_ACTOR_MAP_TTL = 300.0
_ACTOR_MAP_LOCK = threading.Lock()
_ACTOR_MAP_AT = 0.0
_ACTOR_MAP_BY_ACCOUNT: dict[tuple[str, str, str], str | None] = {}
_ACTOR_MAP_BY_PLATFORM: dict[tuple[str, str], str | None] = {}
_ACTOR_MAP_OVERRIDE: dict[str, Any] | None = None
_ACTOR_COUNTERS: dict[str, int] = {}
_ACTOR_LOG_AT = 0.0
_ACTOR_LOG_SECONDS = 60.0
_PLACEHOLDER_ACTORS = frozenset({"", "unknown", "self", "system", "系统"})
_ACTOR_LOG_NAMES = {
    "unresolved": "actor_person_unresolved",
    "shadow": "actor_identity_shadow",
    "account_not_platform": "actor_identity_account_not_platform",
    "self_unmapped": "actor_self_unmapped",
}


def actor_identity_mode() -> str:
    """归一开关（tri）。读不到就按 off——开关故障绝不许改变写入形状。"""
    try:
        return features.mode(features.ACTOR_IDENTITY_NORMALIZE, features.MODE_OFF)
    except Exception:  # noqa: BLE001
        warn("core/terminal/event_store_db.py:81 Exception", err=repr(__import__("sys").exc_info()[1]))
        return features.MODE_OFF


def _load_actor_map() -> tuple[dict, dict]:
    """读 ``(platform, account_id, value) / (platform, value) -> person_id``。

    只读连接；同一个键对应两个不同 person 时记为 ``None``（歧义 ⇒ 不归一）。
    """
    by_account: dict[tuple[str, str, str], str | None] = {}
    by_platform: dict[tuple[str, str], str | None] = {}
    try:
        from core.bootstrap import get_meta_db_path

        conn = sqlite3.connect(f"file:{get_meta_db_path()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "select platform, account_id, identity_value, person_id from person_identities"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 归一失败不许影响写事件
        log.warning("actor_identity_map_failed", error=str(exc)[:160])
        return {}, {}

    for platform, account_id, identity_value, person_id in rows:
        person = str(person_id or "").strip()
        value = str(identity_value or "").strip()
        if not person or not value:
            continue
        platform = str(platform or "").strip()
        account_id = str(account_id or "").strip()
        scoped = (platform, account_id, value)
        if scoped in by_account and by_account[scoped] != person:
            by_account[scoped] = None
        else:
            by_account.setdefault(scoped, person)
        wide = (platform, value)
        if wide in by_platform and by_platform[wide] != person:
            by_platform[wide] = None
        else:
            by_platform.setdefault(wide, person)
    return by_account, by_platform


def load_actor_identity_map(*, refresh: bool = False) -> tuple[dict, dict]:
    """进程内缓存（TTL 300s）的 actor→person 映射。"""
    global _ACTOR_MAP_AT
    if _ACTOR_MAP_OVERRIDE is not None:
        return (
            dict(_ACTOR_MAP_OVERRIDE.get("by_account") or {}),
            dict(_ACTOR_MAP_OVERRIDE.get("by_platform") or {}),
        )
    now = time.monotonic()
    if not refresh and _ACTOR_MAP_AT and now - _ACTOR_MAP_AT < _ACTOR_MAP_TTL:
        return _ACTOR_MAP_BY_ACCOUNT, _ACTOR_MAP_BY_PLATFORM
    with _ACTOR_MAP_LOCK:
        if not refresh and _ACTOR_MAP_AT and now - _ACTOR_MAP_AT < _ACTOR_MAP_TTL:
            return _ACTOR_MAP_BY_ACCOUNT, _ACTOR_MAP_BY_PLATFORM
        by_account, by_platform = _load_actor_map()
        _ACTOR_MAP_BY_ACCOUNT.clear()
        _ACTOR_MAP_BY_ACCOUNT.update(by_account)
        _ACTOR_MAP_BY_PLATFORM.clear()
        _ACTOR_MAP_BY_PLATFORM.update(by_platform)
        _ACTOR_MAP_AT = now
    return _ACTOR_MAP_BY_ACCOUNT, _ACTOR_MAP_BY_PLATFORM


def resolve_actor_person_id(platform: str, account_id: str, raw_id: str) -> str | None:
    """平台号 → person id；解析不出（占位符 / 无行 / 歧义）返回 ``None``。"""
    raw = str(raw_id or "").strip()
    if not raw or raw.startswith("unresolved:") or raw.lower() in _PLACEHOLDER_ACTORS:
        return None
    by_account, by_platform = load_actor_identity_map()
    key = (str(platform or ""), str(account_id or ""), raw)
    if key[1]:
        person = by_account.get(key)
        if person:
            return person
        if key in by_account:  # 一值多人：不猜
            return None
    return by_platform.get((key[0], raw)) or None


def override_actor_identity_map(
    by_account: Mapping | None, by_platform: Mapping | None = None
) -> None:
    """测试注入（``None`` 恢复读库）。"""
    global _ACTOR_MAP_OVERRIDE
    if by_account is None:
        _ACTOR_MAP_OVERRIDE = None
    else:
        _ACTOR_MAP_OVERRIDE = {
            "by_account": dict(by_account),
            "by_platform": dict(by_platform or {}),
        }


def reset_actor_identity_state() -> None:
    """清缓存与计数（测试用）。"""
    global _ACTOR_MAP_AT, _ACTOR_LOG_AT
    override_actor_identity_map(None)
    with _ACTOR_MAP_LOCK:
        _ACTOR_MAP_BY_ACCOUNT.clear()
        _ACTOR_MAP_BY_PLATFORM.clear()
        _ACTOR_MAP_AT = 0.0
    _ACTOR_COUNTERS.clear()
    _ACTOR_LOG_AT = 0.0


def actor_identity_counters() -> dict[str, int]:
    """进程内计数（聚合日志用；测试也读它）。"""
    return dict(_ACTOR_COUNTERS)


def _note_actor(kind: str, event: Event, raw: str) -> None:
    """计数 + **聚合**日志（不逐行）：按 kind 累计，最多每 60s 一条。"""
    global _ACTOR_LOG_AT
    _ACTOR_COUNTERS[kind] = _ACTOR_COUNTERS.get(kind, 0) + 1
    now = time.monotonic()
    if now - _ACTOR_LOG_AT < _ACTOR_LOG_SECONDS:
        return
    _ACTOR_LOG_AT = now
    log.info(
        _ACTOR_LOG_NAMES.get(kind, "actor_identity_note"),
        platform=str(getattr(event, "platform", "") or ""),
        account_id=str(getattr(event, "bot_account_id", "") or ""),
        sample=raw[:28],
        counters=dict(_ACTOR_COUNTERS),
    )


def _is_self_event(event: Event) -> bool:
    """这条是不是鲸鲸自己发的（``metadata.is_self``，与落库列同一事实）。"""
    metadata = getattr(event, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    try:
        return bool(metadata.get("is_self"))
    except Exception:  # noqa: BLE001
        warn("core/terminal/event_store_db.py:220 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def resolve_self_person_id(platform: str, bot_account_id: str) -> str | None:
    """``self``（她自己）→ 她的 person id：bot 账号本身能**唯一**解析成人才算。

    ``self`` 是"她"，``unknown`` 是"真不知道"——两者语义不同，任何报表都不得合并。
    解析不出就保持 ``self`` 原值并计 ``actor_self_unmapped``（绝不写成 ``unknown``）。
    """
    account = str(bot_account_id or "").strip()
    if not account:
        return None
    by_account, by_platform = load_actor_identity_map()
    person = by_account.get((str(platform or ""), account, account))
    if person:
        return person
    return by_platform.get((str(platform or ""), account)) or None


def _looks_like_platform_account(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if text.startswith("wxid_") or text.endswith("@chatroom"):
        return True
    return text.isdigit()


def _actor_columns(event: Event, raw: str) -> tuple[str, str]:
    """返回两列该写的值；off 逐字旧行为（**不补**账号列，否则 505 行老数据会被改写）。"""
    account = str(getattr(event, "actor_account_id", "") or "")
    mode = actor_identity_mode()
    if mode == features.MODE_OFF:
        return raw, account
    platform = str(getattr(event, "platform", "") or "")
    bot = str(getattr(event, "bot_account_id", "") or "")
    if raw == "self" and _is_self_event(event):
        self_person = resolve_self_person_id(platform, bot)
        if mode != features.MODE_ON:
            if self_person is not None:
                _note_actor("shadow", event, raw)
            return raw, account
        if not self_person:
            _note_actor("self_unmapped", event, raw)
            return raw, account
        self_account = account if account and account.lower() not in _PLACEHOLDER_ACTORS else bot
        if not _looks_like_platform_account(self_account):
            _note_actor("account_not_platform", event, self_account)
        return self_person, self_account
    person = resolve_actor_person_id(platform, bot, raw)
    if mode != features.MODE_ON:
        if person is not None:
            _note_actor("shadow", event, raw)
        return raw, account
    account = account or raw
    if not _looks_like_platform_account(account):
        _note_actor("account_not_platform", event, account)
    if person is None:
        _note_actor("unresolved", event, raw)
        return raw, account
    return person, account


def _is_sqlite_cantopen(exc: BaseException) -> bool:
    """SQLite CANTOPEN（``unable to open database file``）文本判据。"""
    return "unable to open database file" in str(exc)


# SQLITE_CANTOPEN（``unable to open database file``）重试预算：Windows 上外部进程
# （杀毒扫描、备份/同步、以及被替换掉的 WAL/-shm）会让**新建的连接**也短暂打不开
# 库文件，单次重试不够；退避 3 次后仍失败才认输（失败即事件丢失，见
# docs/decisions/运维_TEMP不可写导致事件丢失.md）。
_CANTOPEN_MAX_RETRIES = 3
_CANTOPEN_BACKOFF = (0.05, 0.2, 0.5)


def _cantopen_diagnostics(engine: Any) -> dict:
    """CANTOPEN 时要一次性记全「到底哪个文件打不开」。

    为什么需要：``unable to open database file`` 是 SQLite 对**任何**打不开的文件
    （主库 / WAL / SHM / 临时文件）的统一说法，光看这句话永远分不清是盘、权限、
    路径还是临时目录。把 URL 解析出的路径、进程 cwd、文件存在性、WAL/SHM 状态一起
    记下来，下一次突发就能直接定位，不用再猜。
    """
    info: dict[str, Any] = {}
    try:
        url = getattr(getattr(engine, "url", None), "database", "") or ""
        info["db_path"] = str(url)
    except Exception:  # noqa: BLE001
        warn("core/terminal/event_store_db.py:309 Exception", err=repr(__import__("sys").exc_info()[1]))
        info["db_path"] = ""
    try:
        info["cwd"] = os.getcwd()
    except OSError as exc:  # cwd 本身失效时 getcwd 就会抛
        warn("core/terminal/event_store_db.py:313 OSError", err=repr(exc))
        info["cwd"] = "<cwd 不可用: %s>" % str(exc)[:60]
    path = str(info.get("db_path") or "")
    # 键形状固定：没有可解析路径时也给出 False，否则日志字段随场景漂移，
    # 下游（脚本/告警）没法稳定取值。
    info.setdefault("db_exists", False)
    info.setdefault("db_writable", False)
    info.setdefault("db_size", 0)
    info.setdefault("wal_exists", False)
    info.setdefault("shm_exists", False)
    info.setdefault("dir_writable", False)
    if path:
        try:
            info["db_exists"] = os.path.exists(path)
            info["db_writable"] = os.access(path, os.W_OK)
            info["db_size"] = os.path.getsize(path) if info["db_exists"] else 0
            info["wal_exists"] = os.path.exists(path + "-wal")
            info["shm_exists"] = os.path.exists(path + "-shm")
            info["dir_writable"] = os.access(os.path.dirname(path) or ".", os.W_OK)
        except OSError as exc:  # noqa: BLE001
            warn("core/terminal/event_store_db.py:332 OSError", err=repr(exc))
            info["stat_error"] = str(exc)[:80]
    try:
        info["sqlite_version"] = sqlite3.sqlite_version
    except Exception:  # noqa: BLE001
        warn("core/terminal/event_store_db.py:336 Exception", err=repr(__import__("sys").exc_info()[1]))
    return info


def _prefer_memory_temp(session: Session) -> None:
    """把本连接的 SQLite 临时存储钉到内存（``temp_store=MEMORY``）。

    为什么：默认 ``temp_store=FILE`` 时，需要临时 B 树/排序中间结果的语句会在系统
    临时目录建文件；调度器进程的 ``TEMP`` 一旦不可用，SQLite 只报
    ``unable to open database file``。内存模式让这类语句完全不碰磁盘临时目录。
    语句级 PRAGMA，失败只记一次 debug，绝不让它挡住写入。
    """
    try:
        session.execute(text("PRAGMA temp_store=MEMORY"))
    except Exception as exc:  # noqa: BLE001 - 只是优化，不允许影响落库
        log.debug("event_store_temp_store_pragma_failed", error=str(exc)[:120])


def _utcnow() -> datetime:
    """默认事件时间：本地墙钟（naive），与采集侧一致（2026-09-22 统一）。"""
    return datetime.now()


def _as_json(value: Any, fallback: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        warn("core/terminal/event_store_db.py:363 Exception", err=repr(__import__("sys").exc_info()[1]))
        return json.dumps(fallback, ensure_ascii=False)


def _load_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        warn("core/terminal/event_store_db.py:374 Exception", err=repr(__import__("sys").exc_info()[1]))
        return fallback


_SPAN_COLUMNS = ("content_start", "content_end", "media_span_json")
_SPAN_EVENT_TYPES = frozenset({"message", "question", "request", "reply"})
_SPAN_SUPPORT: dict = {}


def _span_columns_available(session: Any) -> bool:
    """terminal_events 上 span 三列是否已迁移（按 bind 缓存；缺列时本轮写入跳过三键）。

    老形状 DB（未跑 m061）不写三键、其余列逐字不变；探测失败按「没有列」处理，
    只告警一次，绝不因为写 span 让事件落库失败。
    """
    try:
        bind = session.get_bind()
    except Exception:
        warn("core/terminal/event_store_db.py:391 Exception", err=repr(__import__("sys").exc_info()[1]))
        bind = None
    key = bind if bind is not None else "terminal_events"
    cached = _SPAN_SUPPORT.get(key)
    if cached is not None:
        return bool(cached)
    available = False
    try:
        from sqlalchemy import text

        rows = session.execute(text("PRAGMA table_info(terminal_events)")).fetchall()
        columns = {str(row[1]) for row in rows if len(row) > 1}
        available = set(_SPAN_COLUMNS) <= columns
    except Exception as exc:
        available = False
        log.warning("terminal_span_columns_probe_failed", error=str(exc)[:200])
    _SPAN_SUPPORT[key] = available
    if not available:
        log.warning("terminal_span_columns_missing", columns=list(_SPAN_COLUMNS))
    return available


def _media_spans(content: str) -> list:
    """内容里每个媒体占位符的原样区间（写时只记录位置，不解释语义）。"""
    if not content:
        return []
    try:
        from core.group_facts import MEDIA_PLACEHOLDER_KINDS, _MEDIA_MARKER_RE
    except Exception:
        warn("core/terminal/event_store_db.py:419 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
    spans = []
    for match in _MEDIA_MARKER_RE.finditer(content):
        raw = match.group(0)
        kind = ""
        for prefix, marker_kind in MEDIA_PLACEHOLDER_KINDS:
            if raw.startswith(prefix):
                kind = str(getattr(marker_kind, "value", marker_kind))
                break
        spans.append(
            {"kind": kind or "unknown", "start": match.start(), "end": match.end(), "raw": raw}
        )
    return spans


def _span_values(event: Event, span_ok: bool) -> dict:
    """span 三键（-1 = 未知，与 EvidenceRef 同一约定）。span_ok=False 时返回空 dict。"""
    if not span_ok:
        return {}
    event_type = str(getattr(event, "event_type", "") or "")
    content = str(getattr(event, "content", "") or "")
    media = _media_spans(content) if event_type in _SPAN_EVENT_TYPES else []
    return {
        "content_start": 0 if content else -1,
        "content_end": len(content) if content else -1,
        "media_span_json": _as_json(media, []),
    }


def _metadata_with_content_hash(event: Event) -> dict[str, Any]:
    """metadata + ``content_sha256``（感知层.md §10-4）。

    为什么不新开一列：这一列不参与索引、不参与幂等键（内容进键会把「转发同一句
    话」吃掉），它只在事后关联时被读一次。放进已经存在的 metadata 里，就不必为了
    一个派生值动表结构（迁移只许新增，但没必要的列就是没必要的债）。

    为什么在**唯一写入口**算：写在落屏处，任何写路径都自动带上；写在读取处，则
    每条读路径都要各自记得算一次（迟早有一条忘掉）。
    """
    meta = dict(event.metadata or {})
    if "content_sha256" not in meta:
        digest = content_sha256(str(event.content or ""))
        if digest:
            meta["content_sha256"] = digest
    return meta


def _event_to_values(event: Event, *, span_ok: bool = False) -> dict[str, Any]:
    """事件 → 行值；``actor_person_id`` 的身份归一只在这里发生（task-49）。

    off 档逐字旧行为（两列都写平台号，``actor_account_id`` 空就空）；on 档
    ``actor_person_id`` 写 person id、``actor_account_id`` 补平台号并原样保留。
    """
    actor_raw = str(event.actor_person_id or "")
    actor_value, actor_account = _actor_columns(event, actor_raw)
    return {
        "event_id": event.event_id or uuid.uuid4().hex,
        "occurred_at": event.occurred_at or _utcnow(),
        "sequence": int(event.sequence or 0),
        "platform": str(event.platform or ""),
        "bot_account_id": str(event.bot_account_id or ""),
        "chat_id": str(event.chat_id or ""),
        "chat_type": str(event.chat_type or ""),
        "actor_person_id": actor_value,
        "actor_account_id": actor_account,
        "recipient_person_ids_json": _as_json(list(event.recipient_person_ids), []),
        "event_type": event.event_type.value if isinstance(event.event_type, EventType) else str(event.event_type),
        "content": str(event.content or ""),
        "action_payload_json": _as_json(dict(event.action_payload or {}), {}),
        "command_id": str(event.command_id or ""),
        "authorization_id": str(event.authorization_id or ""),
        "parent_event_id": str(event.parent_event_id or ""),
        "turn_id": str(getattr(event, "turn_id", "") or ""),
        "external_msg_id": str(getattr(event, "external_msg_id", "") or ""),
        "link_revision": int(event.link_revision or 0),
        "visibility_scope_json": _as_json(
            sorted(scope.value for scope in event.visibility_scope), ["public"]
        ),
        "sensitivity": (
            event.sensitivity.value
            if isinstance(event.sensitivity, Sensitivity)
            else str(event.sensitivity or Sensitivity.NORMAL.value)
        ),
        "metadata_json": _as_json(_metadata_with_content_hash(event), {}),
        **_span_values(event, span_ok),
    }


def _metadata_from_row(row: Any) -> dict[str, Any]:
    """读回 metadata，**剥掉** ``content_sha256``。

    库里那一列在（``metadata_json`` 里，供 SQL 直接关联去重），但不进 ``Event``：
    ``Event`` 装的是「外部发生了什么」这个事实，哈希是从正文派生的索引值；派生态
    混进事实对象，会让「读回来的和写进去的不相等」这种怪事粘到每一条比较上
    （真发生过：一条 end-to-end 用例因为多这个键而 assert 不过）。

    要哈希的地方调 :func:`core.terminal.event_identity.content_sha256_of`，别从
    ``Event.metadata`` 里翻——那是「谁都能改的旁注」。
    """
    meta = dict(_load_json(getattr(row, "metadata_json", "{}"), {}) or {})
    meta.pop("content_sha256", None)
    return meta


def _event_from_row(row: Any) -> Event:
    occurred_at = getattr(row, "occurred_at", None)
    if not isinstance(occurred_at, datetime):
        occurred_at = _utcnow()
    event_type_raw = str(getattr(row, "event_type", "") or EventType.MESSAGE.value)
    try:
        event_type = EventType(event_type_raw)
    except ValueError:
        warn("core/terminal/event_store_db.py:531 ValueError", err=repr(__import__("sys").exc_info()[1]))
        event_type = EventType.MESSAGE
    scopes = _load_json(getattr(row, "visibility_scope_json", "[]"), [])
    if isinstance(scopes, str):
        scopes = [scopes]
    try:
        visibility = frozenset(VisibilityScope(str(scope)) for scope in scopes)
    except ValueError:
        warn("core/terminal/event_store_db.py:538 ValueError", err=repr(__import__("sys").exc_info()[1]))
        visibility = frozenset({VisibilityScope.PUBLIC})
    sensitivity_raw = str(
        getattr(row, "sensitivity", "") or Sensitivity.NORMAL.value
    )
    try:
        sensitivity = Sensitivity(sensitivity_raw)
    except ValueError:
        warn("core/terminal/event_store_db.py:545 ValueError", err=repr(__import__("sys").exc_info()[1]))
        sensitivity = Sensitivity.NORMAL
    recipients = _load_json(getattr(row, "recipient_person_ids_json", "[]"), [])
    if not isinstance(recipients, (list, tuple)):
        recipients = []
    return Event(
        event_id=str(getattr(row, "event_id", "") or ""),
        occurred_at=occurred_at,
        sequence=int(getattr(row, "sequence", 0) or 0),
        platform=str(getattr(row, "platform", "") or ""),
        bot_account_id=str(getattr(row, "bot_account_id", "") or ""),
        chat_id=str(getattr(row, "chat_id", "") or ""),
        chat_type=str(getattr(row, "chat_type", "") or ""),
        actor_person_id=str(getattr(row, "actor_person_id", "") or ""),
        actor_account_id=str(getattr(row, "actor_account_id", "") or ""),
        recipient_person_ids=tuple(str(value) for value in recipients),
        event_type=event_type,
        content=str(getattr(row, "content", "") or ""),
        action_payload=dict(
            _load_json(getattr(row, "action_payload_json", "{}"), {}) or {}
        ),
        command_id=str(getattr(row, "command_id", "") or ""),
        authorization_id=str(getattr(row, "authorization_id", "") or ""),
        parent_event_id=str(getattr(row, "parent_event_id", "") or ""),
        turn_id=str(getattr(row, "turn_id", "") or ""),
        external_msg_id=str(getattr(row, "external_msg_id", "") or ""),
        link_revision=int(getattr(row, "link_revision", 0) or 0),
        visibility_scope=visibility,
        sensitivity=sensitivity,
        metadata=_metadata_from_row(row),
    )


def _next_sequence_expression(model: Any) -> Any:
    """库内原子取号表达式：``MAX(sequence)+1``（放进 INSERT 同一条语句里）。

    为什么不用进程内计数器 / 构造期读一次 max：那两处都是「读旧值 + 各自 +1」，
    多实例（多进程、或同进程里两个 store）必然重号（存量 8 个重号就是这么来的）。
    放进 INSERT 语句内部后，MAX 是在**该语句自己的写事务**里求值的：SQLite 同一
    时刻只允许一个写事务，后来的 append 只能等前一条提交，于是天然串行、不重号。
    ``COALESCE`` 让空表从 1 开始（``clear()`` 之后也重新从 1 起）。
    """
    return select(func.coalesce(func.max(model.sequence), 0) + 1).scalar_subquery()


# ------------------------------------------------------------- 同事务写入入口
# §7.1 ① 要求「事件 + 投影同库单事务」：调用方（``core.tasks.store``）必须在自己的
# session 里两张表一起 commit，不能走 append() 的独立 session。下面这个入口让那条
# 路径**复用 owner 的全部保护**，而不是自己抄一份 INSERT——抄一份就是等着和这里漂移
# （ENG-17 的建表漂移、A §4.1 的第二写方各自少几道保护，都是这个味道）。
def append_in_session(session: Session, event: Event) -> Event:
    """在**调用方传入的 session** 里追加一条事件行（同事务），走 ``append`` 的保护。

    三道保护与 :meth:`TerminalEventStore.append` 是**同一份实现**（``append`` 现在
    也调这里，不存在第二份）：

    1. **幂等提前返回**：``event_id`` 已在库里 → 不再 INSERT，返回传入事件并补上
       已存行的真实 ``sequence``——稳定 id 之后，重放会走到这里；不能把传入对象的
       ``sequence=0`` 交出去，那正是本函数第 3 条要拦的假号；
    2. **``external_msg_id`` 四元组回查**：``(platform, bot_account_id, chat_id,
       external_msg_id)`` 命中已存行 → 返回**已存的那一行**（含它真实的
       ``sequence``）：同一外部消息重放不插第二行，也不会拿到假号；
    3. **``RETURNING sequence`` 非空校验**：落行却拿不到号 = 事实层不完整，抛
       ``RuntimeError``——绝不把 ``sequence=0`` 发给调用方（重号 / 漏投影都从假号
       开始）。取号仍在 INSERT 语句内部（``MAX(sequence)+1``，B6）。

    事务归调用方：本函数**不 commit**（这正是它存在的原因）。并发下唯一键冲突会抛
    ``IntegrityError``，由调用方按自己的事务语义处理（``append()`` 回查已存行；台账
    路径让整个事务回滚——台账行与事件行同生共死，正是 §7.1 ① 要的）。
    """
    from db.meta_models import TerminalEvent

    if not event.event_id:
        from core.contracts.events import observation_event_id

        event.event_id = observation_event_id(
            platform=str(event.platform or ""),
            account_id=str(event.bot_account_id or ""),
            chat_id=str(event.chat_id or ""),
            external_id=str(event.external_msg_id or ""),
        ) or uuid.uuid4().hex
    span_ok = _span_columns_available(session)
    values = _event_to_values(event, span_ok=span_ok)
    external_key = values["external_msg_id"]
    if external_key:
        if span_ok:
            stored_row = session.execute(
                select(TerminalEvent).where(
                    TerminalEvent.platform == values["platform"],
                    TerminalEvent.bot_account_id == values["bot_account_id"],
                    TerminalEvent.chat_id == values["chat_id"],
                    TerminalEvent.external_msg_id == external_key,
                )
            ).scalars().first()
            if stored_row is not None:
                # Idempotent replay: the same external message is already in the
                # stream.  Return the stored winner so callers link to a real
                # event_id (and to its real sequence).
                return _event_from_row(stored_row)
        else:
            stored = session.execute(
                select(TerminalEvent.id, TerminalEvent.sequence).where(
                    TerminalEvent.platform == values["platform"],
                    TerminalEvent.bot_account_id == values["bot_account_id"],
                    TerminalEvent.chat_id == values["chat_id"],
                    TerminalEvent.external_msg_id == external_key,
                )
            ).first()
            if stored is not None:
                event.sequence = int(stored.sequence or 0)
                return event
    existing = session.execute(
        select(TerminalEvent.id, TerminalEvent.sequence).where(
            TerminalEvent.event_id == values["event_id"]
        )
    ).first()
    if existing is not None:
        # 同 id = 同一条观察（稳定 id 之后，重放走这里）。补上已存行的真实
        # sequence，不能把传入对象的 0 交出去。
        event.sequence = int(existing.sequence or 0)
        return event
    payload = dict(values)
    if not payload.get("sequence"):
        payload["sequence"] = _next_sequence_expression(TerminalEvent)
    assigned = session.execute(
        insert(TerminalEvent)
        .values(**payload)
        .returning(TerminalEvent.sequence)
    ).scalar()
    if assigned is None:
        # 落行成功却没有号 = 事实层不完整；宁可报错，也不给调用方一个假的 0。
        raise RuntimeError("terminal_events: INSERT 未返回 sequence")
    event.sequence = int(assigned)
    return event


class ClearForbiddenError(RuntimeError):
    """没有 ``allow_clear=True`` 就清空账本：拒绝（U-00，红线 R1）。"""


class TerminalEventStore:
    """Append-only SQLite event store shared by the whole terminal."""

    def __init__(self, engine: Any):
        self.engine = engine
        self._lock = threading.RLock()
        # 构造期**不再**读 max(sequence)：旧的 `_read_latest_sequence()` 读失败会
        # 静默 return 0（等于把重号风险藏起来）。现在没有计数器可读，取号只在
        # append 的 INSERT 里发生，读失败 = INSERT 失败 = 向上抛，调用方看得见。

    # ---------------------------------------------------------------- append
    def _append_once(self, event: Event) -> Event:
        """一次投递（失败后不复用那条会话）；幂等保护都在 append_in_session 里。"""
        with Session(self.engine) as session:
            _prefer_memory_temp(session)
            stored = append_in_session(session, event)
            if stored is not event:
                # Idempotent replay: the same external message is
                # already in the stream.  Return the stored winner
                # so callers link to a real event_id.
                return stored
            session.commit()
        return event

    def append(self, event: Event) -> Event:
        """落一条事件；``SQLITE_CANTOPEN`` 带退避重试，其余错误原样上抛。

        CANTOPEN 是**连接级**故障（库文件/其 WAL/-shm 被外部进程短暂占住或替换，
        Windows 上杀毒扫描、备份同步都会造成），坏连接会被连接池反复取用，所以每
        轮都 ``dispose()`` 换新连接。``append_in_session`` 的幂等保护
        （``event_id`` / ``external_msg_id`` 回查）保证重试不写出重复行。
        重试预算 ``_CANTOPEN_MAX_RETRIES``；仍失败才抛——调用方会记
        ``terminal_event_record_failed``，那一条事件就丢了，所以预算不能是 1。
        """
        with self._lock:
            attempt = 0
            while True:
                try:
                    return self._append_once(event)
                except OperationalError as exc:
                    if not _is_sqlite_cantopen(exc):
                        raise
                    attempt += 1
                    log.warning(
                        "terminal_event_store_reconnect",
                        error=str(exc)[:200],
                        attempt=attempt,
                        **_cantopen_diagnostics(self.engine),
                    )
                    dispose = getattr(self.engine, "dispose", None)
                    if dispose is not None:
                        dispose()
                    if attempt > _CANTOPEN_MAX_RETRIES:
                        raise
                    time.sleep(_CANTOPEN_BACKOFF[min(attempt - 1, len(_CANTOPEN_BACKOFF) - 1)])
                except IntegrityError as exc:
                    # A concurrent writer won the race on a unique key (event_id
                    # or external_msg_id).  Appends stay idempotent: return the
                    # stored row when we can find it.
                    values = _event_to_values(event)
                    stored = None
                    if values["external_msg_id"]:
                        stored = self.get_by_external_msg_id(
                            platform=values["platform"],
                            bot_account_id=values["bot_account_id"],
                            chat_id=values["chat_id"],
                            external_msg_id=values["external_msg_id"],
                        )
                    if stored is None:
                        stored = self.get(values["event_id"])
                    if stored is not None:
                        return stored
                    # 冲突了却回查不到「已存的那一行」= 事实层状态不明：绝不把手里
                    # 这条（sequence 还是 0）当作落库成功返回，那等于发给调用方一个
                    # 假号（投影用 0 推进游标、对账看成重号都从假号开始）。
                    raise RuntimeError(
                        "terminal_events: 唯一键冲突但回查不到已存行，拒绝返回未落库事件"
                    ) from exc

    def append_many(self, events: Iterable[Event]) -> int:
        count = 0
        for event in events:
            self.append(event)
            count += 1
        return count

    # ------------------------------------------------------------------ link
    def link_event(
        self,
        event_id: str,
        *,
        command_id: str = "",
        authorization_id: str = "",
        turn_id: str = "",
        parent_event_id: str = "",
    ) -> bool:
        """把链接列回填到已 append 的事件行上（append-only 的**受控例外**）。

        为什么允许改写：命令/授权/回合这三条链是在事件行写入**之后**才确定的
        （消息先 append → 命令投影落库 → 授权或执行回填；回复的父事件 id 也要
        先解析），不改这几列就只能把链断在库里。纪律：只写传入的非空值，只碰
        ``command_id`` / ``authorization_id`` / ``turn_id`` / ``parent_event_id``
        四列，**绝不改** content / sequence / visibility / 事件类型；同值不写
        （幂等）。失败向上抛，调用方用 ``link_event_best_effort`` 兜底。
        """
        updates = {
            "command_id": str(command_id or ""),
            "authorization_id": str(authorization_id or ""),
            "turn_id": str(turn_id or ""),
            "parent_event_id": str(parent_event_id or ""),
        }
        targets = {key: value for key, value in updates.items() if value}
        if not str(event_id or "") or not targets:
            return False
        from db.meta_models import TerminalEvent

        with self._lock:
            with Session(self.engine) as session:
                row = session.execute(
                    select(TerminalEvent).where(
                        TerminalEvent.event_id == str(event_id)
                    )
                ).scalars().first()
                if row is None:
                    return False
                changed = False
                for key, value in targets.items():
                    if str(getattr(row, key, "") or "") != value:
                        setattr(row, key, value)
                        changed = True
                if changed:
                    session.commit()
            return changed

    # ---------------------------------------------------------------- query
    def link_external_msg_id(self, event_id: str, *, external_msg_id: str) -> bool:
        """把同步回声的外部 msg_id 回填到已 append 的 reply 事件行（受控例外）。

        与 link_event 同一纪律：只写 external_msg_id 这一列（空才写），绝不改
        content / sequence / 事件类型；幂等；失败向上抛。
        """
        if not str(event_id or "") or not str(external_msg_id or ""):
            return False
        from db.meta_models import TerminalEvent

        with self._lock:
            with Session(self.engine) as session:
                row = session.execute(
                    select(TerminalEvent).where(
                        TerminalEvent.event_id == str(event_id)
                    )
                ).scalars().first()
                if row is None:
                    return False
                if str(getattr(row, "external_msg_id", "") or ""):
                    return False
                row.external_msg_id = str(external_msg_id)
                session.commit()
                return True

    def all(self) -> tuple[Event, ...]:
        return tuple(self.query(limit=None))

    def count(self) -> int:
        from db.meta_models import TerminalEvent

        try:
            with Session(self.engine) as session:
                return int(session.execute(
                    select(func.count(TerminalEvent.id))
                ).scalar() or 0)
        except Exception:
            warn("core/terminal/event_store_db.py:829 Exception", err=repr(__import__("sys").exc_info()[1]))
            return 0

    def sequence_rows(self) -> list[tuple[int, str]]:
        """全表 ``(sequence, event_id)``，按 ``sequence`` 升序（对账器用）。

        读失败**不吞**：对账器要靠它判断「有没有新缺口 / 重号」，静默返回空表
        等于谎报「全好」。口径与白名单见 ``core.terminal.sequence_policy``。
        """
        from db.meta_models import TerminalEvent

        with Session(self.engine) as session:
            rows = session.execute(
                select(TerminalEvent.sequence, TerminalEvent.event_id).order_by(
                    TerminalEvent.sequence.asc(), TerminalEvent.id.asc()
                )
            ).all()
        return [
            (int(sequence or 0), str(event_id or "")) for sequence, event_id in rows
        ]

    def sequence_row_id(self, event_id: str) -> int:
        """``event_id`` → 行主键 ``terminal_events.id``（ENG-23 复合游标后半键）。

        游标要按 ``(sequence, id)`` 推进，而 ``Event`` 不带行 id，所以投影侧拿批内
        最后一条回查一次。读失败返回 0 = 调用方退回「只按号推进」。
        """
        from db.meta_models import TerminalEvent

        if not event_id:
            return 0
        try:
            with Session(self.engine) as session:
                row_id = session.execute(
                    select(TerminalEvent.id).where(
                        TerminalEvent.event_id == str(event_id)
                    )
                ).scalar()
            return max(0, int(row_id or 0))
        except Exception:
            warn("core/terminal/event_store_db.py:868 Exception", err=repr(__import__("sys").exc_info()[1]))
            return 0

    def get(self, event_id: str) -> Event | None:
        from db.meta_models import TerminalEvent

        if not event_id:
            return None
        try:
            with Session(self.engine) as session:
                row = session.execute(
                    select(TerminalEvent).where(TerminalEvent.event_id == event_id)
                ).scalars().first()
            return _event_from_row(row) if row is not None else None
        except Exception:
            warn("core/terminal/event_store_db.py:882 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None

    def get_by_external_msg_id(
        self,
        *,
        platform: str,
        bot_account_id: str,
        chat_id: str,
        external_msg_id: str,
    ) -> Event | None:
        """Look up the message event for one external message id.

        This is the in-process idempotency check used before recording; the
        partial unique index on ``external_msg_id`` is the cross-process guard.
        """
        from db.meta_models import TerminalEvent

        if not external_msg_id:
            return None
        try:
            with Session(self.engine) as session:
                row = session.execute(
                    select(TerminalEvent).where(
                        TerminalEvent.platform == str(platform),
                        TerminalEvent.bot_account_id == str(bot_account_id),
                        TerminalEvent.chat_id == str(chat_id),
                        TerminalEvent.external_msg_id == str(external_msg_id),
                    )
                ).scalars().first()
            return _event_from_row(row) if row is not None else None
        except Exception:
            warn("core/terminal/event_store_db.py:913 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None

    def revocation_state(self, event_id: str) -> str:
        """Return whether a message has a later, linked revoke event."""
        from db.meta_models import TerminalEvent

        if not str(event_id or ""):
            return "unknown"
        try:
            with Session(self.engine) as session:
                target = session.execute(
                    select(TerminalEvent).where(
                        TerminalEvent.event_id == str(event_id)
                    )
                ).scalars().first()
                if target is None:
                    return "unknown"
                direct = session.execute(
                    select(TerminalEvent.id).where(
                        TerminalEvent.parent_event_id == str(event_id),
                        TerminalEvent.event_type == EventType.REVOKE.value,
                    ).limit(1)
                ).first()
                if direct is not None:
                    return "revoked"
                external_id = str(getattr(target, "external_msg_id", "") or "")
                if external_id:
                    indirect = session.execute(
                        select(TerminalEvent.id).where(
                            TerminalEvent.platform == target.platform,
                            TerminalEvent.bot_account_id == target.bot_account_id,
                            TerminalEvent.chat_id == target.chat_id,
                            TerminalEvent.event_type == EventType.REVOKE.value,
                            func.json_extract(
                                TerminalEvent.metadata_json,
                                "$.revoke.ref_external_msg_id",
                            ) == external_id,
                        ).limit(1)
                    ).first()
                    if indirect is not None:
                        return "revoked"
                from core.terminal.revoke import (
                    choose_revoke_target,
                    parse_revoke_notice,
                )

                legacy_revokes = session.execute(
                    select(TerminalEvent).where(
                        TerminalEvent.platform == target.platform,
                        TerminalEvent.bot_account_id == target.bot_account_id,
                        TerminalEvent.chat_id == target.chat_id,
                        TerminalEvent.occurred_at >= target.occurred_at,
                        TerminalEvent.occurred_at
                        <= target.occurred_at + timedelta(seconds=120),
                        TerminalEvent.content.like("%revokemsg%"),
                    ).order_by(TerminalEvent.sequence.asc())
                ).scalars().all()
                if legacy_revokes:
                    candidate_rows = session.execute(
                        select(TerminalEvent).where(
                            TerminalEvent.platform == target.platform,
                            TerminalEvent.bot_account_id == target.bot_account_id,
                            TerminalEvent.chat_id == target.chat_id,
                            TerminalEvent.event_type.in_(
                                self._MESSAGE_EVENT_TYPES
                            ),
                            TerminalEvent.occurred_at
                            >= target.occurred_at - timedelta(seconds=120),
                            TerminalEvent.occurred_at
                            <= target.occurred_at + timedelta(seconds=120),
                        ).order_by(
                            TerminalEvent.occurred_at.asc(),
                            TerminalEvent.sequence.asc(),
                        )
                    ).scalars().all()
                    candidate_events = [
                        _event_from_row(row) for row in candidate_rows
                    ]
                    target_external_id = str(
                        getattr(target, "external_msg_id", "") or ""
                    )
                    unresolved_revoke = False
                    for revoke_row in legacy_revokes:
                        notice = parse_revoke_notice(
                            str(getattr(revoke_row, "content", "") or "")
                        )
                        if notice is None:
                            unresolved_revoke = True
                            continue
                        reference = str(
                            notice.reference_external_msg_id or ""
                        ).strip()
                        if reference:
                            if target_external_id and reference == target_external_id:
                                return "revoked"
                            continue
                        chosen = choose_revoke_target(
                            notice,
                            revoke_row.occurred_at,
                            candidate_events,
                        )
                        if (
                            chosen is not None
                            and chosen.event_id == target.event_id
                        ):
                            return "revoked"
                        if chosen is None:
                            unresolved_revoke = True
                    if unresolved_revoke:
                        return "unknown"
            return "active"
        except Exception as exc:
            warn(
                "terminal_revocation_state_failed",
                event_id=str(event_id)[:80],
                err=repr(exc)[:120],
            )
            return "unknown"

    _MESSAGE_EVENT_TYPES = ("message", "question", "request", "command")

    def recorded_message_event_ids(
        self, *, platform: str, bot_account_id: str
    ) -> dict[tuple[str, str], str]:
        """Like ``recorded_message_keys`` but maps ``key -> event_id``.

        Rows written before Migration 46 (or by a still-running old process)
        carry an empty ``external_msg_id`` but keep the id in
        ``metadata_json.msg_id``; those fall back to the JSON so the forwarder
        / reconciler never mistake an existing event for a missing one.
        """
        from db.meta_models import TerminalEvent

        with Session(self.engine) as session:
            rows = session.execute(
                select(
                    TerminalEvent.chat_id,
                    TerminalEvent.external_msg_id,
                    TerminalEvent.metadata_json,
                    TerminalEvent.event_type,
                    TerminalEvent.event_id,
                ).where(
                    TerminalEvent.platform == str(platform or ""),
                    TerminalEvent.bot_account_id == str(bot_account_id or ""),
                )
            ).all()
        out: dict[tuple[str, str], str] = {}
        for chat_id, external_msg_id, metadata_json, event_type, event_id in rows:
            chat = str(chat_id or "")
            if external_msg_id:
                key = (chat, str(external_msg_id))
            elif str(event_type or "") in self._MESSAGE_EVENT_TYPES:
                metadata = _load_json(metadata_json, {})
                if not isinstance(metadata, dict):
                    continue
                msg_id = str(metadata.get("msg_id") or "")
                if not msg_id:
                    continue
                key = (chat, msg_id)
            else:
                continue
            out.setdefault(key, str(event_id or ""))
        return out

    def recorded_message_keys(
        self, *, platform: str, bot_account_id: str
    ) -> set[tuple[str, str]]:
        """``recorded_message_event_ids`` 的键集合。"""
        return set(
            self.recorded_message_event_ids(
                platform=platform, bot_account_id=bot_account_id
            )
        )

    def backfill_external_msg_ids(
        self, *, platform: str, bot_account_id: str
    ) -> int:
        """把旧代码遗留的空 ``external_msg_id`` 从 metadata_json 补回。

        只处理消息类事件；键已被别的行占用的行跳过（留给对账报告暴露）。
        """
        from db.meta_models import TerminalEvent

        platform_key = str(platform or "")
        account_key = str(bot_account_id or "")
        updated = 0
        with Session(self.engine) as session:
            rows = session.execute(
                select(TerminalEvent).where(
                    TerminalEvent.platform == platform_key,
                    TerminalEvent.bot_account_id == account_key,
                    or_(
                        TerminalEvent.external_msg_id == "",
                        TerminalEvent.external_msg_id.is_(None),
                    ),
                    TerminalEvent.event_type.in_(list(self._MESSAGE_EVENT_TYPES)),
                )
            ).scalars().all()
            for row in rows:
                metadata = _load_json(getattr(row, "metadata_json", "{}"), {})
                if not isinstance(metadata, dict):
                    continue
                msg_id = str(metadata.get("msg_id") or "")
                if not msg_id:
                    continue
                clash = session.execute(
                    select(TerminalEvent.id).where(
                        TerminalEvent.platform == platform_key,
                        TerminalEvent.bot_account_id == account_key,
                        TerminalEvent.chat_id == row.chat_id,
                        TerminalEvent.external_msg_id == msg_id,
                    )
                ).scalar()
                if clash is not None:
                    continue
                row.external_msg_id = msg_id
                updated += 1
            session.commit()
        return updated

    def query(
        self,
        *,
        event_types: Iterable[EventType] | None = None,
        person_id: str = "",
        chat_id: str = "",
        command_id: str = "",
        platform: str = "",
        bot_account_id: str = "",
        limit: int | None = None,
    ) -> list[Event]:
        from db.meta_models import TerminalEvent

        wanted = {e.value for e in event_types} if event_types else None
        try:
            with Session(self.engine) as session:
                statement = select(TerminalEvent)
                if wanted:
                    statement = statement.where(
                        TerminalEvent.event_type.in_(sorted(wanted))
                    )
                if person_id:
                    statement = statement.where(
                        TerminalEvent.actor_person_id == str(person_id)
                    )
                if chat_id:
                    statement = statement.where(TerminalEvent.chat_id == str(chat_id))
                if command_id:
                    statement = statement.where(
                        TerminalEvent.command_id == str(command_id)
                    )
                if platform:
                    statement = statement.where(TerminalEvent.platform == str(platform))
                if bot_account_id:
                    statement = statement.where(
                        TerminalEvent.bot_account_id == str(bot_account_id)
                    )
                if limit is not None:
                    statement = (
                        statement.order_by(
                            TerminalEvent.sequence.desc(), TerminalEvent.id.desc()
                        ).limit(max(1, int(limit)))
                    )
                    rows = list(session.execute(statement).scalars().all())
                    rows.reverse()
                else:
                    statement = statement.order_by(
                        TerminalEvent.sequence.asc(), TerminalEvent.id.asc()
                    )
                    rows = list(session.execute(statement).scalars().all())
            return [_event_from_row(row) for row in rows]
        except Exception:
            warn("core/terminal/event_store_db.py:1068 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []

    def recent(self, limit: int = 200) -> list[Event]:
        return self.query(limit=max(1, int(limit)))

    def since_sequence(
        self, sequence: int, limit: int = 200, last_id: int = 0
    ) -> list[Event]:
        """Return events strictly after the cursor ``(sequence, last_id)``.

        ``sequence`` alone is not a total order on ``terminal_events``（B6 之前的
        重号遗留 + 迟到行），所以游标是复合键：``sequence > :seq`` **或**
        ``sequence = :seq AND id > :id``。``last_id`` 默认 0 = 旧的「只按号」语义，
        单参调用（``since_sequence(cursor, limit=n)``）行为不变。
        """
        from db.meta_models import TerminalEvent

        try:
            with Session(self.engine) as session:
                rows = list(
                    session.execute(
                        select(TerminalEvent)
                        .where(
                            or_(
                                TerminalEvent.sequence > int(sequence or 0),
                                and_(
                                    TerminalEvent.sequence == int(sequence or 0),
                                    TerminalEvent.id > int(last_id or 0),
                                ),
                            )
                        )
                        .order_by(TerminalEvent.sequence.asc(), TerminalEvent.id.asc())
                        .limit(max(1, int(limit)))
                    ).scalars().all()
                )
            return [_event_from_row(row) for row in rows]
        except Exception:
            warn("core/terminal/event_store_db.py:1105 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []

    @staticmethod
    def is_visible(event: Event, principal: PrincipalContext) -> bool:
        return event_visible(event, principal)

    def query_visible(
        self,
        principal: PrincipalContext,
        *,
        event_types: Iterable[EventType] | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        fetch_limit = None if limit is None else max(int(limit) * 5, int(limit) + 100)
        candidates = self.query(event_types=event_types, limit=fetch_limit)
        visible = [event for event in candidates if self.is_visible(event, principal)]
        if limit is not None:
            return visible[-int(limit):]
        return visible

    def clear(self, *, allow_clear: bool = False) -> None:
        """清空账本（测试与运维专用；U-00 起必须显式授权）。

        账本是 append-only 的唯一事实源（R1）：整表删除是**唯一**能抹掉「她说过
        什么」的路径，所以没有显式 ``allow_clear=True`` 就拒绝。生产代码不该出现
        这个调用；测试夹具与运维脚本必须自己写明 ``allow_clear=True``，让删除这件
        事在 diff 里看得见。
        """
        if not allow_clear:
            raise ClearForbiddenError(
                "TerminalEventStore.clear() 需要 allow_clear=True：账本是 append-only "
                "唯一事实源（R1），整表删除必须由调用者显式承担"
            )
        from db.meta_models import TerminalEvent

        with self._lock:
            with Session(self.engine) as session:
                session.execute(delete(TerminalEvent))
                session.commit()
            # 没有进程内计数器要复位：空表的 MAX(sequence) 是 NULL → 下一条从 1 起。

    @classmethod
    def from_engine(cls, engine: Any) -> "TerminalEventStore":
        return cls(engine)
