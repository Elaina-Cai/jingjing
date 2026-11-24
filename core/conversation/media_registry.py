"""Keep track of every media message until its file is actually usable.

The problem this solves
-----------------------
``messages.media_path`` was filled in exactly once, by the ingest pass, and
whatever that pass produced was final.  WeChat writes an image's thumbnail
whenever it feels like it, which was measured at 1.1s for a group image and
27s for a private one, while the ingest pass ran ~6s after both were sent.  The
group image resolved and the private one did not -- and because the md5 that
would have made a second attempt possible was thrown away, it stayed broken
permanently.  She could never see that picture, in any conversation, ever.

So: every media message gets a row here, whether or not the first attempt
worked, carrying the clues (md5, WeChat's session directory, a quoted message
id).  ``status`` is a small state machine and a later pass retries the pending
ones until either the file appears or the retries are exhausted.

Nothing here talks to the model.  Resolution ends at "a decrypted local image
file exists", which is exactly what the existing ingest path already knows how
to turn into something she can see.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import structlog
from safelog import warn

log = structlog.get_logger()

__all__ = [
    "KIND_IMAGE", "KIND_EMOJI", "KIND_QUOTE_IMAGE", "KIND_UNSUPPORTED",
    "STATUS_PENDING", "STATUS_RESOLVED", "STATUS_MISSING", "STATUS_UNSUPPORTED",
    "session_dir_for", "register", "resolve_once", "resolve_pending",
    "rows_for_chat", "quoted_is_media", "nearby_resolved_path",
    "remember_description", "lookup_description", "pending_descriptions",
]

KIND_IMAGE = "image"
KIND_EMOJI = "emoji"
# A quoted message whose quoted content is an image: the picture being talked
# about belongs to the message it quotes, not to this one.
KIND_QUOTE_IMAGE = "quote_image"
KIND_UNSUPPORTED = "unsupported"

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"
STATUS_MISSING = "missing"
STATUS_UNSUPPORTED = "unsupported"

# Backoff between attempts, in seconds.  The first retry is deliberately soon:
# the whole failure mode is "the file arrived seconds later than the sync pass",
# so most pending rows resolve on the first or second try.
_RETRY_SCHEDULE = (30, 120, 600, 1800, 7200, 21600)
_MAX_ATTEMPTS = len(_RETRY_SCHEDULE)


def _as_datetime(value) -> Optional[datetime]:
    """Accept a datetime, or the string a raw SQL row gives back for one.

    Reading ``messages.created_at`` through ``text()`` yields a string, and
    handing that to the ORM raises "SQLite DateTime type only accepts Python
    datetime" -- which, during a 131-row backfill, meant every single write
    failed while the caller's counter happily reported 131 successes.
    """
    if value is None or isinstance(value, datetime):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value)
    except ValueError:
        warn("core/conversation/media_registry.py:76 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return None


def session_dir_for(chat_id: str) -> str:
    """md5(session) -- the directory WeChat files one chat's media under.

    Verified against a live database: table ``Msg_{md5(session_name)}`` in
    message_0.db, and ``cache/<month>/Message/{md5(session)}/Bubble/{md5}_b.dat``
    plus ``msg/attach/{md5(session)}/<month>/Img/{md5}.dat`` on disk.
    """
    return hashlib.md5(str(chat_id or "").encode()).hexdigest()


def _engine(engine=None):
    """Resolve the database to work against.

    Callers that already hold the account engine pass it in; everything else
    falls back to the active repository, which is the account database during
    ingestion and in the agent runtime alike.
    """
    if engine is not None:
        return engine
    from core.terminal.store_port import get_repo

    return get_repo().engine


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def quoted_is_media(engine, quote_ref: str) -> bool:
    """Whether a quoted message id points at a message that has media.

    Most replies quote plain text.  Registering all of them would queue hundreds
    of rows that can never resolve and would be retried forever, so a reply is
    only tracked when the thing it quotes really is a picture.
    """
    ref = str(quote_ref or "").strip()
    if not ref:
        return False
    try:
        ref_id = int(ref)
    except (TypeError, ValueError):
        warn("core/conversation/media_registry.py:120 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return False

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from db.models import Message

    try:
        with Session(_engine(engine)) as session:
            row = session.scalars(
                select(Message).where(Message.msg_id == ref_id)
            ).first()
            if row is None:
                return False
            return str(row.msg_type_name or "") in ("image", "sticker", "video")
    except Exception as exc:  # noqa: BLE001
        log.warning("media_quote_type_lookup_failed", quote_ref=ref[:24],
                    error=str(exc)[:120])
        return False


def register_from_message(
    engine,
    *,
    message_id: int | None = None,
    msg_id: int | None = None,
    chat_id: str = "",
    sender_id: str = "",
    kind: str = "",
    md5: str = "",
    media_path: str = "",
    quote_ref: str = "",
    account_id: str = "",
    platform: str = "wechat",
    created_at=None,
) -> bool:
    """Register one ingested message, if it carries anything worth tracking.

    The single place that decides whether a message is media and whether a reply
    quotes media, so the ingest loop stays a two-line call and this decision can
    be tested against real payloads instead of only through a full sync run.

    ``kind`` non-empty means the message *is* media (with ``md5`` as its clue).
    Otherwise a ``quote_ref`` makes it a reply about someone else's picture --
    and only when the quoted message really is media, because most replies quote
    plain text and tracking those would queue rows that can never resolve.
    """
    if not kind and not quote_ref:
        return False
    if not kind:
        if not quoted_is_media(engine, quote_ref):
            return False
        kind = KIND_QUOTE_IMAGE
    return register(
        engine,
        message_id=message_id,
        msg_id=msg_id,
        chat_id=chat_id,
        sender_id=sender_id,
        kind=kind,
        md5=md5,
        source_path=media_path,
        quote_ref=quote_ref,
        account_id=account_id,
        platform=platform,
        created_at=created_at,
    )


def register(
    engine,
    *,
    message_id: int | None = None,
    msg_id: int | None = None,
    chat_id: str = "",
    sender_id: str = "",
    kind: str = KIND_IMAGE,
    md5: str = "",
    source_path: str = "",
    quote_ref: str = "",
    platform: str = "wechat",
    account_id: str = "",
    created_at: datetime | None = None,
) -> bool:
    """Record a media message.  Idempotent on (account, msg_id) or message_id.

    Called for *every* media message, not only the ones that failed to resolve.
    That is the entire design: a row that says "this message had a picture, here
    is what we know about it" is what makes a later retry possible, and it costs
    nothing when the first attempt already succeeded.

    Returns whether the row was written, so a caller counting its work cannot
    report success for a write that failed.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from db.models import MessageMedia

    if not message_id and not msg_id:
        return False
    created_at = _as_datetime(created_at)
    try:
        with Session(_engine(engine)) as session:
            row: Optional[MessageMedia] = None
            if message_id:
                row = session.scalars(
                    select(MessageMedia).where(MessageMedia.message_id == message_id)
                ).first()
            if row is None and msg_id:
                row = session.scalars(
                    select(MessageMedia).where(
                        MessageMedia.msg_id == msg_id,
                        MessageMedia.account_id == (account_id or ""),
                    )
                ).first()

            if row is None:
                row = MessageMedia(
                    message_id=message_id,
                    msg_id=msg_id,
                    platform=platform or "wechat",
                    account_id=account_id or "",
                    chat_id=chat_id or None,
                    sender_id=sender_id or None,
                    kind=kind,
                    status=STATUS_PENDING,
                    created_at=created_at or datetime.now(),
                    next_retry_at=datetime.now(),
                )
                session.add(row)

            # Refresh the clues: a re-read of the raw message may know more than
            # the first pass did.
            if md5:
                row.md5 = md5
            if source_path:
                row.source_path = source_path
            if quote_ref:
                row.quote_ref = quote_ref
            if chat_id and not row.chat_id:
                row.chat_id = chat_id
            if chat_id:
                row.session_dir = session_dir_for(chat_id)
            # ``created_at`` is the message's own time, not when this row was
            # written -- a backfill pass runs long after the message, and the
            # time is what ties the row back to its transcript line.
            if created_at:
                row.created_at = created_at
            session.commit()
            return True
    except Exception as exc:  # noqa: BLE001 - never break ingestion for this
        log.warning("media_register_failed", msg_id=msg_id, message_id=message_id,
                    error=str(exc)[:140])
        return False


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

def _account_wx_db_path(account_id: str) -> str:
    """The WeChat data directory for one account, not just the default one."""
    try:
        from core.config.settings import config

        for account in config.all_account_configs():
            if account.account_id == account_id and getattr(account, "wx_db_path", ""):
                return str(account.wx_db_path)
    except Exception:
        warn("core/conversation/media_registry.py:290 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        from core.config.settings import config

        return str(config.wechat.db_path or "")
    except Exception:
        warn("core/conversation/media_registry.py:296 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _decrypt_to_cache(path: str) -> str:
    """Decrypt a WeChat ``.dat`` into data/image_cache and return the new path."""
    if not path:
        return ""
    if Path(path).suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        return path if Path(path).exists() else ""
    try:
        from db.sync_service import SyncService

        return SyncService._cache_image_dat(path) or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("media_decrypt_failed", path=str(path)[:90], error=str(exc)[:120])
        return ""


def _quoted_media(row, engine=None, depth: int = 0) -> str:
    """The picture a quoted message refers to.

    WeChat replaces a quoted picture with the placeholder "[媒体消息]", so the
    reply itself has no path and never will -- that is why she keeps saying she
    only sees "[媒体消息]".  The quoted message id is recorded though, and the
    message it points at does have the picture, so the image being discussed is
    reachable.  One level of recursion covers a reply that quotes a reply.
    """
    ref = str(row.quote_ref or "").strip()
    if not ref or depth > 1:
        return ""
    try:
        ref_id = int(ref)
    except (TypeError, ValueError):
        warn("core/conversation/media_registry.py:329 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return ""

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from db.models import Message, MessageMedia

    try:
        with Session(_engine(engine)) as session:
            target = session.scalars(
                select(Message).where(Message.msg_id == ref_id)
            ).first()
            if target is not None:
                path = str(target.media_path or "")
                if path and Path(path).exists():
                    return path
            other = session.scalars(
                select(MessageMedia).where(MessageMedia.msg_id == ref_id)
            ).first()
            if other is None:
                return ""
            if other.local_path and Path(other.local_path).exists():
                return str(other.local_path)
            # The quoted message may itself be waiting for its file; resolve it
            # now rather than making the reply wait for the next retry pass.
            local, status, _error = resolve_once(
                other, write_back=True, engine=engine, depth=depth + 1,
            )
            return local
    except Exception as exc:  # noqa: BLE001
        log.warning("media_quote_lookup_failed", quote_ref=ref[:24],
                    error=str(exc)[:120])
        return ""


def _accept_resolved(local: str) -> tuple[bool, str]:
    """Whether a found file may be published as ``resolved``.

    Resolution used to end at "a file exists at that path", which published a
    truncated picture as usable and handed it to the vision provider, whose
    answer was ``400 unsupported image``.  The judgement itself lives in
    ``core.conversation.media`` (its single owner); this function holds only the
    policy: readable ⇒ accept, otherwise keep the row ``pending`` so the
    existing retry pass comes back to it.

    Returns ``(accepted, last_error)``.
    """
    try:
        from core.conversation.media import (
            INTEGRITY_OK,
            READABLE_VERDICTS,
            image_integrity,
        )
    except Exception as exc:  # noqa: BLE001 - never break resolution for this
        log.debug("media_integrity_unavailable", error=str(exc)[:100])
        return True, ""

    verdict = image_integrity(local)
    if verdict in READABLE_VERDICTS:
        if verdict != INTEGRITY_OK:
            # Readable, but the artifact is not byte-complete.  Measured on this
            # machine's cache: 126 of 330 jpgs had lost their ``FFD9`` end
            # marker, and 102 of those still decoded -- so a missing tail is
            # counted here rather than treated as a failure.
            log.info("media_incomplete_tail", verdict=verdict,
                     path=Path(local).name)
        return True, ""

    # Present but not decodable.  The measured cause is upstream of this file:
    # ``db/image_decrypt.py::_decrypt_dat_v2`` stops at the length the ``.dat``
    # header declares and drops the trailing 16 bytes that really do hold the
    # JPEG's ``FFD9`` (verified on all 24 undecodable cached files: appending
    # that block XOR-decoded yields a whole image every time).  Re-encoding the
    # artifact here would hide that defect and ship a needlessly re-compressed
    # picture, so the row stays ``pending`` and the retry pass keeps it visible
    # until the producer is fixed and the cache is rebuilt.
    log.warning("media_unusable_file", verdict=verdict, path=Path(local).name)
    return False, "truncated" if verdict == "undecodable" else verdict


def resolve_once(row, *, write_back: bool = True, engine=None,
                 depth: int = 0) -> tuple[str, str, str]:
    """Try every resolver for one media row.

    Returns ``(local_path, status, error)``.  Resolvers are tried in the order
    of "most likely to be right and cheapest", and each one is independent --
    the point is that no single missing file ends the attempt.
    """
    from db.content_extract import (
        _resolve_emoji_local,
        _resolve_image_dat,
        _resolve_image_thumbnail,
    )

    wx_db_path = _account_wx_db_path(row.account_id)
    md5 = str(row.md5 or "").strip()
    tried: list[str] = []

    candidates: list[str] = []
    if row.source_path and Path(str(row.source_path)).exists():
        candidates.append(str(row.source_path))
    if md5:
        # WeChat writes the bubble thumbnail first and the full image later;
        # either is usable, and the thumbnail is what the sender actually saw.
        for resolver, label in (
            (_resolve_image_thumbnail, "thumbnail"),
            (_resolve_image_dat, "attach"),
            (_resolve_emoji_local, "emoji"),
        ):
            try:
                found = resolver(md5, wx_db_path) if wx_db_path else resolver(md5)
            except TypeError:
                warn("core/conversation/media_registry.py:441 TypeError", err=repr(__import__("sys").exc_info()[1]))
                found = resolver(md5)
            except Exception as exc:  # noqa: BLE001
                warn("core/conversation/media_registry.py:443 Exception", err=repr(exc))
                tried.append(f"{label}:err={str(exc)[:40]}")
                continue
            if found:
                candidates.append(str(found))
                tried.append(f"{label}:hit")
            else:
                tried.append(f"{label}:miss")

    refused: list[str] = []
    for candidate in candidates:
        local = _decrypt_to_cache(candidate)
        if local and Path(local).exists():
            accepted, reason = _accept_resolved(local)
            if not accepted:
                refused.append(reason)
                tried.append(f"{Path(local).name}:{reason}")
                continue
            if write_back:
                _write_back(row, local, engine)
            return local, STATUS_RESOLVED, ""

    # Nothing of our own worked; the reply may be about someone else's picture.
    if row.quote_ref:
        quoted = _quoted_media(row, engine, depth)
        if quoted and Path(quoted).exists():
            accepted, reason = _accept_resolved(quoted)
            if accepted:
                if write_back:
                    _write_back(row, quoted, engine)
                return quoted, STATUS_RESOLVED, ""
            refused.append(reason)
            tried.append(f"{Path(quoted).name}:{reason}")

    if refused:
        # A file *was* found and refused because it cannot be used as a picture.
        # That is a different situation from "no file at all", and it is the one
        # the retry pass must recognise, so it becomes the row's own error.  The
        # full resolver trail stays in the log rather than in the column.
        log.warning("media_candidates_refused", msg_id=str(row.msg_id or ""),
                    reason=refused[0], tried=", ".join(tried))
        return "", STATUS_PENDING, refused[0]

    if not md5 and not row.source_path and not row.quote_ref:
        error = "no clue recorded"
    elif row.quote_ref and not md5:
        error = ("quoted message " + str(row.quote_ref)[:20]
                 + " has no media of its own yet")
    elif md5 and not candidates:
        error = "found no local file for md5 " + md5[:12]
    else:
        error = "candidates unreadable: " + ", ".join(tried)
    return "", STATUS_PENDING, error


def _write_back(row, local_path: str, engine=None) -> None:
    """Publish the resolved file to ``messages.media_path``.

    Only fills a blank: an existing value came from the ingest pass and is not
    this module's business to second-guess.
    """
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from db.models import Message

        with Session(_engine(engine)) as session:
            message = None
            if row.message_id:
                message = session.get(Message, row.message_id)
            if message is None and row.msg_id:
                message = session.scalars(
                    select(Message).where(Message.msg_id == row.msg_id)
                ).first()
            if message is not None and not (message.media_path or "").strip():
                message.media_path = local_path
                session.commit()
                log.info("media_path_backfilled", message_id=message.id,
                          msg_id=message.msg_id, path=Path(local_path).name)
    except Exception as exc:  # noqa: BLE001
        log.warning("media_write_back_failed", error=str(exc)[:140])


def resolve_pending(engine=None, *, limit: int = 40, now: datetime | None = None,
                    force: bool = False) -> dict:
    """Retry pending media rows.  Safe to call on every sync pass.

    Rows are ordered oldest-first so a message that has been waiting longest --
    and is therefore most likely to have its file on disk by now -- is tried
    first, and each attempt moves its own ``next_retry_at`` forward.  ``force``
    ignores that schedule, for a caller that has just supplied new clues and
    wants an answer now.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from db.models import MessageMedia

    now = now or datetime.now()
    stats = {"tried": 0, "resolved": 0, "missing": 0, "still_pending": 0,
             "unsupported": 0}
    active = _engine(engine)

    try:
        with Session(active) as session:
            query = (
                select(MessageMedia)
                .where(MessageMedia.status == STATUS_PENDING)
            )
            if not force:
                query = query.where(
                    (MessageMedia.next_retry_at.is_(None))
                    | (MessageMedia.next_retry_at <= now)
                )
            rows = session.scalars(
                query.order_by(MessageMedia.id.asc()).limit(max(1, int(limit)))
            ).all()
            work = list(rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("media_pending_query_failed", error=str(exc)[:140])
        return stats

    for row in work:
        stats["tried"] += 1
        if row.kind == KIND_UNSUPPORTED:
            row.status = STATUS_UNSUPPORTED
            row.next_retry_at = None
            stats["unsupported"] += 1
            continue
        local, status, error = resolve_once(row, engine=active)
        row.attempts = int(row.attempts or 0) + 1
        if local:
            row.status = STATUS_RESOLVED
            row.local_path = local
            row.resolved_at = now
            row.next_retry_at = None
            row.last_error = None
            stats["resolved"] += 1
        else:
            row.last_error = error or "unresolved"
            if row.attempts >= _MAX_ATTEMPTS:
                row.status = STATUS_MISSING
                row.next_retry_at = None
                stats["missing"] += 1
            else:
                delay = _RETRY_SCHEDULE[min(row.attempts, len(_RETRY_SCHEDULE) - 1)]
                row.next_retry_at = now + timedelta(seconds=delay)
                stats["still_pending"] += 1

    try:
        with Session(active) as session:
            for row in work:
                session.merge(row)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("media_pending_commit_failed", error=str(exc)[:140])

    if stats["tried"]:
        log.info("media_pending_pass", **stats)
    return stats


def nearby_resolved_path(engine, chat_id: str, created_at,
                         window_seconds: int = 120) -> str:
    """The media file belonging to a moment in a conversation.

    A transcript row written at ingest may have no path of its own: either the
    file was not on disk yet (the timing problem this module exists for) or the
    row simply predates the column.  Matching on the conversation plus a tight
    time window finds the media row that belongs to that turn, which is what
    lets the transcript show the description instead of "[图片]".
    """
    if not chat_id or created_at is None:
        return ""
    from datetime import timedelta

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from db.models import MessageMedia

    try:
        moment = _as_datetime(created_at)
        if moment is None:
            return ""
        window = timedelta(seconds=max(5, int(window_seconds)))
        with Session(_engine(engine)) as session:
            row = session.scalars(
                select(MessageMedia)
                .where(MessageMedia.chat_id == chat_id)
                .where(MessageMedia.local_path.is_not(None))
                .where(MessageMedia.created_at >= moment - window)
                .where(MessageMedia.created_at <= moment + window)
                .order_by(MessageMedia.id.desc())
                .limit(1)
            ).first()
            if row is None:
                return ""
            path = str(row.local_path or "")
            return path if path and Path(path).exists() else ""
    except Exception as exc:  # noqa: BLE001
        log.debug("media_nearby_lookup_failed", chat_id=chat_id, error=str(exc)[:120])
        return ""


def remember_description(engine, path: str, description: str) -> int:
    """Store what a picture showed, on every media row pointing at that file.

    The in-process description cache does not survive a restart and is not
    shared between the scheduler and anything else that renders a transcript, so
    the description is kept on the row itself.  One file can back several rows
    (the same sticker sent twice, a picture quoted later), and they all get it.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from db.models import MessageMedia

    text_value = " ".join(str(description or "").split())
    target = str(path or "").strip()
    if not text_value or not target:
        return 0
    try:
        with Session(_engine(engine)) as session:
            rows = session.scalars(
                select(MessageMedia).where(MessageMedia.local_path == target)
            ).all()
            if not rows:
                # Paths are stored however the resolver produced them; compare
                # on the file name so a differently-spelled directory still hits.
                name = Path(target).name
                rows = [
                    row for row in session.scalars(select(MessageMedia)).all()
                    if row.local_path and Path(str(row.local_path)).name == name
                ]
            for row in rows:
                row.description = text_value
            session.commit()
            return len(rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("media_remember_description_failed", error=str(exc)[:140])
        return 0


def lookup_description(engine, *, path: str = "", chat_id: str = "",
                       created_at=None, window_seconds: int = 120) -> tuple[str, str]:
    """The file and description behind a transcript line.

    Returns ``("", "")`` when the picture has not been described -- either it
    never was, or its file is not known.  Callers render the bare placeholder
    in that case rather than inventing anything.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from db.models import MessageMedia

    want = str(path or "").strip()
    moment = _as_datetime(created_at)
    try:
        with Session(_engine(engine)) as session:
            row = None
            if want:
                row = session.scalars(
                    select(MessageMedia)
                    .where(MessageMedia.local_path == want)
                    .where(MessageMedia.description.is_not(None))
                    .order_by(MessageMedia.id.desc())
                    .limit(1)
                ).first()
            if row is None and chat_id and moment is not None:
                window = timedelta(seconds=max(5, int(window_seconds)))
                row = session.scalars(
                    select(MessageMedia)
                    .where(MessageMedia.chat_id == chat_id)
                    .where(MessageMedia.description.is_not(None))
                    .where(MessageMedia.created_at >= moment - window)
                    .where(MessageMedia.created_at <= moment + window)
                    .order_by(MessageMedia.id.desc())
                    .limit(1)
                ).first()
            if row is None:
                return "", ""
            return str(row.local_path or ""), str(row.description or "")
    except Exception as exc:  # noqa: BLE001
        log.debug("media_lookup_description_failed", error=str(exc)[:120])
        return "", ""


def pending_descriptions(engine, *, limit: int = 20) -> list:
    """Resolved media rows that have a file but no description yet."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from db.models import MessageMedia

    try:
        with Session(_engine(engine)) as session:
            rows = session.scalars(
                select(MessageMedia)
                .where(MessageMedia.local_path.is_not(None))
                .where((MessageMedia.description.is_(None))
                       | (MessageMedia.description == ""))
                .order_by(MessageMedia.id.desc())
                .limit(max(1, int(limit)))
            ).all()
            return list(rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("media_pending_descriptions_failed", error=str(exc)[:140])
        return []


def rows_for_chat(engine, chat_id: str, *, limit: int = 20,
                  resolved_only: bool = False) -> list:
    """Most recent media rows for a chat, newest *message* first.

    Ordered by the message's own time, not by row id.  Rows are written in
    whatever order they are registered -- a backfill inserts old pictures after
    new ones -- so id order is not time order, and treating it as such made the
    image tool hand her a twelve-day-old picture when asked about the one just
    sent.  Id is only a tiebreaker for rows sharing a timestamp.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from db.models import MessageMedia

    try:
        with Session(_engine(engine)) as session:
            query = (
                select(MessageMedia)
                .where(MessageMedia.chat_id == chat_id)
            )
            if resolved_only:
                query = query.where(MessageMedia.local_path.is_not(None))
            return list(session.scalars(
                query.order_by(MessageMedia.created_at.desc(),
                               MessageMedia.id.desc())
                .limit(max(1, int(limit)))
            ).all())
    except Exception as exc:  # noqa: BLE001
        log.warning("media_rows_query_failed", chat_id=chat_id, error=str(exc)[:140])
        return []
