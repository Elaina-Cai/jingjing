"""message_media 基质：媒体件落库 + 附件正文抽取（task-26）。

一句话
======
事件里只留了 ``[文件]`` / ``[图片]`` 占位符，附件名与正文都没进语料；本模块在
采集侧把每个媒体件写成 ``message_media`` 一行，并把能本地抽出的正文写成
``text_excerpt``（≤4000 字，可选全文落 ``data/media_text/<md5>.txt``）。

边界
----
* **best-effort**：任何异常只记一行日志，绝不影响消息入库与回复链。
* **纯本地**：不联网、不调 LLM。pdf 优先用已安装的库（pypdf/PyPDF2/pdfminer/fitz），
  都没有时用内置的 ``zlib + Tj/TJ`` 文本层解析；扫描件（抽不出字）记
  ``status="unsupported"`` + reason，不静默。
* **开关**：``YINYUE_FEATURE_MEDIA_STORE``（tri，默认 off）。off 时
  :func:`register_media_rows` 直接返回 0，不做任何 IO。
* 历史媒体不回填（老行保持缺失），只处理本次入库的新消息。

表结构
------
表由迁移 28 建、迁移 60 补列（``event_id / size_bytes / mime / original_name /
text_excerpt / meta_json``）。写入用 SQLAlchemy Core 的 ``text()``，不依赖 ORM 模型
（``db/models.py`` 里的 ``MessageMedia`` 是账号库的同名孪生，生命周期不同）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zipfile
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import structlog
from sqlalchemy import text as sql_text

from core import features
from core.group_facts import media_store_row_ref
from safelog import warn

log = structlog.get_logger()

TEXT_EXCERPT_MAX = 4000
READ_MAX_BYTES = 200_000
MAX_ROWS_PER_BATCH = 50
TABLE = "message_media"

FILE_SUFFIXES = (
    "pdf", "docx", "xlsx", "pptx", "zip", "rar", "7z",
    "txt", "md", "csv", "json", "log", "py", "yaml", "yml",
)
IMAGE_SUFFIXES = ("jpg", "jpeg", "png", "gif", "webp", "bmp")
AUDIO_SUFFIXES = ("silk", "amr", "mp3", "wav", "ogg", "m4a")
VIDEO_SUFFIXES = ("mp4", "mov", "avi", "mkv")
PASSIVE_SUFFIXES = IMAGE_SUFFIXES + AUDIO_SUFFIXES + VIDEO_SUFFIXES

#: 扩展名 -> MIME：采集侧 ``media_mime`` 为空时的兜底（task-26 观测修正）。
#: 只用于「这是什么类型」的读数与读侧展示，不参与抽取判定。
MIME_BY_SUFFIX = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
    "rar": "application/vnd.rar",
    "7z": "application/x-7z-compressed",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "json": "application/json",
    "log": "text/plain",
    "py": "text/x-python",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "mp3": "audio/mpeg",
    "amr": "audio/amr",
    "m4a": "audio/mp4",
    "silk": "audio/silk",
    "wav": "audio/wav",
    "dat": "application/octet-stream",
}

_FILENAME_RE = re.compile(
    r"([^\s:：/\\]{1,160}\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|txt|md|csv|json))",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def media_store_mode() -> str:
    """开关档位（off/shadow/on）；异常按 off 处理。"""
    try:
        return features.mode(features.MEDIA_STORE, features.MODE_OFF)
    except Exception:
        warn("core/ingestion/media_store.py:107 Exception", err=repr(__import__("sys").exc_info()[1]))
        return features.MODE_OFF


def media_store_enabled() -> bool:
    """开关是否不是 off（shadow/on 都要落库：影子期先攒数据）。"""
    return media_store_mode() != features.MODE_OFF


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def full_text_dir() -> Path:
    """全文落盘目录：``$DATA_DIR/media_text``（缺省 ``<repo>/data/media_text``）。"""
    raw = str(os.environ.get("DATA_DIR") or "").strip()
    base = Path(raw) if raw else repo_root() / "data"
    return base / "media_text"


def _suffix(path: str) -> str:
    name = str(path or "").strip()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].strip().lower()


def _basename(path: str) -> str:
    name = str(path or "").strip().replace("\\", "/")
    return name.rsplit("/", 1)[-1] if name else ""


def _extra_dict(raw: dict) -> dict:
    value = raw.get("extra")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            warn("core/ingestion/media_store.py:147 Exception", err=repr(__import__("sys").exc_info()[1]))
            return {}
    return {}


def media_path_from_row(raw: dict) -> Path | None:
    """这条消息的本地媒体文件路径（能解析到就给），解析不出返回 None。"""
    candidates: list[str] = []
    for key in ("media_path", "local_path", "source_path"):
        value = str(raw.get(key) or "").strip()
        if value:
            candidates.append(value)
    extra = _extra_dict(raw)
    for key in ("media_path", "path", "local_path", "file_path"):
        value = str(extra.get(key) or "").strip()
        if value:
            candidates.append(value)
    for value in candidates:
        path = Path(value)
        if not path.is_absolute():
            path = repo_root() / value
        if path.exists() and path.is_file():
            return path
    return None


def original_name_from_row(raw: dict) -> str:
    """附件显示名：优先原始文件名，其次正文里出现的文件名，最后占位符后的描述。"""
    for value in (
        str(raw.get("original_name") or "").strip(),
        str(raw.get("media_name") or "").strip(),
    ):
        if value:
            return value
    path_name = _basename(str(raw.get("media_path") or ""))
    if path_name:
        return path_name
    extra = _extra_dict(raw)
    for key in ("file_name", "original_name", "media_name", "title", "display_name"):
        value = str(extra.get(key) or "").strip()
        if value:
            return value
    content = str(raw.get("content") or "")
    match = _FILENAME_RE.search(content)
    if match is not None:
        return match.group(1).strip()
    return ""
def kind_of_row(raw: dict) -> str:
    """媒体种类：image|file|video|voice|emoji（空串 = 不是媒体）。

    复用 ``core.group_state`` 的采集侧判据（``msg_type`` 优先、占位符兜底），
    只把它的 ``sticker`` 映射成表里的历史别名 ``emoji``。
    """
    content = str(raw.get("content") or "")
    kind = str(raw.get("msg_type_name") or raw.get("msg_type") or raw.get("kind") or "")
    try:
        mapped = media_store_row_ref(content, kind)
    except Exception:
        warn("core/ingestion/media_store.py:204 Exception", err=repr(__import__("sys").exc_info()[1]))
        mapped = ""
    if mapped:
        return mapped
    suffix = _suffix(str(raw.get("media_path") or ""))
    if suffix in PASSIVE_SUFFIXES:
        if suffix in IMAGE_SUFFIXES:
            return "image"
        if suffix in AUDIO_SUFFIXES:
            return "voice"
        return "video"
    if suffix in FILE_SUFFIXES:
        return "file"
    return ""


def md5_of_file(path: Path, *, limit: int = 8_000_000) -> str:
    """文件 md5（超过 ``limit`` 字节只算前缀，避免读几百 MB 的缓存件）。"""
    try:
        digest = hashlib.md5()
        with path.open("rb") as handle:
            remaining = int(limit)
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.hexdigest()
    except Exception:
        warn("core/ingestion/media_store.py:233 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _read_text_file(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(READ_MAX_BYTES)
    except Exception:
        warn("core/ingestion/media_store.py:241 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    for encoding in ("utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            warn("core/ingestion/media_store.py:246 UnicodeDecodeError", err=repr(__import__("sys").exc_info()[1]))
            continue
    return data.decode("utf-8", errors="replace")


def _xml_text(data: bytes) -> str:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        warn("core/ingestion/media_store.py:254 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _zip_member_text(path: Path, prefixes: Sequence[str], *, limit_members: int = 200) -> str:
    parts: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            wanted = [name for name in names if any(name.startswith(prefix) for prefix in prefixes)]
            if not wanted:
                wanted = names[:limit_members]
            for name in wanted[:limit_members]:
                if name.endswith("/"):
                    continue
                try:
                    parts.append(_xml_text(archive.read(name)))
                except Exception:
                    warn("core/ingestion/media_store.py:273 Exception", err=repr(__import__("sys").exc_info()[1]))
                    continue
    except Exception:
        warn("core/ingestion/media_store.py:275 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    return "\n".join(part for part in parts if part)


def _pdf_builtin_text(data: bytes) -> str:
    """内置 pdf 文本层解析：解 FlateDecode 流，抽 ``(...) Tj`` 与 ``[...] TJ``。"""
    pieces: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        raw = match.group(1)
        payload = raw
        try:
            payload = zlib.decompress(raw)
        except Exception:
            warn("core/ingestion/media_store.py:288 Exception", err=repr(__import__("sys").exc_info()[1]))
            try:
                payload = zlib.decompress(raw.strip(b"\r\n"))
            except Exception:
                warn("core/ingestion/media_store.py:291 Exception", err=repr(__import__("sys").exc_info()[1]))
                payload = raw
        body = payload.decode("latin-1", errors="replace")
        if "Tj" not in body and "TJ" not in body:
            continue
        for token in re.finditer(r"\((?:\\.|[^\\()])*\)", body):
            value = token.group(0)[1:-1]
            value = (
                value.replace("\\(", "(")
                .replace("\\)", ")")
                .replace("\\\\", "\\")
                .replace("\\n", " ")
                .replace("\\r", " ")
                .replace("\\t", " ")
            )
            pieces.append(value)
        pieces.append(" ")
    text = _WS_RE.sub(" ", "".join(pieces)).strip()
    return text


def _pdf_text(path: Path) -> tuple[str, str]:
    """返回 ``(text, reason)``；text 为空时 reason 说明为什么。"""
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name)
            reader = module.PdfReader(str(path))
            chunks = [page.extract_text() or "" for page in reader.pages]
            text = _WS_RE.sub(" ", "\n".join(chunks)).strip()
            if text:
                return text, ""
            return "", "no text layer"
        except ImportError:
            warn("core/ingestion/media_store.py:323 ImportError", err=repr(__import__("sys").exc_info()[1]))
            continue
        except Exception as exc:
            warn("core/ingestion/media_store.py:325 Exception", err=repr(exc))
            return "", "pdf %s failed: %s" % (module_name, str(exc)[:120])
    try:
        from pdfminer.high_level import extract_text as _pdfminer_text

        text = _WS_RE.sub(" ", str(_pdfminer_text(str(path)) or "")).strip()
        if text:
            return text, ""
        return "", "no text layer"
    except ImportError:
        warn("core/ingestion/media_store.py:334 ImportError", err=repr(__import__("sys").exc_info()[1]))
    except Exception as exc:
        warn("core/ingestion/media_store.py:336 Exception", err=repr(exc))
        return "", "pdf pdfminer failed: %s" % str(exc)[:120]
    try:
        import fitz

        document = fitz.open(str(path))
        text = _WS_RE.sub(" ", "\n".join(page.get_text() for page in document)).strip()
        if text:
            return text, ""
        return "", "no text layer"
    except ImportError:
        warn("core/ingestion/media_store.py:346 ImportError", err=repr(__import__("sys").exc_info()[1]))
    except Exception:
        warn("core/ingestion/media_store.py:348 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        data = path.read_bytes()
    except Exception as exc:
        warn("core/ingestion/media_store.py:352 Exception", err=repr(exc))
        return "", "read failed: %s" % str(exc)[:120]
    text = _pdf_builtin_text(data)
    if text:
        return text, ""
    return "", "no text layer (builtin)"


def extract_media_text(path: Path | None, *, kind: str = "") -> dict:
    """抽取一个媒体件的可读正文。

    返回 ``{"status", "text", "reason", "size_bytes", "mime"}``：
    ``status`` ∈ ``ok`` / ``missing`` / ``unsupported``。
    """
    result: dict[str, Any] = {
        "status": "missing",
        "text": "",
        "reason": "",
        "size_bytes": 0,
        "mime": "",
    }
    if path is None:
        result["reason"] = "media path not resolved at ingest"
        return result
    if not path.exists():
        # 路径解析到了、文件却不在：与「还没轮到解析」分开，24h 分布要能区分。
        result["reason"] = "media file gone at extract: %s" % path.name[:120]
        return result
    try:
        result["size_bytes"] = int(path.stat().st_size)
    except Exception:
        warn("core/ingestion/media_store.py:382 Exception", err=repr(__import__("sys").exc_info()[1]))
        result["size_bytes"] = 0
    suffix = _suffix(path.name)
    result["mime"] = MIME_BY_SUFFIX.get(suffix, "")
    if suffix in PASSIVE_SUFFIXES:
        result["status"] = "ok"
        result["reason"] = "no text layer for %s" % (suffix or kind or "media")
        return result
    text = ""
    reason = ""
    if suffix == "pdf":
        text, reason = _pdf_text(path)
    elif suffix == "docx":
        text = _zip_member_text(path, ("word/document.xml",))
    elif suffix == "xlsx":
        text = _zip_member_text(path, ("xl/sharedStrings.xml", "xl/worksheets/sheet1.xml"))
    elif suffix == "pptx":
        text = _zip_member_text(path, ("ppt/slides/",))
    elif suffix == "zip":
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
            text = "\n".join(names[:200])
        except Exception as exc:
            warn("core/ingestion/media_store.py:405 Exception", err=repr(exc))
            reason = "zip read failed: %s" % str(exc)[:120]
    elif suffix in FILE_SUFFIXES:
        text = _read_text_file(path)
    else:
        result["status"] = "unsupported"
        result["reason"] = "no extractor for %s" % (suffix or "unknown")
        return result
    text = str(text or "").strip()
    if not text:
        result["status"] = "unsupported"
        result["reason"] = reason or "no extractable text"
        return result
    result["status"] = "ok"
    result["text"] = text[:TEXT_EXCERPT_MAX]
    result["reason"] = reason
    return result


def _write_full_text(md5: str, text: str) -> str:
    """全文落 ``media_text/<md5>.txt``（best-effort，返回落盘路径）。"""
    if not md5 or not text:
        return ""
    try:
        target_dir = full_text_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / ("%s.txt" % md5)
        if not target.exists():
            target.write_text(text, encoding="utf-8")
        return str(target)
    except Exception:
        warn("core/ingestion/media_store.py:435 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _now() -> datetime:
    return datetime.now()


def _meta_engine() -> Any:
    from core.terminal.store_port import get_meta_repo

    return get_meta_repo().engine


def mime_of_row(raw: dict, path: Path | None = None, extracted: dict | None = None) -> str:
    """这一行的 MIME：``media_mime`` 优先，其次抽取结果，最后按扩展名兜底。

    采集侧对图片/表情经常不给 ``media_mime``；没有兜底时 24h 分布里 mime 会全是空。
    """
    declared = str((raw or {}).get("media_mime") or "").strip()
    if declared:
        return declared
    if extracted:
        value = str(extracted.get("mime") or "").strip()
        if value:
            return value
    name = ""
    if path is not None:
        name = path.name
    if not name:
        name = str((raw or {}).get("media_path") or (raw or {}).get("original_name") or "")
    return MIME_BY_SUFFIX.get(_suffix(name), "")


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        warn("core/ingestion/media_store.py:472 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return 0


def _row_exists(conn: Any, *, chat_id: str, kind: str, event_id: str, msg_id: int) -> bool:
    if not chat_id or not kind:
        return False
    sql = (
        "SELECT id FROM " + TABLE + " WHERE chat_id = :chat_id AND kind = :kind"
        " AND ((event_id = :event_id AND :event_id <> '')"
        " OR (msg_id = :msg_id AND :msg_id > 0)) LIMIT 1"
    )
    try:
        row = conn.execute(
            sql_text(sql),
            {"chat_id": chat_id, "kind": kind, "event_id": event_id, "msg_id": msg_id},
        ).fetchone()
        return row is not None
    except Exception:
        warn("core/ingestion/media_store.py:490 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False
_INSERT_SQL = (
    "INSERT INTO " + TABLE + " ("
    "message_id, msg_id, platform, account_id, chat_id, sender_id, kind, status,"
    " md5, session_dir, quote_ref, source_path, local_path, description, attempts,"
    " last_error, next_retry_at, created_at, resolved_at,"
    " event_id, size_bytes, mime, original_name, text_excerpt, meta_json"
    ") VALUES ("
    ":message_id, :msg_id, :platform, :account_id, :chat_id, :sender_id, :kind, :status,"
    " :md5, :session_dir, :quote_ref, :source_path, :local_path, :description, :attempts,"
    " :last_error, :next_retry_at, :created_at, :resolved_at,"
    " :event_id, :size_bytes, :mime, :original_name, :text_excerpt, :meta_json"
    ")"
)


def _build_values(
    raw: dict,
    *,
    platform: str,
    account_id: str,
    event_id: str,
    now: datetime,
) -> dict:
    kind = kind_of_row(raw)
    path = media_path_from_row(raw)
    started = time.perf_counter()
    extracted = extract_media_text(path, kind=kind)
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    md5 = str(raw.get("md5") or "").strip() or _extra_dict(raw).get("md5") or ""
    md5 = str(md5 or "").strip()
    if not md5 and path is not None:
        md5 = md5_of_file(path)
    excerpt = str(extracted.get("text") or "")[:TEXT_EXCERPT_MAX]
    full_path = _write_full_text(md5, str(extracted.get("text") or "")) if len(extracted.get("text") or "") > TEXT_EXCERPT_MAX else ""
    chat_id = str(raw.get("chat_id") or "")
    meta = {
        "msg_id": str(raw.get("msg_id") or raw.get("id") or ""),
        "msg_type_name": str(raw.get("msg_type_name") or raw.get("msg_type") or ""),
        "source": str(raw.get("source") or platform),
        "session_dir": str(_extra_dict(raw).get("session_dir") or ""),
        "full_text_path": full_path,
        "reason": str(extracted.get("reason") or ""),
        "content": str(raw.get("content") or "")[:300],
        # task-26 观测：逐件抽取耗时（24h 分布用），纯读侧统计，不参与判定。
        "elapsed_ms": elapsed_ms,
        "has_text": bool(excerpt),
    }
    return {
        "message_id": _int_or_zero(raw.get("local_id") or raw.get("message_id")),
        "msg_id": _int_or_zero(raw.get("msg_id") or raw.get("id")),
        "platform": str(platform or raw.get("source") or "wechat"),
        "account_id": str(account_id or raw.get("account_id") or ""),
        "chat_id": chat_id,
        "sender_id": str(raw.get("sender_id") or ""),
        "kind": kind,
        "status": str(extracted.get("status") or "missing"),
        "md5": md5,
        "session_dir": str(_extra_dict(raw).get("session_dir") or ""),
        "quote_ref": str(raw.get("quote_reply_id") or ""),
        "source_path": str(raw.get("media_path") or ""),
        "local_path": str(path) if path is not None else "",
        "description": str(raw.get("media_mime") or ""),
        "attempts": 1 if path is not None else 0,
        "last_error": str(extracted.get("reason") or ""),
        "next_retry_at": None,
        "created_at": now,
        "resolved_at": now if extracted.get("status") == "ok" else None,
        "event_id": str(event_id or ""),
        "size_bytes": int(extracted.get("size_bytes") or 0),
        "mime": mime_of_row(raw, path, extracted),
        "original_name": original_name_from_row(raw),
        "text_excerpt": excerpt,
        "meta_json": json.dumps(meta, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# 2026-10-26 入库即解析：媒体解析一直是 scheduler 的攒批任务
# （`resolve_pending(limit=40)`，退避表 30/120/600/1800/7200/21600 秒），等它回头
# 处理某一张图时，那一轮早就回完了 —— 10-26 12:54:26 那句「图我这边只是个空框」
# 与 12:58:23 才写进来的 local_path，中间差的就是这段时间。这里在入库那一刻为
# 「这一条」就地试一次：成功就地写 ok，失败仍按原来的 missing 落库，
# 幂等语义不变。
# ---------------------------------------------------------------------------

_INGEST_RESOLVE_KINDS = ("image", "quote_image")


def _store_row_description(engine, row_id: int, text: str) -> None:
    """把描述写回**账号库**那一行。

    2026-10-26：loop 的 `sense_sync` 是裸 sqlite 读账号库的 `messages`，
    图片描述它只认 `message_media.description`（同一个库）。所以描述必须落到
    账号库这一行，不能落到任一进程的默认 repo（那可能是 meta 库或别的账号）。
    """
    if engine is None or not row_id or not text:
        return
    try:
        from sqlalchemy.orm import Session

        from db.models import MessageMedia

        with Session(engine) as session:
            row = session.get(MessageMedia, int(row_id))
            if row is None:
                return
            row.description = str(text)[:500]
            session.commit()
        log.info("media_description_written", row_id=int(row_id), chars=len(str(text)))
    except Exception as exc:  # noqa: BLE001
        log.debug("media_description_write_failed", error=str(exc)[:160])


def _prefetch_description(path: Path | None, *, engine=None, row_id: int = 0) -> None:
    """图刚从加密态解出来时，后台先把描述问好（不阻塞入库/调度）。"""
    if path is None:
        return
    try:
        from core.conversation.media import prefetch_image_description

        on_ready = None
        if row_id:
            on_ready = lambda text: _store_row_description(engine, row_id, text)  # noqa: E731
        prefetch_image_description(str(path), on_ready=on_ready)
    except Exception as exc:  # noqa: BLE001
        log.debug("media_prefetch_description_failed", error=str(exc)[:160])


def _resolve_at_ingest(raw: dict, *, now: datetime) -> Path | None:
    """为这一条入站媒体就地解析一次（best-effort，任何异常都返回 None）。

    查的是**账号库**里那个 pending 行（幂等的 `message_media`）：
    `resolve_pending` 就是在那一库上工作的，本模块写的是 meta 库。
    先按 md5 认行（msg_id 两库不一定一致：有时尾数被浮点吃掉），再退 msg_id。
    """
    kind = kind_of_row(raw)
    if kind not in _INGEST_RESOLVE_KINDS:
        return None
    chat_id = str(raw.get("chat_id") or "")
    md5 = str(raw.get("md5") or _extra_dict(raw).get("md5") or "").strip().lower()
    msg_id = _int_or_zero(raw.get("msg_id") or raw.get("id"))
    if not chat_id or (not md5 and not msg_id):
        return None
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from core.conversation.media_registry import resolve_once
        from core.terminal.store_port import get_repo
        from db.models import MessageMedia

        account_engine = get_repo().engine
        with Session(account_engine) as session:
            candidates = list(session.scalars(
                select(MessageMedia)
                .where(MessageMedia.chat_id == chat_id)
                .order_by(MessageMedia.id.desc())
                .limit(20)
            ).all())
            row = None
            for candidate in candidates:
                if md5 and str(candidate.md5 or "").strip().lower() == md5:
                    row = candidate
                    break
            if row is None and msg_id:
                for candidate in candidates:
                    if int(candidate.msg_id or 0) == int(msg_id):
                        row = candidate
                        break
            if row is None:
                return None
            local, status, error = resolve_once(row, engine=account_engine)
            row.attempts = int(row.attempts or 0) + 1
            if local and status == "resolved":
                row.status = "resolved"
                row.local_path = local
                row.resolved_at = now
                row.next_retry_at = None
                row.last_error = None
                session.commit()
                log.info("media_resolved_at_ingest", chat_id=chat_id[:64],
                         path=Path(local).name)
                row_id = int(getattr(row, "id", 0) or 0)
                _prefetch_description(Path(local), engine=account_engine, row_id=row_id)
                return Path(local)
            row.last_error = str(error or "")[:400]
            session.commit()
            log.debug("media_unresolved_at_ingest", chat_id=chat_id[:64],
                      status=status, error=str(error or "")[:120])
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("media_ingest_resolve_failed", error=str(exc)[:200])
        return None


def describe_missing_media(engine: Any, *, limit: int = 6,
                           kinds: Sequence[str] = ("image", "quote_image"),
                           newer_than_hours: int = 72) -> dict:
    """给「已经落盘、但还没有描述」的图补一次视觉描述（写回账号库那一行）。

    2026-10-27 用户问："她最新还是说看不到图里的内容"。
    查下来：入库时就解析成功的图会走 `_prefetch_description`；**晚到**的图
    （微信几秒后才落盘）由 `resolve_pending` + `backfill_media_from_account`
    回填，那条路只补 mime/size/md5/local_path，**从来不问描述** —— 于是
    `message_media.description` 一直是 NULL，视界里只剩 OCR 的界面噪声。

    这里补上这一环：只碰「有 local_path、没 description、status=resolved」的行，
    每批 limit 张，问一次视觉模型，成功就把描述写回该行（失败计数，不抛）。
    """
    report = {"rows": 0, "described": 0, "failed": 0, "skipped": 0}
    if not media_store_enabled():
        return report
    try:
        from datetime import timedelta

        from sqlalchemy import text as sql_text

        since = _now() - timedelta(hours=max(1, int(newer_than_hours)))
        marks = ",".join(":k%d" % i for i in range(len(kinds)))
        params: dict[str, Any] = {"since": since, "limit": int(max(1, limit))}
        for i, k in enumerate(kinds):
            params["k%d" % i] = str(k)
        sql = (
            "SELECT id, local_path, kind FROM " + TABLE +
            " WHERE status = 'resolved'"
            " AND (description IS NULL OR description = '')"
            " AND local_path IS NOT NULL AND local_path <> ''"
            " AND kind IN (" + marks + ")"
            " AND created_at >= :since"
            " ORDER BY id DESC LIMIT :limit"
        )
        with engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(sql_text(sql), params)]
        report["rows"] = len(rows)
        for row in rows:
            path = _resolve_local(row.get("local_path"))
            if path is None or not path.exists():
                report["skipped"] += 1
                continue
            try:
                from core.conversation.media import ensure_image_description

                text = ensure_image_description(str(path), engine=engine)
            except Exception as exc:  # noqa: BLE001
                report["failed"] += 1
                log.warning("media_describe_row_failed", row_id=int(row.get("id") or 0),
                            error=str(exc)[:200])
                continue
            if not str(text or "").strip():
                report["failed"] += 1
                continue
            _store_row_description(engine, int(row.get("id") or 0), text)
            report["described"] += 1
        if report["described"]:
            log.info("media_described_missing", **report)
    except Exception as exc:  # noqa: BLE001
        log.warning("media_describe_missing_failed", error=str(exc)[:200])
    return report


def _apply_ingest_resolve(values: dict, resolved: Path) -> dict:
    """把就地解析到的文件信息重算进待写行（不动其它字段）。"""
    rebuilt = _extract_values(resolved, kind=str(values.get("kind") or ""),
                              md5_hint=str(values.get("md5") or ""))
    try:
        meta = json.loads(str(values.get("meta_json") or "{}"))
        if not isinstance(meta, dict):
            meta = {}
    except Exception:  # noqa: BLE001
        meta = {}
    meta["reason"] = str(rebuilt.get("last_error") or "")
    meta["has_text"] = bool(rebuilt.get("text_excerpt"))
    meta["resolved_at_ingest"] = True
    merged = dict(values)
    for key in ("status", "md5", "size_bytes", "mime", "text_excerpt",
                "last_error", "resolved_at"):
        if key in rebuilt:
            merged[key] = rebuilt[key]
    merged["local_path"] = str(resolved)
    merged["attempts"] = 1
    merged["meta_json"] = json.dumps(meta, ensure_ascii=False)
    return merged


def register_media_rows(
    messages: Iterable[dict],
    *,
    platform: str,
    account_id: str,
    event_ids: Sequence[str] | None = None,
    engine: Any | None = None,
    max_rows: int = MAX_ROWS_PER_BATCH,
) -> int:
    """把一批入站消息里的媒体件写进 ``message_media``（best-effort，返回写入行数）。

    * 开关 off → 直接 0，不做任何 IO；
    * 幂等：同 ``(chat_id, kind)`` 且 ``event_id`` 或 ``msg_id`` 命中已存行 → 跳过；
    * 单批最多 ``max_rows`` 行，避免首次开启时把整段历史扫一遍；
    * 任何异常只记日志，不向上抛（调用方是同步主链路）。
    """
    if not media_store_enabled():
        return 0
    rows = [row for row in messages if isinstance(row, dict)]
    if not rows:
        return 0
    ids = [str(value or "") for value in (event_ids or [])]
    written = 0
    skipped = 0
    failed = 0
    try:
        write_engine = engine if engine is not None else _meta_engine()
    except Exception as exc:
        log.warning("media_registry_engine_failed", error=str(exc)[:200])
        return 0
    now = _now()
    for index, raw in enumerate(rows):
        if not kind_of_row(raw):
            continue
        if written >= max(0, int(max_rows)):
            break
        event_id = ids[index] if index < len(ids) else ""
        values = None
        try:
            values = _build_values(
                raw,
                platform=platform,
                account_id=account_id,
                event_id=event_id,
                now=now,
            )
            if (
                not str(values.get("local_path") or "")
                and values.get("kind") in _INGEST_RESOLVE_KINDS
            ):
                resolved = _resolve_at_ingest(raw, now=now)
                if resolved is not None:
                    values = _apply_ingest_resolve(values, resolved)
            with write_engine.begin() as conn:
                if _row_exists(
                    conn,
                    chat_id=values["chat_id"],
                    kind=values["kind"],
                    event_id=values["event_id"],
                    msg_id=int(values["msg_id"] or 0),
                ):
                    skipped += 1
                    continue
                conn.execute(sql_text(_INSERT_SQL), values)
                written += 1
        except Exception as exc:
            failed += 1
            log.warning(
                "media_row_failed",
                chat_id=str(raw.get("chat_id") or "")[:64],
                kind=(values or {}).get("kind", ""),
                error=str(exc)[:200],
            )
    if written or skipped or failed:
        log.info(
            "media_registry_written",
            rows=len(rows),
            written=written,
            skipped=skipped,
            failed=failed,
            event_ids=len(ids),
        )
    return written


# ---------------------------------------------------------------------------
# task-26：账号库 resolved -> meta 库 missing 的回填桥
#
# 背景：`scheduler/main.py` 每轮 sync 会跑 `media_registry.resolve_pending()`，
# 它只在**账号库**的 message_media 上把 pending 行标成 resolved/missing 并写
# local_path；meta 库（本模块写的表）拿不到这个结果，于是「入站时路径还没解析」
# 的行会永远停在 missing。桥只处理「本轮刚 resolved」的账号行，且只更新 meta 里
# 仍为 missing 的行（ok 的行绝不动）。
# ---------------------------------------------------------------------------


def _extract_values(path: Path, *, kind: str, md5_hint: str = "") -> dict:
    """对一个已存在的文件重跑抽取，返回可直接写库的字段（不查库）。"""
    started = time.perf_counter()
    extracted = extract_media_text(path, kind=kind)
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    md5 = str(md5_hint or "").strip() or md5_of_file(path)
    full_text = str(extracted.get("text") or "")
    excerpt = full_text[:TEXT_EXCERPT_MAX]
    full_path = _write_full_text(md5, full_text) if len(full_text) > TEXT_EXCERPT_MAX else ""
    meta = {
        "reason": str(extracted.get("reason") or ""),
        "full_text_path": full_path,
        "elapsed_ms": elapsed_ms,
        "has_text": bool(excerpt),
        "backfilled_from": "account_db",
    }
    return {
        "status": str(extracted.get("status") or "missing"),
        "md5": md5,
        "size_bytes": int(extracted.get("size_bytes") or 0),
        "mime": mime_of_row({"media_path": path.name}, path, extracted),
        "text_excerpt": excerpt,
        "last_error": str(extracted.get("reason") or ""),
        "meta_json": json.dumps(meta, ensure_ascii=False),
        "resolved_at": _now() if extracted.get("status") == "ok" else None,
    }


def _mark_backfill_failure(
    engine: Any,
    row: dict,
    *,
    platform: str,
    account_id: str,
    reason: str,
) -> None:
    """账号行已 resolved 但文件取不到时，把原因落进 meta 的 missing 行（best effort）。

    只碰 ``status='missing'`` 的行；失败只 warning，不影响 sync pass。
    """
    sql = (
        "UPDATE " + TABLE + " SET last_error = :reason,"
        " attempts = COALESCE(attempts, 0) + 1"
        " WHERE chat_id = :chat_id AND msg_id = :msg_id AND status = 'missing'"
    )
    params = {
        "reason": str(reason or "")[:400],
        "chat_id": str(row.get("chat_id") or ""),
        "msg_id": _int_or_zero(row.get("msg_id")),
    }
    if platform:
        sql += " AND platform = :platform"
        params["platform"] = str(platform)
    if account_id:
        sql += " AND account_id = :account_id"
        params["account_id"] = str(account_id)
    try:
        with engine.begin() as conn:
            conn.execute(sql_text(sql), params)
    except Exception as exc:  # noqa: BLE001
        log.warning("media_meta_backfill_reason_failed", error=str(exc)[:200])


def _recent_resolved_rows(engine: Any, limit: int) -> list[dict]:
    """账号库里最近 resolved 的媒体行（只读，失败返回空列表）。"""
    sql = (
        "SELECT msg_id, chat_id, kind, local_path FROM message_media"
        " WHERE status = 'resolved' AND local_path IS NOT NULL AND local_path <> ''"
        " ORDER BY resolved_at DESC, id DESC LIMIT :limit"
    )
    try:
        with engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(sql_text(sql), {"limit": int(limit)})]
    except Exception as exc:  # noqa: BLE001
        log.warning("media_meta_backfill_query_failed", error=str(exc)[:200])
        return []


def _resolve_local(path_text: str) -> Path | None:
    raw = str(path_text or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = repo_root() / candidate
    return candidate


def backfill_media_from_account(
    engine: Any,
    *,
    platform: str = "",
    account_id: str = "",
    limit: int = 40,
    now: datetime | None = None,
    meta_engine: Any | None = None,
) -> dict:
    """把账号库最近 resolved 的媒体行补进 meta 表（幂等、不抛、只在开关非 off 时工作）。

    * 只更新 meta 里 ``status='missing'`` 的行——已 ``ok`` 的行不覆盖；
    * 每轮最多 ``limit`` 行，与 ``resolve_pending(limit=40)`` 同批；
    * 写完重跑抽取并整体重算：local_path/status/mime/size_bytes/md5/text_excerpt/meta_json；
    * 同一行重复回填结果不变（第二次 updated=0）；任何异常只 warning。
    """
    report = {"rows": 0, "updated": 0, "extracted": 0, "failed": 0}
    if not media_store_enabled():
        return report
    try:
        account_rows = _recent_resolved_rows(engine, limit)
        report["rows"] = len(account_rows)
        if not account_rows:
            return report
        write_engine = meta_engine if meta_engine is not None else _meta_engine()
        stamp = now or _now()
        for row in account_rows:
            try:
                path = _resolve_local(row.get("local_path"))
                if path is None or not path.exists():
                    report["failed"] += 1
                    reason = "media file gone at backfill: %s" % (path.name if path else "")
                    _mark_backfill_failure(
                        write_engine,
                        row,
                        platform=str(platform or ""),
                        account_id=str(account_id or ""),
                        reason=reason,
                    )
                    log.warning(
                        "media_meta_backfill_row",
                        chat_id=str(row.get("chat_id") or "")[:64],
                        msg_id=str(row.get("msg_id") or ""),
                        reason="file_gone",
                    )
                    continue
                values = _extract_values(path, kind=str(row.get("kind") or ""))
                values["local_path"] = str(path)
                values["original_name"] = path.name[:300]
                values["resolved_at"] = values["resolved_at"] or stamp
                values["platform"] = str(platform or "")
                values["account_id"] = str(account_id or "")
                values["chat_id"] = str(row.get("chat_id") or "")
                values["msg_id"] = _int_or_zero(row.get("msg_id"))
                sql = (
                    "UPDATE " + TABLE + " SET status = :status, mime = :mime,"
                    " size_bytes = :size_bytes, md5 = :md5, text_excerpt = :text_excerpt,"
                    " local_path = :local_path, last_error = :last_error,"
                    " original_name = COALESCE(NULLIF(original_name, ''), :original_name),"
                    " meta_json = :meta_json, resolved_at = :resolved_at,"
                    " attempts = COALESCE(attempts, 0) + 1"
                    " WHERE chat_id = :chat_id AND msg_id = :msg_id AND status = 'missing'"
                )
                if values["platform"]:
                    sql += " AND platform = :platform"
                if values["account_id"]:
                    sql += " AND account_id = :account_id"
                with write_engine.begin() as conn:
                    result = conn.execute(sql_text(sql), values)
                    changed = int(getattr(result, "rowcount", 0) or 0)
                if changed:
                    report["updated"] += changed
                    report["extracted"] += 1
            except Exception as exc:  # noqa: BLE001
                report["failed"] += 1
                log.warning(
                    "media_meta_backfill_row",
                    chat_id=str(row.get("chat_id") or "")[:64],
                    msg_id=str(row.get("msg_id") or ""),
                    error=str(exc)[:200],
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("media_meta_backfill_failed", error=str(exc)[:200])
    finally:
        if report["rows"]:
            log.info("media_meta_backfill", **report)
    return report



__all__ = [
    "AUDIO_SUFFIXES",
    "FILE_SUFFIXES",
    "IMAGE_SUFFIXES",
    "MAX_ROWS_PER_BATCH",
    "PASSIVE_SUFFIXES",
    "READ_MAX_BYTES",
    "TEXT_EXCERPT_MAX",
    "VIDEO_SUFFIXES",
    "backfill_media_from_account",
    "extract_media_text",
    "full_text_dir",
    "kind_of_row",
    "md5_of_file",
    "media_path_from_row",
    "media_store_enabled",
    "MIME_BY_SUFFIX",
    "media_store_mode",
    "mime_of_row",
    "original_name_from_row",
    "register_media_rows",
    "repo_root",
]
