"""Cross-message media state and reference resolution.

This module is the data layer behind "the user sent an image, then referred to
it" and "the user asked about an image before it arrived".  It is intentionally
pure with respect to platforms: callers pass already-normalised ids and paths.

Two stores are kept in process memory:

* :class:`RecentMediaStore` -- images that already arrived, per chat, with a
  TTL.  Handles image-first ordering and same-batch text-first ordering.
* :class:`PendingMediaStore` -- questions that referred to an image which had
  not arrived yet.  When the image finally lands, the scheduler can replay the
  original question together with the image.

Connecting the stores to the scheduler/conversational agent is intentionally
done at the edges (``scheduler.main`` and ``agents.conversational``); the rules
here stay testable without a DB, LLM or platform.
"""

from __future__ import annotations
import os

import base64
import hashlib
import json
import mimetypes
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import structlog
from safelog import warn
from core.infra.envflags import flag, text

log = structlog.get_logger()

DEFAULT_MEDIA_TTL_SECONDS = 600.0
PAT_MEDIA_TTL_SECONDS = 180.0
NAME_MEDIA_TTL_SECONDS = 120.0
MAX_MEDIA_PER_CHAT = 20
MAX_ADJACENT_MESSAGE_GAP = 5

_MEDIA_TYPES = frozenset({"image", "sticker", "emoji"})
_MEDIA_CONTENT_PREFIXES = ("[图片]", "[表情包")

#: 整条正文只有一个没描述的媒体占位符。判定层（core/context、core/routing）不该认
#: 「表情包」这类平台字面量，要判这种消息就调这里（2026-11-22 从 experience_commit 下沉）。
_PLACEHOLDER_ONLY = frozenset({"[表情包]", "[图片]", "[文件]", "[语音]", "[视频]", "[拍了拍]"})


def is_placeholder_only(text: str) -> bool:
    """正文是空的，或只有一个没描述的媒体占位符。"""
    body = " ".join(str(text or "").split())
    return not body or body in _PLACEHOLDER_ONLY

# Explicit references to "the image".  High confidence: a later image may be
# attached even across several unrelated messages within the TTL.
_EXPLICIT_MEDIA_MARKERS = (
    "这张图", "那张图", "这个图", "这张照片", "那张照片",
    "刚才那张", "刚才的图", "刚发的图", "我刚才发的图", "我发的图", "发的图",
    "上面的图", "上面那张", "下面那张", "图片呢", "图呢",
    "看图", "看看这张", "看下这张", "看这张", "看一下这张",
    # emoji / sticker references
    "这个表情", "那个表情", "刚才那个表情", "刚才的表情", "发的表情",
    "我发的表情", "这个表情包", "那个表情包", "表情呢", "表情包呢",
    "看看这个表情", "看下这个表情", "看这个表情", "说说这个表情",
)
# Weaker "look at this" without an image noun.  Only used when the most recent
# chat media is from the same sender and very fresh.
_LOOK_MARKERS = ("看看", "看下", "看这个", "你看看", "看一下")

_PAT_RE = re.compile(r'"([^"]+)"\s*拍了拍')
_STICKER_MD5_RE = re.compile(r"\[表情包[:：]([0-9a-fA-F]{32})\]")


def extract_sticker_md5(value: Any) -> str:
    """Extract a 32-hex sticker md5 from a message dict / row / content."""
    if isinstance(value, Mapping):
        raw = value.get("sticker_id") or value.get("emoji_md5") or ""
        if not raw:
            extra = value.get("extra")
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except Exception:
                    warn("core/conversation/media.py:76 Exception", err=repr(__import__("sys").exc_info()[1]))
                    extra = {}
            if isinstance(extra, Mapping):
                raw = extra.get("emoji_md5") or extra.get("md5") or ""
        if not raw:
            raw = extract_sticker_md5(value.get("content") or "")
        return str(raw or "").strip().lower()
    match = _STICKER_MD5_RE.search(str(value or ""))
    return match.group(1).lower() if match else ""

@dataclass(frozen=True)
class QuotedMedia:
    """从「[引用 作者: 内容]」前缀拆出的引用信息（2026-09-23 修复）。"""

    author: str = ""
    content: str = ""
    md5: str = ""  # 引用内容里可提取的 32-hex 表情包 md5
    has_media_hint: bool = False  # 引用内容像媒体（[媒体消息]/[图片]/[表情包]…）


_HEX32_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])")
_QUOTED_MEDIA_HINTS = (
    "[媒体消息", "[图片", "[表情包", "[动画表情", "[视频", "[语音", "[文件", "[链接",
)


def extract_quoted_media(value: Any) -> QuotedMedia:
    """解析消息开头的 ``[引用 作者: 内容]``，返回作者/内容/可提取的 md5。

    2026-10-11 收口：**格式解析只在 canonical（``core.conversation.addressing``）
    一处**。这里原先自己写了一份 ``_QUOTED_PREFIX_RE``，与 canonical 口径分叉，
    代价是一次**真实的漏修**——2026-09-23 修「引用明确指向媒体时不再拿最近一张
    充数」时，副本正则要求作者 ≥1 字符，**匹配不上 ``[引用: [媒体消息]]``（空作者）**，
    那 12 条于是落到"拿最近一张充数"的旧分支：修复只覆盖了一半形态。
    实测 561 条真实引用行：canonical 覆盖 504 / 副本覆盖 492（**副本是严格子集**）、
    作者名 492/492 一致、md5 一处不差。

    微信的引用行里带原始定位串（``wxid…:srv_id:0:<md5>::0``），表情包的 md5
    可以直接抠出来——这正是"她要识别的是被引用的那张表情包"的抓手。
    """
    from core.conversation.addressing import quote_of, quoted_body

    quoted = quote_of(value)
    if not quoted.present:
        return QuotedMedia()
    author = quoted.author_name if quoted.resolvable else ""
    content = quoted_body(value)
    md5 = ""
    hex_match = _HEX32_RE.search(content)
    if hex_match:
        md5 = hex_match.group(1).lower()
    has_hint = bool(md5) or content.startswith(_QUOTED_MEDIA_HINTS)
    return QuotedMedia(author=author, content=content, md5=md5, has_media_hint=has_hint)


def is_sticker_message(
    msg_type_name: str | None,
    content: str = "",
    sticker_id: str = "",
) -> bool:
    kind = str(msg_type_name or "").strip().lower()
    if kind in ("sticker", "emoji"):
        return True
    if str(content or "").lstrip().startswith("[表情包"):
        return True
    return bool(sticker_id) and str(content or "").lstrip().startswith("[")


@dataclass(frozen=True)
class MediaReference:
    matched: bool = False
    marker: str = ""
    confidence: str = "none"  # high / medium / none

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.matched


def detect_media_reference(text: str) -> MediaReference:
    """Return whether *text* explicitly points at a recent image."""
    value = (text or "").strip()
    if not value:
        return MediaReference()
    for marker in _EXPLICIT_MEDIA_MARKERS:
        if marker in value:
            return MediaReference(True, marker, "high")
    for marker in _LOOK_MARKERS:
        if marker in value:
            return MediaReference(True, marker, "medium")
    return MediaReference()


def is_media_message(
    msg_type_name: str | None,
    content: str = "",
    media_path: str = "",
) -> bool:
    """True when a message row *looks* like image/sticker media.

    ``media_path`` is not required: content placeholders (``[图片]``) let
    callers classify a message even before decryption/caching succeeded.
    """
    kind = str(msg_type_name or "").strip().lower()
    if kind in _MEDIA_TYPES:
        return True
    text = (content or "").strip()
    if text.startswith(_MEDIA_CONTENT_PREFIXES):
        return True
    return bool(media_path) and bool(text) and text.startswith("[")


_PAT_NAME_PLACEHOLDERS = {"", "unknown", "?", "-", "none", "null"}


def pat_identity(
    content: str,
    sender_id: str,
    sender_name: str,
) -> tuple[str, str]:
    """Keep the real ``sender_id`` when extracting a pat's display name.

    Old scheduler code cleared ``sender_id`` for pat messages, which broke every
    later attempt to match the pat back to a recent image / conversation window.
    """
    if "拍了拍" in (content or ""):
        match = _PAT_RE.match((content or "").lstrip())
        if match:
            name = match.group(1).strip()
            current = str(sender_name or "").strip().lower()
            if name and current in _PAT_NAME_PLACEHOLDERS:
                sender_name = name
    return sender_id, sender_name


def parse_timestamp(value: Any) -> datetime | None:
    """Best-effort parse of message ``created_at`` values."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00").replace("/", "-"))
    except ValueError:
        warn("core/conversation/media.py:221 ValueError", err=repr(__import__("sys").exc_info()[1]))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            warn("core/conversation/media.py:226 ValueError", err=repr(__import__("sys").exc_info()[1]))
            continue
    return None


def media_fingerprint(path: str) -> str:
    """Stable id for a media file, used for description caches/dedupe."""
    try:
        data = Path(path).read_bytes()
    except Exception:
        warn("core/conversation/media.py:235 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    return hashlib.sha1(data).hexdigest()


# ---------------------------------------------------------------------------
# image integrity -- the single verdict on "can this file be used as a picture?"
# ---------------------------------------------------------------------------
# Why this exists (2026-10-02 「她看不见图」): the vision provider answered
# ``400 You have uploaded an unsupported image`` for every cached picture, and the
# log said ``llm_vision_all_failed`` three times in one evening with zero
# successes.  The files were truncated: ``data/image_cache/*.jpg`` had lost the
# trailing 16 bytes of the JPEG (including the ``FFD9`` end marker), because the
# WeChat ``.dat`` container carries one more 16-byte block than its header
# declares and the decoder stopped at the declared length.  Nothing checked the
# artifact, so a truncated file was published as ``resolved`` and handed to the
# model, which rejected it -- the failure the user saw as "图片没读出来".
#
# Two different questions are asked of the same artifact, and they have
# different answers (measured on this machine: 126 of 330 cached jpgs were
# missing the end marker, but 102 of those still decoded to byte-identical
# pixels and the provider accepted them):
#
# * ``is_complete_image`` -- is the artifact byte-complete?  ("is the tail there")
# * ``is_image_readable`` -- can it actually be used as a picture?  ("will the
#   provider accept it")  This is the one a status field or a model call may
#   rely on.
#
# Both read the one implementation below; nobody re-sniffs the bytes.
INTEGRITY_OK = "ok"                    # complete and decodable
INTEGRITY_TRUNCATED = "truncated"      # end marker missing, but still decodable
INTEGRITY_UNDECODABLE = "undecodable"  # end marker missing and undecodable
INTEGRITY_CORRUPT = "corrupt"          # not truncated, still undecodable
INTEGRITY_MISSING = "missing"          # no such file, or unreadable

#: Verdicts that a status field or a provider call may treat as usable.
READABLE_VERDICTS = frozenset({INTEGRITY_OK, INTEGRITY_TRUNCATED})

_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"IEND\xaeB`\x82"
_GIF_HEADS = (b"GIF87a", b"GIF89a")
_GIF_TRAILER = b"\x3b"


def _structural_tail_ok(head: bytes, tail: bytes, size: int) -> bool | None:
    """Whether the format's own end marker is present; ``None`` = cannot judge.

    ``head``/``tail`` are the first and last bytes of the file, so a page of
    pixels never has to be read to answer this.  ``None`` means "this is not a
    container we parse structurally" -- the decode check then decides alone.
    """
    if head.startswith(_JPEG_SOI):
        return tail.endswith(_JPEG_EOI)
    if head.startswith(_PNG_SIG):
        # PNG ends with the IEND chunk: length(0) + type + CRC.
        return tail.endswith(_PNG_IEND)
    if head.startswith(_GIF_HEADS):
        return tail.endswith(_GIF_TRAILER)
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        # The RIFF size field counts everything after itself, so it must equal
        # size - 8 exactly; a short file is a torn write.
        return int.from_bytes(head[4:8], "little") + 8 == size
    if head.startswith(b"BM"):
        return int.from_bytes(head[2:6], "little") == size
    return None


def _decodes_as_image(path: Path) -> bool:
    """Full decode, not just a header peek.

    ``Image.open`` reads the header lazily and happily opens a file whose pixel
    data is cut off; only ``load()`` forces the decoder through the whole image
    and raises on a truncated scan.  Without Pillow the answer is ``True`` so the
    structural verdict stands alone rather than every image being refused.
    """
    try:
        from PIL import Image  # type: ignore
    except Exception:  # noqa: BLE001
        warn("core/conversation/media.py:314 Exception", err=repr(__import__("sys").exc_info()[1]))
        return True
    try:
        with Image.open(path) as image:
            image.load()
        return True
    except Exception:  # noqa: BLE001
        warn("core/conversation/media.py:320 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def image_integrity(path: str) -> str:
    """Classify one image file.  The **only** implementation of this judgement.

    Returns one of :data:`INTEGRITY_OK` / ``TRUNCATED`` / ``UNDECODABLE`` /
    ``CORRUPT`` / ``MISSING``.  Never raises: a file we cannot even stat is
    ``MISSING``, which is also what a caller wants to hear.
    """
    candidate = Path(str(path or "").strip())
    if not str(path or "").strip():
        return INTEGRITY_MISSING
    try:
        size = candidate.stat().st_size
    except OSError:
        warn("core/conversation/media.py:336 OSError", err=repr(__import__("sys").exc_info()[1]))
        return INTEGRITY_MISSING
    if not candidate.is_file() or size <= 0:
        return INTEGRITY_MISSING

    tail_ok: bool | None = None
    try:
        with candidate.open("rb") as handle:
            head = handle.read(16)
            handle.seek(max(0, size - 16))
            tail = handle.read()
        tail_ok = _structural_tail_ok(head, tail, size)
    except OSError:
        warn("core/conversation/media.py:348 OSError", err=repr(__import__("sys").exc_info()[1]))
        return INTEGRITY_MISSING

    if not _decodes_as_image(candidate):
        # A missing end marker is the measured cause on this machine, and it is
        # the verdict that tells a retry pass "the file may still be arriving".
        return INTEGRITY_UNDECODABLE if tail_ok is False else INTEGRITY_CORRUPT
    if tail_ok is False:
        return INTEGRITY_TRUNCATED
    return INTEGRITY_OK


def is_complete_image(path: str) -> bool:
    """Whether *path* is a byte-complete, decodable image.

    The strict judgement: the format's end marker is present **and** the pixels
    decode.  Anything less is not called complete -- see
    :func:`is_image_readable` for the weaker "we can still use it" question.
    """
    return image_integrity(path) == INTEGRITY_OK


def is_image_readable(path: str) -> bool:
    """Whether *path* can actually be used as a picture (provider will accept it).

    True for a complete file and for one whose only defect is a missing end
    marker while every pixel still decodes -- measured against the live vision
    provider, which answers ``200`` for the latter and ``400 unsupported image``
    for a file that cannot be decoded at all.
    """
    return image_integrity(path) in READABLE_VERDICTS


# ---------------------------------------------------------------------------
# media type -- real bytes, never the extension
# ---------------------------------------------------------------------------
# The vision payload used to be labelled from ``mimetypes.guess_type`` (the file
# name) and the downscale branch hardcoded ``image/jpeg``.  A PNG or a GIF that
# the owner sent therefore went out *declared* as a JPEG: the bytes and the type
# disagreed, which is a lie the provider is entitled to reject.  The type is now
# read from the same magic bytes the integrity judge reads, so the two can never
# describe different files.
MIME_JPEG = "image/jpeg"
MIME_PNG = "image/png"
MIME_GIF = "image/gif"
MIME_WEBP = "image/webp"

#: Extension -> media type, used **only** as corroboration for a container whose
#: magic bytes Pillow reports but this list does not cover.
_EXTENSION_MIME = {
    ".jpg": MIME_JPEG, ".jpeg": MIME_JPEG, ".jpe": MIME_JPEG,
    ".png": MIME_PNG, ".gif": MIME_GIF, ".webp": MIME_WEBP,
}

_MAGIC_MIME = (
    (b"\xff\xd8\xff", MIME_JPEG),
    (b"\x89PNG\r\n\x1a\n", MIME_PNG),
    (b"GIF87a", MIME_GIF),
    (b"GIF89a", MIME_GIF),
)


def image_mime(path) -> str:
    """The media type of *path* read from its bytes, or "" when unknown.

    Never guesses: an unknown format returns ``""`` so callers degrade honestly
    instead of announcing a JPEG the provider will reject.  Pillow is consulted
    after the magic bytes because it names containers this list does not carry
    (a BMP, a TIFF) -- and for one of those the extension is then a corroborating
    fact rather than the only evidence.
    """
    candidate = Path(str(path or "").strip())
    try:
        with candidate.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        warn("core/conversation/media.py:423 OSError", err=repr(__import__("sys").exc_info()[1]))
        return ""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return MIME_WEBP
    for magic, mime in _MAGIC_MIME:
        if head.startswith(magic):
            return mime
    try:
        from PIL import Image  # type: ignore

        with Image.open(candidate) as image:
            detected = str(image.format or "").upper()
    except Exception:  # noqa: BLE001
        warn("core/conversation/media.py:435 Exception", err=repr(__import__("sys").exc_info()[1]))
        detected = ""
    if detected == "WEBP":
        return MIME_WEBP
    if detected in ("JPEG", "JPG"):
        return MIME_JPEG
    if detected in ("PNG", "GIF"):
        return f"image/{detected.lower()}"
    if detected:
        # Some other format Pillow knows: believe the extension only as
        # corroboration, so an unknown container is never announced as JPEG.
        return _EXTENSION_MIME.get(candidate.suffix.lower(), "")
    return ""


def media_path_to_data_uri(
    path: str,
    *,
    max_bytes: int = 6_000_000,
    max_side: int = 1600,
    # 2026-10-25：deepseek 那套网关对"超大 base64 图"会直接 400
    # （Failed to load image or audio file），所以对外发送前先压到这个尺寸以内
    shrink_side: int = 768,
    shrink_quality: int = 70,
) -> str:
    """Read a local image and return a ``data:`` URI for ``chat_vision``.

    Large images are downscaled/re-encoded to JPEG when Pillow is available.
    Any failure returns ``""`` so callers degrade to a text-only reply.

    A file that cannot be decoded is a failure like any other: it returns ``""``
    and the caller's existing "I could not read that picture" path runs, instead
    of the provider answering ``400 unsupported image`` for a call that could
    never have succeeded.  This is the one funnel every vision call goes through,
    so the check lives here rather than at each call site.
    """
    raw_path = (path or "").strip()
    if not raw_path:
        return ""
    candidate = Path(raw_path)
    if not candidate.exists():
        # Image cache paths are usually stored relative to the project root;
        # tolerate callers passing just the file name.
        alt = Path("data/image_cache") / candidate.name
        if alt.exists():
            candidate = alt
        else:
            return ""
    verdict = image_integrity(str(candidate))
    if verdict not in READABLE_VERDICTS:
        log.warning("media_unreadable_image", verdict=verdict,
                    path=candidate.name, size=_safe_size(candidate))
        return ""
    try:
        data = candidate.read_bytes()
    except Exception:
        warn("core/conversation/media.py:486 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    if not data:
        return ""
    # The media type is read off the bytes, never off the file name: a PNG named
    # ``x.jpg`` used to be announced as ``image/jpeg``, so the declared type and
    # the payload disagreed.  An unidentifiable format gets no type at all rather
    # than a fabricated one.
    mime = image_mime(candidate)
    # 2026-10-25：网关对 >~200KB 的 base64 图直接 400，所以小图才走原样直出，其它一律重编码
    if len(data) <= 200_000 and mime:
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    try:
        from PIL import Image  # type: ignore

        with Image.open(candidate) as image:
            image = image.convert("RGB")
            if max(image.size) > shrink_side:
                image.thumbnail((shrink_side, shrink_side))
            out = __import__("io").BytesIO()
            image.save(out, format="JPEG", quality=shrink_quality)
            # Only this branch produces JPEG bytes, and it is the only place
            # ``image/jpeg`` is a fact rather than a guess.
            encoded = base64.b64encode(out.getvalue()).decode("ascii")
            return f"data:{MIME_JPEG};base64,{encoded}"
    except Exception:
        warn("core/conversation/media.py:510 Exception", err=repr(__import__("sys").exc_info()[1]))
        if mime and len(data) <= max_bytes:
            return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        return ""


def _safe_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        warn("core/conversation/media.py:519 OSError", err=repr(__import__("sys").exc_info()[1]))
        return 0


@dataclass(frozen=True)
class MediaItem:
    platform: str
    account_id: str
    chat_id: str
    sender_id: str
    sender_name: str
    msg_id: str
    media_path: str = ""
    kind: str = "image"
    created_at: datetime | None = None
    sticker_id: str = ""
    description: str = ""


@dataclass(frozen=True)
class PendingMediaRequest:
    platform: str
    account_id: str
    chat_id: str
    sender_id: str
    sender_name: str
    question: str
    trigger: str = ""
    created_at: datetime | None = None
    ttl_seconds: float = DEFAULT_MEDIA_TTL_SECONDS
    ack_sent: bool = False
    kind: str = "question"  # "question" (text-first reference) / "pat"

    @property
    def expired(self) -> bool:
        return False  # replaced by store checks; kept for API symmetry


class RecentMediaStore:
    """Ring buffer of arrived media, newest last, keyed by chat."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_MEDIA_TTL_SECONDS,
        max_per_chat: int = MAX_MEDIA_PER_CHAT,
    ) -> None:
        self._lock = threading.RLock()
        self._ttl = float(ttl_seconds)
        self._max_per_chat = int(max_per_chat)
        self._items: dict[tuple[str, str, str], deque[MediaItem]] = {}

    @staticmethod
    def _key(platform: str, account_id: str, chat_id: str) -> tuple[str, str, str]:
        return (str(platform or ""), str(account_id or ""), str(chat_id or ""))

    def record(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        sender_id: str,
        sender_name: str = "",
        msg_id: str = "",
        media_path: str = "",
        kind: str = "image",
        sticker_id: str = "",
        description: str = "",
        now: datetime | None = None,
    ) -> MediaItem | None:
        path = str(media_path or "").strip()
        sticker = str(sticker_id or "").strip().lower()
        desc = str(description or "").strip()
        if not any((path, sticker, desc)):
            return None
        now = now or datetime.now()
        key = self._key(platform, account_id, chat_id)
        item = MediaItem(
            platform=key[0], account_id=key[1], chat_id=key[2],
            sender_id=str(sender_id or ""), sender_name=str(sender_name or ""),
            msg_id=str(msg_id or ""), media_path=path,
            kind=str(kind or "image"), created_at=now,
            sticker_id=sticker, description=desc,
        )
        with self._lock:
            queue = self._items.setdefault(
                key, deque(maxlen=self._max_per_chat),
            )
            if item.msg_id:
                for index, existing in enumerate(queue):
                    if existing.msg_id and existing.msg_id == item.msg_id:
                        if (
                            (not existing.media_path and item.media_path)
                            or (not existing.description and item.description)
                            or (not existing.sticker_id and item.sticker_id)
                        ):
                            queue[index] = item
                            return item
                        return existing
            queue.append(item)
            self._prune_locked(now)
        return item

    def _prune_locked(self, now: datetime) -> None:
        cutoff = now.timestamp() - self._ttl
        for queue in list(self._items.values()):
            while queue:
                created = queue[0].created_at or now
                if created.timestamp() >= cutoff:
                    break
                queue.popleft()

    def list_items(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        sender_id: str = "",
        now: datetime | None = None,
    ) -> list[MediaItem]:
        now = now or datetime.now()
        key = self._key(platform, account_id, chat_id)
        with self._lock:
            self._prune_locked(now)
            items = list(self._items.get(key, ()))
        if sender_id:
            items = [item for item in items if item.sender_id == str(sender_id)]
        return list(reversed(items))

    def latest(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        sender_id: str = "",
        now: datetime | None = None,
    ) -> MediaItem | None:
        items = self.list_items(
            platform=platform, account_id=account_id, chat_id=chat_id,
            sender_id=sender_id, now=now,
        )
        return items[0] if items else None

    def age_seconds(self, item: MediaItem, now: datetime | None = None) -> float:
        now = now or datetime.now()
        created = item.created_at or now
        return max(0.0, (now - created).total_seconds())

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class PendingMediaStore:
    """Questions that referred to an image which had not arrived yet."""

    def __init__(self, ttl_seconds: float = DEFAULT_MEDIA_TTL_SECONDS) -> None:
        self._lock = threading.RLock()
        self._ttl = float(ttl_seconds)
        self._items: dict[tuple[str, str, str, str], PendingMediaRequest] = {}

    @staticmethod
    def _key(platform: str, account_id: str, chat_id: str,
             sender_id: str) -> tuple[str, str, str, str]:
        return (str(platform or ""), str(account_id or ""),
                str(chat_id or ""), str(sender_id or ""))

    def add_or_replace(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        sender_id: str,
        sender_name: str = "",
        question: str,
        trigger: str = "",
        kind: str = "question",
        now: datetime | None = None,
        ttl_seconds: float | None = None,
    ) -> tuple[PendingMediaRequest, bool]:
        """Return ``(request, created)``; ``created`` is False when replacing."""
        now = now or datetime.now()
        key = self._key(platform, account_id, chat_id, sender_id)
        with self._lock:
            existing = self._items.get(key)
            created = existing is None or self._expired(existing, now)
            request = PendingMediaRequest(
                platform=key[0], account_id=key[1], chat_id=key[2],
                sender_id=key[3], sender_name=str(sender_name or ""),
                question=str(question or ""),
                trigger=str(trigger or ""),
                created_at=now,
                ttl_seconds=float(ttl_seconds or self._ttl),
                ack_sent=bool(existing and existing.ack_sent and not created),
                kind=str(kind or "question"),
            )
            self._items[key] = request
            return request, created

    def _expired(self, request: PendingMediaRequest, now: datetime) -> bool:
        created = request.created_at or now
        return (now - created).total_seconds() > float(request.ttl_seconds)

    def get(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        sender_id: str,
        sender_name: str = "",
        now: datetime | None = None,
    ) -> PendingMediaRequest | None:
        now = now or datetime.now()
        with self._lock:
            key = self._key(platform, account_id, chat_id, sender_id)
            request = self._items.get(key)
            if request is None and sender_name:
                request = self._find_by_name_locked(
                    platform, account_id, chat_id, sender_name,
                )
                if request is not None:
                    key = self._key(
                        request.platform, request.account_id,
                        request.chat_id, request.sender_id,
                    )
            if request is None:
                return None
            if self._expired(request, now):
                self._items.pop(key, None)
                return None
            return request

    def _find_by_name_locked(
        self, platform: str, account_id: str, chat_id: str, sender_name: str,
    ) -> PendingMediaRequest | None:
        prefix = (str(platform or ""), str(account_id or ""), str(chat_id or ""))
        name = str(sender_name or "").strip()
        for key, request in self._items.items():
            if key[:3] != prefix:
                continue
            if _same_sender_values(
                key[3], request.sender_name, "", name,
            ):
                return request
        return None

    def pop_match(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        sender_id: str,
        sender_name: str = "",
        now: datetime | None = None,
    ) -> PendingMediaRequest | None:
        now = now or datetime.now()
        with self._lock:
            key = self._key(platform, account_id, chat_id, sender_id)
            request = self._items.pop(key, None)
            if request is None and sender_name:
                request = self._find_by_name_locked(
                    platform, account_id, chat_id, sender_name,
                )
                if request is not None:
                    key = self._key(
                        request.platform, request.account_id,
                        request.chat_id, request.sender_id,
                    )
                    self._items.pop(key, None)
            if request is None:
                return None
            if self._expired(request, now):
                return None
            return request

    def drop(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        sender_id: str,
        sender_name: str = "",
        kind: str = "",
        now: datetime | None = None,
    ) -> PendingMediaRequest | None:
        """Remove a matching request (used to cancel a stale pat expectation)."""
        now = now or datetime.now()
        with self._lock:
            key = self._key(platform, account_id, chat_id, sender_id)
            request = self._items.get(key)
            if request is None and sender_name:
                request = self._find_by_name_locked(
                    platform, account_id, chat_id, sender_name,
                )
                if request is not None:
                    key = self._key(
                        request.platform, request.account_id,
                        request.chat_id, request.sender_id,
                    )
            if request is None:
                return None
            if kind and request.kind != kind:
                return None
            return self._items.pop(key, None)

    def mark_ack_sent(self, request: PendingMediaRequest) -> None:
        key = self._key(
            request.platform, request.account_id,
            request.chat_id, request.sender_id,
        )
        with self._lock:
            current = self._items.get(key)
            if current is request or (
                current is not None
                and current.created_at == request.created_at
                and current.question == request.question
            ):
                self._items[key] = PendingMediaRequest(
                    platform=current.platform,
                    account_id=current.account_id,
                    chat_id=current.chat_id,
                    sender_id=current.sender_id,
                    sender_name=current.sender_name,
                    question=current.question,
                    trigger=current.trigger,
                    created_at=current.created_at,
                    ttl_seconds=current.ttl_seconds,
                    ack_sent=True,
                    kind=current.kind,
                )

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


# ---------------------------------------------------------------------------
# process-wide accessors (scheduler and conversational agent share one view)
# ---------------------------------------------------------------------------

_recent_store: RecentMediaStore | None = None
_pending_store: PendingMediaStore | None = None
_store_lock = threading.RLock()


def get_recent_media_store() -> RecentMediaStore:
    global _recent_store
    with _store_lock:
        if _recent_store is None:
            _recent_store = RecentMediaStore()
        return _recent_store


def get_pending_media_store() -> PendingMediaStore:
    global _pending_store
    with _store_lock:
        if _pending_store is None:
            _pending_store = PendingMediaStore()
        return _pending_store


def reset_media_state() -> None:
    """Test helper: clear all process-wide media state."""
    global _recent_store, _pending_store, _description_cache
    with _store_lock:
        if _recent_store is not None:
            _recent_store.clear()
        if _pending_store is not None:
            _pending_store.clear()
        if _description_cache is not None:
            _description_cache.clear()
        _recent_store = None
        _pending_store = None
        _description_cache = None


# ---------------------------------------------------------------------------
# batch helpers used by the scheduler
# ---------------------------------------------------------------------------

def _same_sender_values(
    sender_id_a: str, sender_name_a: str,
    sender_id_b: str, sender_name_b: str,
) -> bool:
    """Match by id when both look real, otherwise fall back to display name."""
    id_a = str(sender_id_a or "").strip()
    id_b = str(sender_id_b or "").strip()
    if id_a and id_b and id_a != "unknown" and id_b != "unknown":
        return id_a == id_b
    name_a = str(sender_name_a or "").strip()
    name_b = str(sender_name_b or "").strip()
    if name_a and name_b:
        return name_a == name_b
    return id_a == id_b


def _chat_key(msg: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(msg.get("source") or msg.get("platform") or ""),
        str(msg.get("account_id") or ""),
        str(msg.get("chat_id") or ""),
    )


def _message_path(msg: Mapping[str, Any]) -> str:
    return str(msg.get("media_path") or "").strip()


def record_batch_media(
    messages: Sequence[Mapping[str, Any]],
    *,
    platform: str = "",
    account_id: str = "",
    description_lookup: Callable[[str], str] | None = None,
) -> int:
    """Record every media message in a sync batch into the recent store.

    Stickers/emoji are recorded even when their raw file cannot be converted
    to an image: their md5 (and cached description) is enough for later turns.
    """
    store = get_recent_media_store()
    count = 0
    for msg in messages:
        if not isinstance(msg, Mapping):
            continue
        path = _message_path(msg)
        sticker_id = extract_sticker_md5(msg)
        is_sticker = is_sticker_message(
            msg.get("msg_type_name"), msg.get("content") or "", sticker_id,
        )
        is_media = bool(path) and is_media_message(
            msg.get("msg_type_name"), msg.get("content") or "", path,
        )
        if not is_media and not is_sticker:
            continue
        description = ""
        if sticker_id and description_lookup is not None:
            try:
                description = str(description_lookup(sticker_id) or "")
            except Exception:
                warn("core/conversation/media.py:964 Exception", err=repr(__import__("sys").exc_info()[1]))
                description = ""
        source = str(msg.get("source") or platform or "")
        chat_id = str(msg.get("chat_id") or "")
        sender_id = str(msg.get("sender_id") or "")
        item = store.record(
            platform=source, account_id=account_id,
            chat_id=chat_id, sender_id=sender_id,
            sender_name=str(msg.get("sender_name") or ""),
            msg_id=str(msg.get("msg_id") or ""),
            media_path=path,
            kind=str(msg.get("msg_type_name") or "image"),
            sticker_id=sticker_id,
            description=description,
            # TTL is measured from arrival into this process, not from the
            # message's DB timestamp (backfills may be hours old).
            now=datetime.now(),
        )
        if item is not None:
            count += 1
    return count


def attach_batch_media(
    messages: Sequence[Mapping[str, Any]],
    *,
    persona_names: Iterable[str] = (),
    owner_ids: Iterable[str] = (),
    max_gap: int = MAX_ADJACENT_MESSAGE_GAP,
) -> dict[int, list[str]]:
    """Attach nearby images to triggered messages inside one sync batch.

    Works for both text-first and image-first orders.  Explicit image
    references may reach a few messages away; bare name/pat triggers require
    true adjacency (or the image already being the latest chat media, which is
    handled by :func:`resolve_recent_media` for cross-batch cases).
    """
    names = [str(name) for name in persona_names if str(name).strip()]
    owners = {str(owner) for owner in owner_ids if str(owner).strip()}
    by_chat: dict[tuple[str, str, str], list[int]] = {}
    for index, msg in enumerate(messages):
        if not isinstance(msg, Mapping):
            continue
        by_chat.setdefault(_chat_key(msg), []).append(index)

    attachments: dict[int, list[str]] = {}
    for indices in by_chat.values():
        def _is_candidate(index: int) -> bool:
            candidate = messages[index]
            path = _message_path(candidate)
            if path and is_media_message(
                candidate.get("msg_type_name"),
                candidate.get("content") or "",
                path,
            ):
                return True
            return is_sticker_message(
                candidate.get("msg_type_name"),
                candidate.get("content") or "",
                extract_sticker_md5(candidate),
            )

        media_indices = [index for index in indices if _is_candidate(index)]
        if not media_indices:
            continue
        for index in indices:
            msg = messages[index]
            if index in media_indices:
                continue
            content = str(msg.get("content") or "")
            sender_id = str(msg.get("sender_id") or "")
            msg_type = str(msg.get("msg_type_name") or "").strip().lower()
            is_group = str(msg.get("chat_type") or "") == "group"
            invoked = (
                msg_type == "pat"
                or any(name and name in content for name in names)
                or (not is_group and sender_id in owners)
            )
            reference = detect_media_reference(content)
            if not invoked and not reference.matched:
                continue

            prev_media = None
            for candidate in reversed(media_indices):
                if candidate >= index:
                    continue
                if not _same_sender_values(
                    sender_id, str(msg.get("sender_name") or ""),
                    str(messages[candidate].get("sender_id") or ""),
                    str(messages[candidate].get("sender_name") or ""),
                ):
                    continue
                if index - candidate > max_gap:
                    break
                prev_media = candidate
                break
            next_media = None
            for candidate in media_indices:
                if candidate <= index:
                    continue
                if not _same_sender_values(
                    sender_id, str(msg.get("sender_name") or ""),
                    str(messages[candidate].get("sender_id") or ""),
                    str(messages[candidate].get("sender_name") or ""),
                ):
                    continue
                if candidate - index > max_gap:
                    break
                next_media = candidate
                break

            candidate = None
            if reference.matched:
                # image-first is the common case; a text-first "看看这张"
                # naturally reaches the next image.
                candidate = prev_media if prev_media is not None else next_media
            elif invoked:
                # Bare name / pat: only attach when truly adjacent.
                if prev_media is not None and index - prev_media <= 2:
                    candidate = prev_media
                elif next_media is not None and next_media - index <= 2:
                    candidate = next_media
            if candidate is None:
                continue
            candidate_msg = messages[candidate]
            path = _message_path(candidate_msg)
            sticker = extract_sticker_md5(candidate_msg)
            description = str(
                candidate_msg.get("_sticker_description")
                or candidate_msg.get("sticker_description")
                or ""
            )
            if path:
                msg["_attached_media_paths"] = [path]
            if sticker:
                msg["_attached_sticker_id"] = sticker
                msg["_sticker_description"] = description
            msg["_media_reference"] = (
                reference.confidence if reference.matched else "adjacent"
            )
            if path:
                attachments[index] = [path]
    return attachments


# ---------------------------------------------------------------------------
# per-turn resolution used by the conversational agent
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedMedia:
    """Result of resolving a turn's reference to recent media."""

    paths: list[str] = field(default_factory=list)
    sticker_ids: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    items: list[MediaItem] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.paths or self.sticker_ids or self.descriptions)


def _select_recent_media_items(
    *,
    platform: str,
    account_id: str,
    chat_id: str,
    sender_id: str,
    sender_name: str = "",
    is_group: bool,
    trigger_reason: str = "",
    content: str = "",
    now: datetime | None = None,
) -> list[MediaItem]:
    now = now or datetime.now()
    # 2026-09-23 修复：引用明确指向媒体时，优先"被引用的那个"——不再拿
    # "最近一张"充数（她在群里把"你有这个表情吗"答成了上一张表情包）。
    quoted = extract_quoted_media(content)
    if quoted.author or quoted.content:
        if quoted.md5:
            for item in get_recent_media_store().list_items(
                platform=platform, account_id=account_id,
                chat_id=chat_id, now=now,
            ):
                if item.sticker_id == quoted.md5:
                    return [item]
            # 被引用的表情包可能已滑出 recent store：合成"仅 md5"条目，
            # 描述交给 ensure_emoji_description 补；来源记引用作者。
            return [MediaItem(
                platform=platform, account_id=account_id, chat_id=chat_id,
                sender_id="", sender_name=quoted.author or "",
                msg_id="", kind="sticker", sticker_id=quoted.md5,
                created_at=now,
            )]
        # 引用的是文本、或是定位不到 md5 的媒体（如 [媒体消息]）：
        # 不注入任何"最近媒体"——宁可少看，不猜错。
        return []
    store = get_recent_media_store()
    items = store.list_items(
        platform=platform, account_id=account_id, chat_id=chat_id, now=now,
    )
    if not items:
        return []
    last = items[0]
    trigger = str(trigger_reason or "")
    reference = detect_media_reference(content)

    def same_sender(item: MediaItem) -> bool:
        if not sender_id and not sender_name:
            return True
        return _same_sender_values(
            sender_id, sender_name, item.sender_id, item.sender_name,
        )

    if reference.matched and reference.confidence == "high":
        for item in items:
            if same_sender(item):
                return [item]
        # In 1:1 chats there is only one possible owner, so allow it even when
        # sender ids are missing/unknown.
        if not is_group:
            return [last]
        return []

    if same_sender(last):
        if "pat" in trigger and store.age_seconds(last, now) <= PAT_MEDIA_TTL_SECONDS:
            return [last]
        if reference.matched and store.age_seconds(last, now) <= NAME_MEDIA_TTL_SECONDS:
            return [last]
        if (
            trigger in ("mentioned_in_group", "name_call")
            and store.age_seconds(last, now) <= NAME_MEDIA_TTL_SECONDS
            and len((content or "").strip()) <= 20
        ):
            return [last]
    return []


def resolve_recent_media(
    *,
    platform: str,
    account_id: str,
    chat_id: str,
    sender_id: str,
    sender_name: str = "",
    is_group: bool,
    trigger_reason: str = "",
    content: str = "",
    now: datetime | None = None,
) -> list[str]:
    """Return local image paths a turn should look at, if any."""
    items = _select_recent_media_items(
        platform=platform, account_id=account_id, chat_id=chat_id,
        sender_id=sender_id, sender_name=sender_name, is_group=is_group,
        trigger_reason=trigger_reason, content=content, now=now,
    )
    return [item.media_path for item in items if item.media_path]


def resolve_recent_media_details(
    *,
    platform: str,
    account_id: str,
    chat_id: str,
    sender_id: str,
    sender_name: str = "",
    is_group: bool,
    trigger_reason: str = "",
    content: str = "",
    now: datetime | None = None,
) -> ResolvedMedia:
    """Like :func:`resolve_recent_media` but also returns sticker metadata."""
    items = _select_recent_media_items(
        platform=platform, account_id=account_id, chat_id=chat_id,
        sender_id=sender_id, sender_name=sender_name, is_group=is_group,
        trigger_reason=trigger_reason, content=content, now=now,
    )
    return ResolvedMedia(
        paths=[item.media_path for item in items if item.media_path],
        sticker_ids=[item.sticker_id for item in items if item.sticker_id],
        descriptions=[item.description for item in items if item.description],
        items=items,
    )


class MediaDescriptionCache:
    """In-memory cache of generic image descriptions, keyed by file hash."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, str] = {}

    def get(self, path: str) -> str:
        key = media_fingerprint(path)
        if not key:
            return ""
        with self._lock:
            return self._items.get(key, "")

    def set(self, path: str, description: str) -> None:
        key = media_fingerprint(path)
        text = (description or "").strip()
        if not key or not text:
            return
        with self._lock:
            self._items[key] = text

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_description_cache: MediaDescriptionCache | None = None


def get_media_description_cache() -> MediaDescriptionCache:
    global _description_cache
    with _store_lock:
        if _description_cache is None:
            _description_cache = MediaDescriptionCache()
        return _description_cache


_GENERIC_IMAGE_QUESTIONS = frozenset({
    "", "[图片]", "看看", "看下", "这张图", "那张图", "看看这张图",
    "看下这张图", "看一下这张图", "图片", "图", "这是什么", "什么意思",
    "描述一下", "描述下", "描述一下这张图", "这张图是什么",
})


def is_generic_image_request(text: str) -> bool:
    """True when the turn only asks for a description, not a specific question."""
    value = (text or "").strip()
    if value in _GENERIC_IMAGE_QUESTIONS:
        return True
    if len(value) <= 8 and any(marker in value for marker in ("看看", "看下", "这张图", "那张图")):
        return True
    return False


def cached_image_description(paths: Sequence[str]) -> str:
    cache = get_media_description_cache()
    for path in paths or ():
        description = cache.get(str(path))
        if description:
            return description
    return ""


def remember_image_description(paths: Sequence[str], description: str, *, engine=None) -> None:
    """Remember what a picture showed, in memory and on its media row.

    The row is the one that matters long-term: the in-process cache dies with
    the process, so a description kept only there cannot be used to render a
    transcript (or the summary built from it) after a restart.
    """
    cache = get_media_description_cache()
    for path in paths or ():
        if str(path).strip():
            cache.set(str(path), description)
            _store_description_on_media_row(str(path), description, engine=engine)
            return


def _store_description_on_media_row(path: str, description: str, *, engine=None) -> None:
    try:
        from core.terminal.store_port import get_repo
        from core.conversation.media_registry import remember_description

        remember_description(engine if engine is not None else get_repo().engine, path, description)
    except Exception as exc:  # noqa: BLE001
        # 2026-11-01：原来是 debug —— 描述只在内存里、库上永远是 NULL，谁都看不见。
        # 落库失败必须留痕（写库守卫拒绝 / 库不可写 / 行不存在都算），否则外层
        # media_barrier 会一直以为"这张图还没看清"，每次白等 20 秒再跳过这一拍。
        log.warning("media_description_persist_failed", path=str(path)[-48:],
                    error=str(exc)[:160], hint="写库被拒或行不存在：描述只在内存里")


# How much of a description to inline into a transcript line.  A description is
# a short phrase already; the cap is only there so a runaway answer cannot eat
# the prompt budget.
_TRANSCRIPT_DESCRIPTION_CHARS = 60


_RO_ENGINE: dict = {"path": "", "engine": None}


def _read_only_meta_engine():
    """元库的**只读**句柄。

    2026-10-27：渲染只是读，不该为了一句描述去要可写 engine —— 不传 engine 时
    media_registry 会退回默认可写库，`db.write_guard` 每次渲染都刷一条
    `production_write_blocked` 错误，日志里看着像出事了。只读连接守卫不拦。
    """
    try:
        from core.bootstrap import get_meta_db_path
        from db.write_guard import read_only_engine

        path = str(get_meta_db_path())
        if _RO_ENGINE["path"] != path:
            _RO_ENGINE["engine"] = read_only_engine(path)
            _RO_ENGINE["path"] = path
        return _RO_ENGINE["engine"]
    except Exception:  # noqa: BLE001 - 拿不到就只剩"读账号库"那条路
        return None


def _render_quoted_media(text: str, *, media_path: str = "", chat_id: str = "",
                         created_at=None, engine=None) -> str:
    """引用媒体（定位串 / `[引用 X: [图片]]`）→ 带上她看见的描述。

    2026-10-27：`render_media_content` 只认字面量 ``[图片]``，而微信引用表情包/文件时
    只给定位串；这里交给 `core.conversation.media_ref`（它认得 md5 在第 4 段）。
    查不到就原样返回，渲染永不抛、永不阻断。
    """
    try:
        from core.conversation.media_ref import render_media_locators

        if engine is None:
            engine = _read_only_meta_engine()
        return render_media_locators(
            text, path=media_path, chat_id=chat_id, created_at=created_at, engine=engine,
            mode=("wait" if describe_on_turn_enabled() else "peek"),
            budget_s=IMAGE_BRIEF_BUDGET_S,
        )
    except Exception:  # noqa: BLE001 - 渲染不阻断
        warn("core/conversation/media.py:_render_quoted_media Exception",
             err=repr(__import__("sys").exc_info()[1]))
        return text


def render_media_content(content: str, media_path: str = "", *,
                         chat_id: str = "", created_at=None,
                         window_seconds: int = 120, engine=None) -> str:
    """Say what a picture showed instead of leaving a bare ``[图片]``.

    A transcript line that says "[图片]" carries no information, and the model
    cannot ask about it later either.  Once the image has been described -- which
    happens on the turn it arrives -- that description is cached by file, so any
    later rendering of the same turn, and the rolling summary built from it, can
    name what was in the picture.

    When the row has no path of its own (it was written before the file existed,
    or before the column did) the media registry is asked for the file belonging
    to that moment in that conversation.
    """
    text = str(content or "")
    if "[图片]" not in text:
        # 2026-10-27：微信把「引用一条媒体」存成定位串（`chat:时间戳:1:md5::0`）而不是
        # 字面量 [图片]，所以这里以前直接原样返回 —— 她引用自己发的表情包时，模型看到
        # 的就是"一串编号"。引用行一律交给 _render_quoted_media 认。
        if "[引用" in text:
            return _render_quoted_media(text, media_path=media_path, chat_id=chat_id,
                                        created_at=created_at, engine=engine)
        return text

    path = str(media_path or "").strip()
    description = ""
    stored_path, stored_description = "", ""
    try:
        from core.conversation.media_registry import lookup_description

        stored_path, stored_description = lookup_description(
            engine, path=path, chat_id=chat_id, created_at=created_at,
            window_seconds=window_seconds,
        )
    except Exception:  # noqa: BLE001
        warn("core/conversation/media.py:1374 Exception", err=repr(__import__("sys").exc_info()[1]))
        stored_description = ""
    description = stored_description
    if not description and path:
        # The in-process cache is the fallback: it is warm during a live turn
        # even before the row has been written.
        try:
            description = cached_image_description([path])
        except Exception:  # noqa: BLE001
            warn("core/conversation/media.py:1382 Exception", err=repr(__import__("sys").exc_info()[1]))
            description = ""
    if not description:
        return text
    short = " ".join(str(description).split())[:_TRANSCRIPT_DESCRIPTION_CHARS]
    if not short:
        return text
    text = text.replace("[图片]", f"[图片: {short}]")
    if "[引用" in text:
        text = _render_quoted_media(text, media_path=media_path, chat_id=chat_id,
                                    created_at=created_at, engine=engine)
    return text


def media_path_from_message_row(row: Any) -> str:
    """Return a row's media path only when the row really is image media."""
    path = str(getattr(row, "media_path", "") or "").strip()
    if not path:
        return ""
    kind = str(getattr(row, "msg_type_name", "") or "")
    content = str(getattr(row, "content", "") or "")
    if is_media_message(kind, content, path):
        return path
    return ""


def sticker_md5_from_message_row(row: Any) -> str:
    """Extract a sticker md5 from a DB message row."""
    return extract_sticker_md5({
        "sticker_id": getattr(row, "sticker_id", "") or "",
        "content": getattr(row, "content", "") or "",
        "extra": getattr(row, "extra", "") or "",
    })


def build_pending_media_message(
    pending: PendingMediaRequest,
    image_msg: Mapping[str, Any],
    *,
    platform: str = "",
    sticker_id: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Build the synthetic turn that answers a text-first image question.

    The original question is replayed with the image attached and routing
    forced to "addressed"; the group chat never sees an injected @mention
    because the content stays exactly the original question text.
    """
    synthetic = dict(image_msg or {})
    path = str(synthetic.get("media_path") or "")
    question = (
        "[图片]" if str(pending.kind or "") == "pat"
        else pending.question
    )
    synthetic.update({
        "source": platform or synthetic.get("source") or "wechat",
        "msg_type_name": "text",
        "content": question,
        "sender_id": pending.sender_id,
        "sender_name": pending.sender_name,
        "media_path": path,
        "_attached_media_paths": [path] if path else [],
        "_attached_sticker_id": str(sticker_id or ""),
        "_sticker_description": str(description or ""),
        "extra": json.dumps({"mention_self": True}, ensure_ascii=False),
        # 保留原问题的发送时间：补图后的回答是在回应“当时”的问题。
        "created_at": pending.created_at or synthetic.get("created_at"),
    })
    return synthetic


def unresolved_media_reference(content: str) -> bool:
    """True when a message *explicitly* asks for an image we did not get yet."""
    return detect_media_reference(content).confidence == "high"


# ---------------------------------------------------------------------------
# 2026-10-26 到达即描述：把「这一轮有图」变成「这一轮有描述」
#
# render_media_content 的注释一直写着「描述在到达那一轮由视觉模型生成」，但这句话
# 只在她主动调 analyze_image 时成立。图在入库那一刻还没落盘时（message_media
# status='missing' / last_error='media path not resolved at ingest'），那一轮没有
# 任何像素可看，描述于是永远没人生成，历史里就只剩裸 [图片]。
#
# 这里补的是「谁去生成」这一根线：给一个已经在磁盘上的文件，缺描述就现问一次
# 视觉模型，写回行上。渲染侧（lookup_description / render_media_content）不用改。
# ---------------------------------------------------------------------------

_IMAGE_BRIEF_QUESTION = (
    "用一句话说清这张图里最值得说的东西，30 字以内：画面主体是什么、在做什么，"
    "有文字就把文字读出来。不要寒暄，不要以「这张图」开头，"
    "不要写分析步骤或编号，只给最后那一句话。"
)
def _image_brief_question() -> str:
    """写描述时的问法：基础问法 + 形象包的辨识特征提示（没有包就只是基础问法）。

    2026-10-26 P2.5：描述是后面"认得出自己"的唯一依据，所以趁这一次就把特征问清楚，
    而不是事后为了认图再多问一次视觉模型。
    """
    try:
        from core.identity.self_look import describe_hint  # noqa: PLC0415

        hint = describe_hint()
    except Exception:  # noqa: BLE001 - 提示拿不到不影响看图
        hint = ""
    if not hint:
        return _IMAGE_BRIEF_QUESTION
    return _IMAGE_BRIEF_QUESTION + (
        "\n画面里如果是动漫/卡通人物，把看得见的辨识特征并进那句话：" + hint +
        "。看不出来就不写，不要猜。"
    )


IMAGE_BRIEF_BUDGET_S = 12.0
_IMAGE_DESCRIBE_ENV = "YINYUE_IMAGE_DESCRIBE_ON_TURN"
_IMAGE_DESCRIBE_TRIED: dict[str, int] = {}
_IMAGE_DESCRIBE_TRIES_MAX = 2  # 同一张图最多问两次：超时/空返回不该永久拉黑
_IMAGE_DESCRIBE_LOCK = threading.Lock()
_IMAGE_DESCRIBE_INFLIGHT: dict[str, threading.Event] = {}
_IMAGE_DESCRIBE_WORKER = threading.local()


def describe_on_turn_enabled() -> bool:
    """这一轮要不要为缺描述的图现问一次（tri：默认 on，写 off 关掉）。"""
    import os

    raw = str(os.environ.get(_IMAGE_DESCRIBE_ENV, "on") or "").strip().lower()
    return raw not in {"0", "off", "false", "no", "none", "disable", "disabled"}


def _pick_vision_text(result: Any) -> str:
    """从 ChatResult 里取「她真正看到的东西」。

    2026-10-26：ChatResult 只有 text / usage / finish_reason / reasoning_blocks /
    raw_meta 五个字段，**没有** reasoning 或 content 属性 —— 只按属性名兜
    (text, content, reasoning) 会一直拿到空串，图明明送进去了却判定「模型没返回」。
    思考型模型（deepseek-flash）把观察写在 reasoning_blocks[*].text 里，且常因
    max_tokens 用尽而 finish_reason='length'、text 为空，所以这里必须读块。
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, Mapping):
        for key in ("text", "content", "reasoning", "reasoning_content"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    else:
        for name in ("text", "content", "reasoning", "reasoning_content"):
            value = getattr(result, name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    blocks = (result.get("reasoning_blocks") if isinstance(result, Mapping)
              else getattr(result, "reasoning_blocks", None)) or ()
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Mapping):
            piece = block.get("text")
        else:
            piece = getattr(block, "text", None)
        if isinstance(piece, str) and piece.strip():
            parts.append(piece.strip())
    return "\n".join(parts).strip()


_BRIEF_NOISE = re.compile(r"^(\d+[.、)]|[-*•])\s*")


def _clean_brief(raw: str) -> str:
    """把思考稿压成一句能放进 [图片: ...] 的话。"""
    text = str(raw or "").replace("**", "").strip()
    if not text:
        return ""
    kept: list[str] = []
    _META = ("分析", "检查", "综合", "草稿", "输入", "问题", "限制", "用户想要", "目标",
             "要求", "禁止", "注意", "回答", "步骤", "思考", "根据", "提示", "接下来",
             "观察到", "画面主体是", "需要", "可以写", "字数")
    for line in text.splitlines():
        item = _BRIEF_NOISE.sub("", line.strip())
        if not item:
            continue
        # 分析骨架行（「分析用户请求」「检查图片内容」）与"复述任务"都不是描述本身
        if item.endswith(("：", ":")) or item.startswith(_META):
            continue
        kept.append(item)
    # 2026-10-27：思考稿里常常混着"算字数 / 试写 / 取舍"的中间稿，取最后一行会把它们捞上来。
    # 规则：从后往前找第一行"像描述"的句子——够长、不以连接词开头、不含量词算术。
    _BAD_START = ("但", "也", "所以", "因此", "尝试", "计算", "或者", "字数", "重新", "检查",
                  "等等", "不过", "因此", "结论是", "草稿")
    _BAD_IN = ("字数：", "字数:", "计算：", "个字符", "字。", "可以了", "不改了", "先这样")
    body = ""
    for item in reversed(kept or [text.replace("\n", " ")]):
        cand = " ".join(item.split())
        if len(cand) < 8 or len(cand) > 80:
            continue
        if cand.startswith(_BAD_START) or any(k in cand for k in _BAD_IN):
            continue
        body = cand
        break
    if not body:                      # 全都像中间稿：老实说不知道
        return ""
    for mark in ("草稿1：", "草稿1:", "一句话描述：", "综合成一句话描述："):
        if mark in body:
            body = body.split(mark)[-1].strip()
    return body.lstrip("：: *")[:60].strip()


def _ask_vision_sync(path: str, question: str, budget_s: float) -> str:
    """问一次视觉模型，硬超时。超时/失败都返回 ""（本轮按「还没看到」处理）。"""
    import asyncio
    import concurrent.futures

    data_uri = media_path_to_data_uri(str(path))
    if not data_uri:
        return ""

    async def _ask() -> str:
        from core.bootstrap import get_llm_router

        router = get_llm_router()
        result = await router.call(
            "vision",
            agent="image_analyzer",
            task="vision",
            messages=[
                {"role": "system",
                 "content": "你是一个图片分析专家。请仔细观察图片，准确回答用户的问题。"},
                {"role": "user", "content": question},
            ],
            images=[data_uri],
            temperature=0.3,
            max_tokens=2000,
        )
        return _clean_brief(_pick_vision_text(result))

    # 2026-10-28：同步开关。默认走线程池（硬超时靠 shutdown(wait=False) 成立），
    # 但临时脚本/验证工具在解释器关闭阶段还会被后台线程调进来，那时线程池已经
    # shutdown，submit 会抛 "cannot schedule new futures after interpreter shutdown"，
    # 描述就丢了。置 YINYUE_VISION_SYNC=1 时直接在当前线程跑，不再碰线程池。
    _sync = flag("YINYUE_VISION_SYNC")
    if _sync:
        try:
            return str(asyncio.run(_ask()) or "").strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("image_describe_failed", path=Path(path).name,
                        error=str(exc)[:160], mode="sync")
            return ""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(lambda: asyncio.run(_ask()))
    except RuntimeError as exc:
        # 解释器已经在关：退回同步跑一次，别把描述丢了。
        log.warning("image_describe_pool_closed", path=Path(path).name,
                    error=str(exc)[:120])
        try:
            return str(asyncio.run(_ask()) or "").strip()
        except Exception as exc2:  # noqa: BLE001
            log.warning("image_describe_failed", path=Path(path).name,
                        error=str(exc2)[:160], mode="fallback")
            return ""
    try:
        return str(future.result(timeout=max(1.0, float(budget_s))) or "").strip()
    except concurrent.futures.TimeoutError:
        log.warning("image_describe_timeout", path=Path(path).name,
                    budget_s=budget_s)
        return ""
    except Exception as exc:  # noqa: BLE001
        log.warning("image_describe_failed", path=Path(path).name,
                    error=str(exc)[:160])
        return ""
    finally:
        # 不等线程：future.result(timeout) 的硬超时靠这一句才成立，
        # 否则 pool 退出时会把卡住的视觉调用等回来。
        pool.shutdown(wait=False)


def _existing_description(target: str, engine=None) -> str:
    """先看进程内缓存，再看库里那行（都是「已有描述」的合法来源）。"""
    cached = cached_image_description([target])
    if cached:
        return cached
    try:
        from core.conversation.media_registry import lookup_description

        _, stored = lookup_description(engine, path=target)
    except Exception as exc:  # noqa: BLE001
        log.debug("image_describe_lookup_failed", error=str(exc)[:120])
        return ""
    return str(stored or "")


def _describe_now(target: str, budget_s: float, engine=None) -> str:
    """真问一次视觉模型并写回行上（去重和预算由调用方负责）。"""
    text = _ask_vision_sync(target, _image_brief_question(), budget_s)
    if not text:
        return ""
    try:
        remember_image_description([target], text, engine=engine)
    except Exception as exc:  # noqa: BLE001
        log.debug("image_describe_remember_failed", error=str(exc)[:120])
    log.info("image_described", path=Path(target).name, chars=len(text))
    return text


def _claim_describe_slot(target: str) -> bool:
    """同一文件在本进程最多问 _IMAGE_DESCRIBE_TRIES_MAX 次。"""
    with _IMAGE_DESCRIBE_LOCK:
        tries = _IMAGE_DESCRIBE_TRIED.get(target, 0)
        if tries >= _IMAGE_DESCRIBE_TRIES_MAX:
            return False
        if len(_IMAGE_DESCRIBE_TRIED) > 512:
            _IMAGE_DESCRIBE_TRIED.clear()
        _IMAGE_DESCRIBE_TRIED[target] = tries + 1
        return True


def ensure_image_description(path: str, *, engine=None,
                             budget_s: float = IMAGE_BRIEF_BUDGET_S) -> str:
    """这张图有描述就返回它；没有就现问一次视觉模型并写回行上。

    2026-10-26：图刚落盘时 prefetch_image_description 通常已经在后台问着了，
    所以这里先等它（最多一个预算），等不到才自己问 —— 正常路径下不重复付费。
    """
    target = str(path or "").strip()
    if not target:
        return ""
    try:
        if not Path(target).is_file():
            return ""
    except OSError:
        return ""

    existing = _existing_description(target, engine)
    if existing:
        return existing

    # 后台正在为同一张图跑：等它，别重复付一次视觉调用。
    if getattr(_IMAGE_DESCRIBE_WORKER, "path", "") != target:
        with _IMAGE_DESCRIBE_LOCK:
            pending = _IMAGE_DESCRIBE_INFLIGHT.get(target)
        if pending is not None and not pending.is_set():
            pending.wait(timeout=max(1.0, float(budget_s)))
            existing = _existing_description(target, engine)
            if existing:
                return existing

    if not _claim_describe_slot(target):
        return ""
    return _describe_now(target, budget_s, engine)


def prefetch_image_description(path: str, *,
                               budget_s: float = IMAGE_BRIEF_BUDGET_S,
                               on_ready: Any = None) -> bool:
    """图刚落盘时在后台先问一次，轮到回复时多半已有描述（不阻塞任何人）。

    幂等：已有描述 / 已经在问 / 已问够次数，都直接返回 False。
    `on_ready(描述)` 在后台问完时回调一次 —— 给"必须写回某个具体库/某一行"
    的调用方用（loop 读的是账号库的 message_media，不是任一进程的默认 repo）。
    """
    if not describe_on_turn_enabled():
        return False
    target = str(path or "").strip()
    if not target:
        return False
    try:
        if not Path(target).is_file():
            return False
    except OSError:
        return False
    if _existing_description(target):
        return False
    with _IMAGE_DESCRIBE_LOCK:
        if target in _IMAGE_DESCRIBE_INFLIGHT:
            return False
        done = threading.Event()
        _IMAGE_DESCRIBE_INFLIGHT[target] = done
    if not _claim_describe_slot(target):
        with _IMAGE_DESCRIBE_LOCK:
            _IMAGE_DESCRIBE_INFLIGHT.pop(target, None)
        return False

    def _run() -> None:
        _IMAGE_DESCRIBE_WORKER.path = target
        try:
            text = _describe_now(target, budget_s)
            if text and on_ready is not None:
                try:
                    on_ready(text)
                except Exception as exc:  # noqa: BLE001
                    log.debug("image_prefetch_on_ready_failed", error=str(exc)[:120])
        except Exception as exc:  # noqa: BLE001
            log.debug("image_prefetch_failed", error=str(exc)[:120])
        finally:
            _IMAGE_DESCRIBE_WORKER.path = ""
            with _IMAGE_DESCRIBE_LOCK:
                _IMAGE_DESCRIBE_INFLIGHT.pop(target, None)
            done.set()

    thread = threading.Thread(target=_run, name="image-describe", daemon=True)
    try:
        thread.start()
    except Exception as exc:  # noqa: BLE001
        with _IMAGE_DESCRIBE_LOCK:
            _IMAGE_DESCRIBE_INFLIGHT.pop(target, None)
        log.debug("image_prefetch_spawn_failed", error=str(exc)[:120])
        return False
    return True
