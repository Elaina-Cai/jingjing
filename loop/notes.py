"""小本本：她自己记的事。

2026-10-28 合并。原来是两个文件：
  - `self_history.jsonl`（每天凌晨 3 点写的日记）
  - `memo.jsonl`（记着的约定 / 事实 / 规矩 / 待办）
概念上是同一件事——"她记着什么"，所以合成一本，用 `kind` 区分。

kind:
  diary    她自己写的那篇（一天最多一篇）
  fact     事实（"他住在杭州"）
  rule     规矩（"别在群里主动接话"）
  promise  答应过的事（带 due）
  note     其它零碎
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime

log = logging.getLogger(__name__)

_BAD = {"n": 0, "warned": False}


def _bad_line(exc) -> None:
    # 坏行仍然跳过（一行坏不能拖垮整本），但不再静默：数着，并且只喊一次。
    _BAD["n"] += 1
    if not _BAD["warned"]:
        _BAD["warned"] = True
        log.warning("notes_bad_line", extra={"err": str(exc)[:200]})   # 事件名裸（check.py EVENT_RE）；stdlib Logger 只认 extra=


def bad_line_count() -> int:
    """读到现在一共碰到多少坏行（测试与面板用）。"""
    return int(_BAD["n"])


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "loop", "notes.jsonl")
KINDS = {"diary": "日记", "fact": "事实", "rule": "规矩", "promise": "答应过", "note": "记着"}
_LOCK = threading.RLock()
_CACHE = {"mtime": 0.0, "rows": []}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _journal_path() -> str:
    from core.infra.envflags import text

    configured = text("YINYUE_NOTES_JOURNAL_DB")
    if configured:
        return configured
    marker = Path(ROOT) / "logs" / "notes-journal.active"
    try:
        return marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _journal_connection(path: str, *, write: bool = False):
    mode = "rw" if write else "ro"
    connection = sqlite3.connect(Path(path).resolve().as_uri() + f"?mode={mode}", uri=True)
    try:
        snapshot = connection.execute("SELECT source_hash FROM note_snapshot").fetchall()
        if snapshot != [(hashlib.sha256(Path(PATH).read_bytes()).hexdigest(),)]:
            raise ValueError("notes_source_drift")
        return connection
    except BaseException:
        connection.close()
        raise


def _journal_rows(path: str) -> list:
    with closing(_journal_connection(path)) as connection:
        return [json.loads(payload) for (payload,) in connection.execute(
            "SELECT payload FROM note_revisions ORDER BY seq"
        )]


def _load() -> list:
    journal = _journal_path()
    if journal:
        return _journal_rows(journal)
    try:
        mt = os.path.getmtime(PATH)
    except OSError:
        return []
    if _CACHE["mtime"] == mt and _CACHE["rows"]:
        return _CACHE["rows"]
    rows = []
    try:
        with open(PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception as exc:  # noqa: BLE001
                    _bad_line(exc)
                    continue
    except OSError:
        rows = []
    _CACHE["mtime"] = mt
    _CACHE["rows"] = rows
    return rows



_LEGACY = {"mtime": 0.0, "rows": []}


def _legacy_rows() -> list:
    """旧的两个文件（self_history / memo）也一起读，避免过渡期丢写。"""
    old1 = os.path.join(ROOT, "data", "loop", "self_history.jsonl")
    old2 = os.path.join(ROOT, "data", "loop", "memo.jsonl")
    try:
        mt = max(os.path.getmtime(old1) if os.path.isfile(old1) else 0.0,
                 os.path.getmtime(old2) if os.path.isfile(old2) else 0.0)
    except OSError:
        return []
    if _LEGACY["mtime"] == mt and _LEGACY["rows"]:
        return _LEGACY["rows"]
    rows = []
    if os.path.isfile(old1):
        for line in open(old1, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                _bad_line(exc)
                continue
            if str(r.get("text") or "").strip():
                rows.append({"at": str(r.get("at") or r.get("day") or ""), "kind": "diary",
                             "text": str(r.get("text") or ""), "_legacy": True})
    if os.path.isfile(old2):
        for line in open(old2, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                _bad_line(exc)
                continue
            t = str(r.get("text") or "").strip()
            if not t:
                continue
            k = str(r.get("kind") or "note").lower()
            if k not in ("fact", "rule", "promise", "note"):
                k = "note"
            row = {"at": str(r.get("at") or ""), "kind": k, "text": t, "_legacy": True}
            if r.get("target"):
                row["target"] = str(r.get("target"))
            rows.append(row)
    _LEGACY["mtime"] = mt
    _LEGACY["rows"] = rows
    return rows


def _all_rows() -> list:
    """notes.jsonl + 旧文件；同一 note_id 只保留最新 revision，撤销版隐藏。"""
    rows = list(_load()) + list(_legacy_rows())
    latest: dict[str, tuple[int, dict]] = {}
    passthrough: list[tuple[int, dict]] = []
    for pos, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        # Canonical-memory snapshots are obsolete caches, never a second fact source.
        if row.get("memory_id") or row.get("memory_source") == "loop_remember":
            continue
        nid = str(row.get("note_id") or "").strip()
        if not nid:
            passthrough.append((pos, row))
            continue
        prev = latest.get(nid)
        if prev is None or (_revision_of(row), pos) >= (_revision_of(prev[1]), prev[0]):
            latest[nid] = (pos, row)
    merged = [row for _, row in sorted(list(latest.values()) + passthrough, key=lambda item: item[0])]
    seen, out = set(), []
    for row in merged:
        if _status_of(row) == "revoked":
            continue
        key = (str(row.get("kind") or ""), str(row.get("target") or ""),
               str(row.get("visibility") or ""), str(row.get("text") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def all_rows() -> list:
    """公开读取口：已经按 note_id 折叠、过滤撤销版。"""
    return _all_rows()


def diary_rows() -> list:
    """她自己的日记（不按会话可见范围过滤；这是自读视图）。"""
    return sorted([row for row in _all_rows() if row.get("kind") == "diary"],
                  key=lambda row: str(row.get("at") or ""))


def _new_note_id() -> str:
    return "note-" + uuid.uuid4().hex[:16]


def _revision_of(row: dict) -> int:
    try:
        return max(1, int(row.get("revision") or 1))
    except (TypeError, ValueError):
        return 1


def _status_of(row: dict) -> str:
    return str(row.get("status") or "active").strip().lower() or "active"


def _find_latest(note_id: str) -> dict | None:
    rows = _load()
    found = [(i, r) for i, r in enumerate(rows)
             if isinstance(r, dict) and str(r.get("note_id") or "") == note_id]
    if not found:
        return None
    return max(found, key=lambda item: (_revision_of(item[1]), item[0]))[1]


def _append_row(row: dict) -> dict:
    journal = _journal_path()
    if journal:
        with closing(_journal_connection(journal, write=True)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                "SELECT MAX(revision) FROM note_revisions WHERE note_id=?", (row["note_id"],)
            ).fetchone()[0]
            if int(row["revision"]) != int(latest or 0) + 1:
                raise ValueError("note_revision_conflict")
            connection.execute(
                "INSERT INTO note_revisions(note_id, revision, payload) VALUES(?,?,?)",
                (row["note_id"], row["revision"], json.dumps(row, ensure_ascii=False)),
            )
        return row
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with _LOCK:
        with open(PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _CACHE["mtime"] = 0.0
    return row


def add(kind: str, text: str, *, target: str = "", at: str = "", due: str = "",
        extra: dict | None = None, evidence_refs: tuple[str, ...] | list[str] = (),
        note_id: str = "", revision: int = 0, status: str = "active") -> dict:
    """追加一版小本本记录。

    2026-11-08：写入契约补齐 ``note_id`` / ``revision`` / ``status`` /
    ``evidence_refs``。同一 ``note_id`` 的后续版本只追加、不覆盖；读取侧
    按 revision 取最新一版，``revoked`` 版本从所有读取入口消失。
    """
    kind = str(kind or "note").strip().lower()
    if kind not in KINDS:
        kind = "note"
    text = str(text or "").strip()
    if not text and str(status or "active") != "revoked":
        return {}
    nid = str(note_id or "").strip() or _new_note_id()
    rev = int(revision or 0)
    if rev <= 0:
        latest = _find_latest(nid)
        rev = (_revision_of(latest) + 1) if latest is not None else 1
    row = {
        "at": at or _now(),
        "kind": kind,
        "text": text,
        "note_id": nid,
        "revision": rev,
        "status": str(status or "active").strip().lower() or "active",
    }
    if target:
        row["target"] = str(target)
    if due:
        row["due"] = str(due)
    refs = [str(x).strip() for x in (evidence_refs or ()) if str(x).strip()]
    if refs:
        row["evidence_refs"] = refs
    if extra:
        for key, value in extra.items():
            if key in {"note_id", "revision", "status", "at", "kind", "text",
                       "target", "due", "evidence_refs"}:
                continue
            if value is not None:
                row[key] = value
    return _append_row(row)


def revise(note_id: str, text: str | None = None, *, at: str = "", due: str | None = None,
           target: str | None = None, evidence_refs: list[str] | tuple[str, ...] | None = None,
           extra: dict | None = None) -> dict:
    """给一条记录追加新版本；旧版本保留，读取侧只看最新版。"""
    latest = _find_latest(str(note_id or "").strip())
    if latest is None:
        return {}
    body = str(text if text is not None else latest.get("text") or "").strip()
    if not body:
        return {}
    reserved = {"note_id", "revision", "status", "at", "kind", "text", "target", "due",
                "evidence_refs", "revoke_reason"}
    merged = {k: v for k, v in latest.items()
              if k not in reserved and not str(k).startswith("_")}
    if extra:
        merged.update(extra)
    refs = list(latest.get("evidence_refs") or ())
    if evidence_refs is not None:
        refs.extend(evidence_refs)
    seen, ordered = set(), []
    for ref in refs:
        value = str(ref).strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return add(str(latest.get("kind") or "note"), body,
               target=(latest.get("target") or "") if target is None else target,
               at=at or _now(),
               due=(latest.get("due") or "") if due is None else due,
               extra=merged, evidence_refs=tuple(ordered),
               note_id=str(latest.get("note_id")),
               revision=_revision_of(latest) + 1)


def revoke(note_id: str, *, reason: str = "", at: str = "",
           evidence_refs: list[str] | tuple[str, ...] = ()) -> dict:
    """追加一条撤销墓碑；读取侧按同一 note_id 的最新 revision 隐藏。"""
    latest = _find_latest(str(note_id or "").strip())
    if latest is None:
        return {}
    refs = list(latest.get("evidence_refs") or ()) + list(evidence_refs or ())
    seen, ordered = set(), []
    for ref in refs:
        value = str(ref).strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    row = {
        "at": at or _now(),
        "kind": str(latest.get("kind") or "note"),
        "text": "",
        "note_id": str(latest.get("note_id")),
        "revision": _revision_of(latest) + 1,
        "status": "revoked",
        "revoke_reason": str(reason or "")[:200],
    }
    if latest.get("target"):
        row["target"] = str(latest.get("target"))
    if ordered:
        row["evidence_refs"] = ordered
    return _append_row(row)


def has_diary(day: str) -> bool:
    for r in _all_rows():
        if r.get("kind") == "diary" and str(r.get("at") or "").startswith(day):
            return True
    return False


def visible_rows(rows, target: str = "") -> list:
    """自动记忆读取的唯一范围判据：同会话或明确共享；空来源不是全局授权。"""
    current = str(target or "").strip()
    return [r for r in rows if isinstance(r, dict) and _status_of(r) != "revoked" and (
        r.get("visibility") == "shared" or
        (current and str(r.get("target") or "").strip() == current))]


def diaries(n: int = 3, *, target: str = "") -> list:
    ds = [r for r in visible_rows(_all_rows(), target) if r.get("kind") == "diary"]
    ds.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return ds[: int(n)]


def _resident_rows(target: str) -> list:
    from core.infra.envflags import text

    source = text("YINYUE_SCOPED_NOTES_DB")
    current = _all_rows()
    if not source:
        return visible_rows(current, target)
    from loop.ports import current_read_scope

    scope = current_read_scope()
    if scope is None or not all((scope.platform, scope.account_id, scope.chat_id)) or scope.chat_id != target:
        return []
    rows = {hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest(): row
            for row in current}
    try:
        with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("BEGIN")
            bindings = connection.execute("SELECT fingerprint, platform, account_id, chat_id FROM scoped_notes").fetchall()
            moved = {key for key, _, _, _ in bindings}
            targets = {chat for _, _, _, chat in bindings}
            allowed_targets = {chat for _, platform, account, chat in bindings
                               if (platform, account) == (scope.platform, scope.account_id)}
            owned = connection.execute(
                "SELECT fingerprint, payload FROM scoped_notes WHERE platform=? AND account_id=? AND chat_id=?",
                (scope.platform, scope.account_id, scope.chat_id),
            ).fetchall()
        projected = {}
        for key, payload in owned:
            row = json.loads(payload)
            if key in rows and row == rows[key]:
                projected[key] = row
        return visible_rows(
            [projected[key] if key in projected else row for key, row in rows.items()
             if (key not in moved or key in projected)
             and (row.get("target") not in targets or row.get("target") in allowed_targets)],
            target,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        log.warning("notes_scoped_unavailable", kind=type(exc).__name__)
        return []


def residents(target: str = "", budget: int = 300) -> list:
    """只读取当前会话与明确共享的长期记忆，再按时间排序。"""
    rows = [r for r in _resident_rows(target) if r.get("kind") in ("fact", "rule", "promise", "note")]
    t = str(target or "").strip()

    def rank(r):
        same = 1 if (t and str(r.get("target") or "") == t) else 0
        return (same, str(r.get("at") or ""))
    rows.sort(key=rank, reverse=True)
    out, used = [], 0
    for r in rows:
        line = "%s：%s" % (KINDS.get(str(r.get("kind")), "记着"), str(r.get("text") or ""))
        if used + len(line) > int(budget) and out:
            break
        out.append(line)
        used += len(line) + 1
    return out


_END_MARKS = "。！？…!?；;"


def _cut_sentence(text, n: int) -> str:
    """按句子截，不切在字中间（2026-10-28：以前硬切 220 字，她读到「…旧话头当成了新回声。\n最」）。"""
    s = str(text or "")
    if len(s) <= n:
        return s
    head = s[:n]
    cut = max(head.rfind(ch) for ch in _END_MARKS)
    return head[: cut + 1] if cut >= n // 3 else head[: n - 1] + "…"


def note_block(target: str = "", budget: int = 300, diary_n: int = 2) -> list:
    """拼成两小节，供 prompt 用：先日记，再长期的事。"""
    out = []
    for r in diaries(diary_n, target=target):
        out.append("- %s %s" % (str(r.get("at") or "")[5:16], _cut_sentence(r.get("text"), 220)))
    rest = residents(target, budget)
    if rest:
        out.extend("- " + x for x in rest)
    return out


def import_legacy() -> dict:
    """一次性把 self_history.jsonl（日记）与 memo.jsonl（长期）导入，可重复执行（跳过已导入）。"""
    have = {json.dumps(r, ensure_ascii=False, sort_keys=True) for r in _load()}
    added = {"diary": 0, "memo": 0}
    old = os.path.join(ROOT, "data", "loop", "self_history.jsonl")
    if os.path.isfile(old):
        for line in open(old, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                _bad_line(exc)
                continue
            text = str(r.get("text") or "").strip()
            if not text:
                continue
            row = {"at": str(r.get("at") or r.get("day") or ""), "kind": "diary", "text": text}
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key in have:
                continue
            add("diary", text, at=row["at"], extra={"src": "self_history"})
            have.add(key)
            added["diary"] += 1
    old2 = os.path.join(ROOT, "data", "loop", "memo.jsonl")
    if os.path.isfile(old2):
        for line in open(old2, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                _bad_line(exc)
                continue
            text = str(r.get("text") or "").strip()
            if not text:
                continue
            kind = str(r.get("kind") or "note").lower()
            if kind not in ("fact", "rule", "promise", "note"):
                kind = "note"
            row = {"at": str(r.get("at") or ""), "kind": kind, "text": text}
            if r.get("target"):
                row["target"] = str(r.get("target"))
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key in have:
                continue
            add(kind, text, at=row["at"], target=str(r.get("target") or ""),
                due=str(r.get("due") or ""), extra={"src": "memo"})
            have.add(key)
            added["memo"] += 1
    return added
