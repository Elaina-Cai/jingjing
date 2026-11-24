from __future__ import annotations

import hashlib
import os


import structlog
from datetime import datetime
from pathlib import Path
from sqlalchemy import create_engine, func, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from db.models import Message, Contact, SyncState, init_db, ChatType, MessageSource, MsgSource
from db.repo_analysis import SyncStateRepository

from db.wx_schema import WxMessage, WxContact, WxMsgType, WxSubType, normalize_local_type
from db.content_extract import (
    extract_text,
    extract_media_path,
    extract_image_md5,
    extract_emoji_meta,
    is_pat_sent_by_self,
)
from db.repository import RepositoryManager
from core.bootstrap import get_account_repo
from core.conversation.media import is_complete_image
from core.infra.cache import TTLCache
from db.message_identity import global_message_id
from db.outbox_store import enqueue_message_outbox
from db.parser import (
    extract_link_url, extract_link_title,
    parse_packed_info, extract_group_sender, extract_pat_sender,
    ensure_str, resolve_msg_type_name,
)
from db.image_decrypt import decrypt_dat, decode_wxgf, decrypt_emoji_file
from safelog import warn
from core.infra.warn_guard import once

log = structlog.get_logger()


from core.infra import egress


class SyncService:
    def __init__(self, db_path: str, account_id: str = "", platform: str = "wechat",
                 context=None):
        # Make sure an existing account DB has the cursor_json column even
        # when SyncService is constructed outside AccountDBManager.
        if Path(db_path).exists():
            try:
                from db.migrations import run_migrations
                run_migrations(db_path)
            except Exception as e:
                log.warning("sync_state_migration_failed", db_path=db_path, error=str(e)[:120])
        self.engine = init_db(db_path)
        self.sync_state = SyncStateRepository(self.engine)

        # AccountContext gives us a ScopedRepository: contacts/analysis/
        # send_log resolve to this account, while meta data resolves to the
        # shared database.  get_account_repo() fails closed instead of
        # silently reading the meta DB.
        self.context = context
        self.repo = context.repo if context is not None else get_account_repo()
        self._account_id = account_id
        self._platform = platform
        self._self_wxid = ""
        self._sender_name_cache: TTLCache[str] = TTLCache(ttl_seconds=3600.0, max_entries=500, label="sender_name")
        self._name_to_wxid_cache: TTLCache[str] = TTLCache(ttl_seconds=3600.0, max_entries=500, label="name_to_wxid")
        # Per-chat "newest row we already hold", used as the visibility line
        # when a table is re-scanned outside bootstrap.  Per chat, not global.
        self._chat_boundary_cache: TTLCache[int] = TTLCache(ttl_seconds=600.0, max_entries=500, label="chat_boundary")
        try:
            from core.config.settings import config
            self._self_wxid = config.wechat.get_self_wxid()
        except Exception as e:
            log.warning("self_wxid_init_failed", error=str(e))

    def _resolve_sender_name(self, sender_id: str) -> str | None:
        if not sender_id:
            return None
        cached = self._sender_name_cache.get(sender_id)
        if cached is not None:
            return cached or None
        try:
            name = ""
            with Session(self.engine) as s:
                contact = s.get(Contact, sender_id)
                if contact:
                    name = contact.remark or contact.nickname or ""
            if not name:
                contact = self.repo.contacts.get_by_wxid(sender_id)
                if contact:
                    name = contact.remark or contact.nickname or ""
            self._sender_name_cache.put(sender_id, name)
            return name or None
        except Exception as e:
            log.warning("sender_name_resolve_failed", sender_id=sender_id, error=str(e))
            self._sender_name_cache.put(sender_id, "")
            return None

    def _resolve_name_to_wxid(self, display_name: str) -> str:
        """Best-effort reverse lookup: a display name back to its wxid.

        Pats are the one message kind whose wire format names the sender in
        prose (``"P5" 拍了拍 "鲸鲸"``) and carries no id at all, so the name
        is the only handle available and it has to be resolved back through the
        contact tables.  Returns "" when the name is unknown, which is honest:
        the caller keeps the prose name rather than inventing an id.
        """
        name = str(display_name or "").strip()
        if not name:
            return ""
        cached = self._name_to_wxid_cache.get(name)
        if cached is not None:
            return cached
        found = ""
        try:
            with Session(self.engine) as s:
                row = s.execute(
                    select(Contact.wxid).where(
                        or_(
                            func.coalesce(Contact.remark, "") == name,
                            func.coalesce(Contact.nickname, "") == name,
                        )
                    ).limit(1)
                ).scalar()
                if row:
                    found = str(row)
            if not found:
                for contact in self.repo.contacts.list_all():
                    if name in (
                        getattr(contact, "remark", "") or "",
                        getattr(contact, "nickname", "") or "",
                    ):
                        found = str(getattr(contact, "wxid", "") or "")
                        break
        except Exception as e:
            log.debug("name_to_wxid_resolve_failed", name=name, error=str(e)[:120])
        self._name_to_wxid_cache.put(name, found)
        return found

    def _global_seen_boundary(self) -> int:
        """Newest ``created_at`` we already hold, as a fallback visibility line.

        Only used when a chat has no rows of its own yet (see
        :meth:`_seen_boundary_for`).  Best-effort: any failure returns 0, which
        disables the boundary rather than hiding everything.
        """
        try:
            with Session(self.engine) as own_session:
                value = own_session.execute(
                    text("SELECT MAX(created_at) FROM messages")
                ).scalar()
        except Exception as e:  # noqa: BLE001 - boundary is best-effort
            log.debug("sync_boundary_probe_failed", error=str(e)[:120])
            return 0
        return self._as_epoch(value)

    def _seen_boundary_for(self, chat_id: str) -> int:
        """Newest ``created_at`` we already hold *for this chat*.

        The visibility boundary must be per conversation.  A single global MAX
        would let one busy chat's newest row become the line for every other
        chat, so a message that genuinely just arrived in a quiet chat could be
        filed as history merely because its ``create_time`` sits a little below
        another chat's latest.  Falls back to the global value only when this
        chat has no rows yet (nothing to compare against).
        """
        key = str(chat_id or "")
        if not key:
            return self._global_seen_boundary()
        cached = self._chat_boundary_cache.get(key)
        if cached is not None:
            return cached
        value = 0
        try:
            with Session(self.engine) as own_session:
                row = own_session.execute(
                    text("SELECT MAX(created_at) FROM messages WHERE chat_id = :c"),
                    {"c": key},
                ).scalar()
            value = self._as_epoch(row)
        except Exception as e:  # noqa: BLE001 - boundary is best-effort
            log.debug("sync_chat_boundary_probe_failed",
                      chat_id=key, error=str(e)[:120])
        if value <= 0:
            value = self._global_seen_boundary()
        self._chat_boundary_cache.put(key, value)
        return value

    @staticmethod
    def _as_epoch(value) -> int:
        """Coerce a ``created_at`` column value to an int epoch, or 0."""
        if isinstance(value, datetime):
            return int(value.timestamp())
        if value:
            try:
                return int(
                    datetime.fromisoformat(
                        str(value).replace(" ", "T", 1)
                    ).timestamp()
                )
            except Exception:  # noqa: BLE001 - unparseable -> no boundary
                warn("db/sync_service.py:200 Exception", err=repr(__import__("sys").exc_info()[1]))
                return 0
        return 0

    def sync_messages(
        self,
        wx_msg_db_path: str,
        source: str = MessageSource.WECHAT,
        batch_size: int = 5000,
    ) -> int:
        last_sync_ts = self.sync_state.get_last_sync_ts(source)
        table_cursors = self.sync_state.get_cursors(source)

        # One-time migration path: older versions stored a wall-clock cursor
        # in the legacy DB and no per-table cursor at all.  On the first run
        # after the upgrade we scan the affected Msg_* tables once, but only
        # quietly backfill rows at/behind the legacy cursor (they belong to
        # history); rows after it are genuinely new and must stay visible to
        # the event pipeline.  A bounded backfill window keeps this cheap on
        # large installations.
        #
        # ``hide_before_ts`` is the *visibility* boundary and is deliberately
        # independent of ``bootstrap_mode`` (which is only a cost control for
        # how far back we bother to read).  Tying the two together was a real
        # bug: a table whose cursor is reset to -1 -- which happens on
        # ``sync_table_cursor_reset`` and on any table seen for the first time --
        # gets a full re-scan, and every replayed row was stamped ``now()`` as
        # if it had just arrived.  On 2026-09-15 that replayed three months of
        # history into a live group: she read it all aloud and answered
        # messages from May.  Anything older than the boundary is history,
        # whichever scan happens to find it.
        bootstrap_mode = not table_cursors
        legacy_sync_ts = 0
        backfill_min_ts = 0
        backfill_days = 30
        if bootstrap_mode:
            # Boundary = max(legacy wall-clock cursor, newest row already in
            # this account DB).  Taking the max also protects account-scoped
            # installs where the legacy cursor belongs to a different account:
            # rows already persisted here are never replayed as new.
            legacy_ts = 0
            try:
                legacy_ts = int(self.repo.sync_state.get_last_sync_ts(source) or 0)
            except Exception:
                warn("db/sync_service.py:243 Exception", err=repr(__import__("sys").exc_info()[1]))
                legacy_ts = 0

            account_max_ts = 0
            try:
                with Session(self.engine) as own_session:
                    account_max = own_session.execute(
                        text("SELECT MAX(created_at) FROM messages")
                    ).scalar()
                if isinstance(account_max, datetime):
                    account_max_ts = int(account_max.timestamp())
                elif account_max:
                    account_max_ts = int(
                        datetime.fromisoformat(
                            str(account_max).replace(" ", "T", 1)
                        ).timestamp()
                    )
            except Exception as e:
                log.warning("sync_bootstrap_boundary_failed", error=str(e)[:120])
                account_max_ts = 0

            legacy_sync_ts = max(legacy_ts, account_max_ts)
            if legacy_sync_ts > 0:
                try:
                    backfill_days = int(os.environ.get("WX_SYNC_BACKFILL_MAX_AGE_DAYS", "30") or "30")
                except ValueError:
                    warn("db/sync_service.py:268 ValueError", err=repr(__import__("sys").exc_info()[1]))
                    backfill_days = 30
                backfill_min_ts = max(0, legacy_sync_ts - max(0, backfill_days) * 86400)
        else:
            # Not a bootstrap run, but the boundary still has to exist: a
            # per-table cursor reset (see below) re-reads a whole table, and
            # without a boundary every one of those rows would be published as
            # new.  ``_seen_boundary_for`` answers per chat, because a single
            # global MAX across the whole ``messages`` table is too blunt: one
            # busy chat's newest row would become the visibility line for every
            # other chat, and a live message that arrived with a slightly older
            # create_time (clock skew, WeChat's own write order) would be
            # silently filed as history.  Resolved lazily and cached per chat.
            legacy_sync_ts = self._global_seen_boundary()

        # Read-only source database: NullPool closes the SQLite file handle as
        # soon as the Session is done.  A pooled connection here keeps
        # merge_msg.db open and makes the next decrypt pass fail on Windows
        # with [WinError 32] "file is in use".
        wx_engine = create_engine(
            f"sqlite:///{wx_msg_db_path}",
            poolclass=NullPool,
        )
        total = 0

        with Session(wx_engine) as wx_session:
            name2id_rows = wx_session.execute(
                text("SELECT rowid, user_name FROM Name2Id")
            ).fetchall()
            user_names = []
            sender_id_map = {}
            for row in name2id_rows:
                name = ensure_str(row[1]) if row[1] else ""
                if name:
                    user_names.append(name)
                sender_id_map[row[0]] = name
            hash_to_name = {}
            for name in user_names:
                h = hashlib.md5(name.encode("utf-8")).hexdigest()
                hash_to_name[h] = name

            msg_tables = wx_session.execute(
                text("SELECT name FROM sqlite_master WHERE name LIKE 'Msg_%' AND type='table'")
            ).fetchall()

            if not msg_tables:
                log.info("sync_no_msg_tables")
                return 0

            db_max_ts = 0
            for (table_name,) in msg_tables:
                try:
                    mx = wx_session.execute(
                        text(f"SELECT MAX(create_time) FROM [{table_name}]")
                    ).scalar()
                    if mx and mx > db_max_ts:
                        db_max_ts = mx
                except Exception as e:
                    log.debug("ts_parse_skip", error=str(e)[:60])
                    continue

            # Older versions stored a wall-clock create_time cursor.  If the
            # raw DB was restored/rolled back its local_id sequence may have
            # restarted as well, so drop the cursors and rescan once instead
            # of silently skipping the restored table forever.
            if db_max_ts > 0 and db_max_ts < last_sync_ts - 86400:
                log.warning("sync_db_rolled_back",
                            db_max_ts=db_max_ts, last_sync_ts=last_sync_ts,
                            action="reset_cursors")
                table_cursors = {}
                # Re-scan, but treat rows already behind the old cursor as
                # historical backfill.  A long-idle database can also trigger
                # this branch; in that case nothing should be replayed as new.
                #
                # The boundary must be the *older* of the two, not
                # ``last_sync_ts``: this branch only runs when the raw DB's
                # newest row is already more than a day behind that cursor, so
                # using ``last_sync_ts`` would put the line above every row in
                # the database and hide all of them -- including messages that
                # genuinely arrived after the restore.  She would go silent in
                # every chat until a row's create_time climbed back past it.
                # ``db_max_ts`` is the real "we have nothing newer than this"
                # point in the rolled-back source; duplicate protection for the
                # already-seen rows comes from the ``messages.msg_id`` primary
                # key, not from this visibility line.
                bootstrap_mode = True
                legacy_sync_ts = min(last_sync_ts, db_max_ts)
                backfill_min_ts = max(0, legacy_sync_ts - max(0, backfill_days) * 86400)

            table_plans = []
            all_new_count = 0
            for (table_name,) in msg_tables:
                try:
                    max_local_id = wx_session.execute(
                        text(f"SELECT MAX(local_id) FROM [{table_name}]")
                    ).scalar()
                except Exception as e:
                    log.warning("table_meta_query_failed", table=table_name, error=str(e)[:80])
                    continue

                cursor = table_cursors.get(table_name)
                if cursor is None:
                    # Never seen this table before.  Scan it from local_id=-1
                    # (includes a possible local_id=0).  The messages.msg_id
                    # primary-key check keeps this one-time reconcile
                    # idempotent, and it also recovers rows missed by the old
                    # wall-clock cursor implementation.
                    cursor = -1
                elif max_local_id is None or int(max_local_id) < int(cursor):
                    log.warning(
                        "sync_table_cursor_reset",
                        table=table_name,
                        old_cursor=cursor,
                        max_local_id=max_local_id,
                    )
                    cursor = -1

                # Reading depth: during a bootstrap we also cap how far back we
                # read at all (cost control).  Outside bootstrap, and on a
                # cursor reset, we read the whole table but the boundary below
                # still decides what counts as new.
                min_create_time = backfill_min_ts if bootstrap_mode else 0
                cnt = wx_session.execute(
                    text(
                        f'SELECT COUNT(*) FROM [{table_name}] '
                        f'WHERE local_id > :cursor AND create_time >= :min_ts'
                    ),
                    {"cursor": cursor, "min_ts": min_create_time},
                ).scalar()
                cnt = int(cnt or 0)
                all_new_count += cnt
                table_plans.append({
                    "table": table_name,
                    "cursor": int(cursor),
                    "max_local_id": int(max_local_id) if max_local_id is not None else int(cursor),
                    "new_count": cnt,
                    "min_create_time": int(min_create_time),
                    "hide_before_ts": int(legacy_sync_ts),
                })

            if all_new_count == 0:
                log.info("sync_no_new_messages")
                return 0

            log.info(
                "sync_start",
                new_count=all_new_count,
                tables=len(table_plans),
                cursors=len(table_cursors),
            )

            failed_tables: set[str] = set()
            final_cursors = dict(table_cursors)
            # Media rows to create once this batch is committed and the message
            # ids exist.  Collected per batch, registered after the commit so
            # nothing is added to the ingest transaction itself.
            media_pending: list[dict] = []
            for plan in table_plans:
                table_name = plan["table"]
                cursor = plan["cursor"]
                offset = 0
                table_failed = False
                while True:
                    try:
                        rows = wx_session.execute(
                            text(
                                f"SELECT local_id, server_id, local_type, "
                                f"real_sender_id, create_time, "
                                f"message_content, compress_content, packed_info_data, source, "
                                f"sub_type, DisplayContent "
                                f"FROM [{table_name}] "
                                f"WHERE local_id > :cursor AND create_time >= :min_ts "
                                f"ORDER BY local_id ASC LIMIT :limit OFFSET :offset"
                            ),
                            {
                                "cursor": cursor,
                                "min_ts": plan.get("min_create_time", 0),
                                "limit": batch_size,
                                "offset": offset,
                            },
                        ).fetchall()
                    except Exception as _exc:  # noqa: silent(老库缺列属正常：每表只报一次，见下)
                        # 2026-10-24: 老库没有 sub_type / DisplayContent(微信 3.x)，走下面的
                        # fallback 是正常的；但每批都报一次太吵 => 每张表只报一次。
                        # 2026-10-27: 每张表每 6 小时最多说一次（进程内），
                        # 否则每 2 分钟一轮 sync 就会刷 5 行同样的告警。
                        if once("legacy_columns:" + table_name, every_s=6 * 3600):
                            warn("db/sync_service.legacy_columns", table=table_name, err=repr(_exc))
                        try:
                            fallback_rows = wx_session.execute(
                                text(
                                    f"SELECT local_id, server_id, local_type, "
                                    f"real_sender_id, create_time, "
                                    f"message_content, compress_content, packed_info_data, source "
                                    f"FROM [{table_name}] "
                                    f"WHERE local_id > :cursor AND create_time >= :min_ts "
                                    f"ORDER BY local_id ASC LIMIT :limit OFFSET :offset"
                                ),
                                {
                                    "cursor": cursor,
                                    "min_ts": plan.get("min_create_time", 0),
                                    "limit": batch_size,
                                    "offset": offset,
                                },
                            ).fetchall()
                            rows = [tuple(r) + (0, "") for r in fallback_rows]
                        except Exception as e:
                            log.warning(
                                "batch_query_failed",
                                table=table_name,
                                cursor=cursor,
                                offset=offset,
                                error=str(e)[:80],
                            )
                            table_failed = True
                            break

                    if not rows:
                        break

                    with Session(self.engine) as own_session:
                        for row in rows:
                            asr_text = ""
                            hash_id = table_name.replace("Msg_", "")
                            talker = hash_to_name.get(hash_id, hash_id)
                            real_sender_raw = row[3]
                            real_sender = str(real_sender_raw) if real_sender_raw else ""
                            source_raw = row[8] if len(row) > 8 else ""
                            source_str = ""
                            if isinstance(source_raw, str):
                                source_str = source_raw
                            elif isinstance(source_raw, bytes):
                                try:
                                    source_str = source_raw.decode("utf-8", errors="ignore")
                                except Exception:
                                    warn("db/sync_service.py:496 Exception", err=repr(__import__("sys").exc_info()[1]))
                            is_self = 1 if "<msgsource>" in source_str else 0

                            raw_sub_type = row[9] if len(row) > 9 else 0
                            try:
                                raw_sub_type = int(raw_sub_type)
                            except (ValueError, TypeError):
                                warn("db/sync_service.py:503 (ValueError, TypeError)", err=repr(__import__("sys").exc_info()[1]))
                                raw_sub_type = 0
                            raw_display_content = ensure_str(row[10]) if len(row) > 10 else ""

                            raw_type = row[2]
                            msg_type_val, raw_sub_type = normalize_local_type(
                                raw_type, raw_sub_type,
                            )

                            wx_msg = WxMessage(
                                local_id=row[0],
                                msg_svr_id=row[1],
                                type=msg_type_val,
                                sub_type=raw_sub_type,
                                is_sender=is_self,
                                create_time=row[4],
                                str_talker=talker,
                                str_content=ensure_str(row[5]),
                                display_content=raw_display_content,
                                compress_content=row[6] or b"",
                                bytes_extra=row[7] or b"",
                                real_sender_id=real_sender,
                            )

                            if wx_msg.type == WxMsgType.SYSTEM and wx_msg.sub_type == 0:
                                _content_check = wx_msg.display_content or wx_msg.str_content or ""
                                if "拍了拍" in _content_check:
                                    wx_msg.sub_type = 4
                                    log.debug("pat_detected_by_content", msg_id=wx_msg.msg_svr_id)

                            if wx_msg.type == WxMsgType.SHARE_LINK and wx_msg.sub_type in (
                                WxSubType.QUOTE_REPLY_33, WxSubType.QUOTE_REPLY_36, WxSubType.QUOTE_REPLY_57,
                            ):
                                from db.content_extract import _decompress_xml
                                _xml = _decompress_xml(wx_msg)
                                if "<refermsg>" not in _xml:
                                    wx_msg.sub_type = 0
                                    log.debug(
                                        "quote_sub_type_demoted",
                                        msg_id=wx_msg.msg_svr_id,
                                        reason="no_refermsg_in_xml",
                                    )

                            if not wx_msg.is_processable:
                                continue

                            message_identity = global_message_id(
                                source, self._account_id, talker, wx_msg.msg_svr_id,
                            )
                            existing = own_session.get(Message, message_identity)
                            if existing is None:
                                # Rows migrated from the single-account era may
                                # have an empty account_id, so fall back to the
                                # platform identity fields before inserting.
                                existing = own_session.execute(
                                    select(Message).where(
                                        Message.msg_id == wx_msg.msg_svr_id,
                                        Message.source == source,
                                        Message.chat_id == talker,
                                    ).limit(1)
                                ).scalar_one_or_none()
                            if existing:
                                continue

                            content = extract_text(wx_msg)

                            if wx_msg.type == WxMsgType.VOICE and not content:
                                content = "[语音消息]"

                            if not content:
                                continue

                            # Pat sources are plaintext
                            # `<msgsource><tmp_node/></msgsource>`, so the
                            # generic source check often marks incoming pats
                            # as self-sent.  The title is authoritative here:
                            # `我拍了拍…` means outgoing, `"X" 拍了拍我`
                            # means incoming.
                            if wx_msg.type == WxMsgType.PAT:
                                wx_msg.is_sender = 1 if is_pat_sent_by_self(content) else 0

                            packed_wxids = parse_packed_info(wx_msg.bytes_extra)

                            sender_wxid = ""
                            pat_author_name = ""
                            is_group = bool(talker and talker.endswith("@chatroom"))
                            if is_group:
                                sender_wxid, content = extract_group_sender(content)
                                if not sender_wxid and packed_wxids:
                                    sender_wxid = packed_wxids[0]
                                if not sender_wxid and wx_msg.real_sender_id:
                                    try:
                                        rid = int(wx_msg.real_sender_id)
                                        sender_wxid = sender_id_map.get(rid, "")
                                    except (ValueError, TypeError):
                                        warn("db/sync_service.py:597 (ValueError, TypeError)", err=repr(__import__("sys").exc_info()[1]))
                                # A pat carries no id anywhere -- its wire form
                                # is `"P5" 拍了拍 "鲸鲸"`.  Without this branch
                                # every group pat fell through to "unknown", so
                                # all of them collapsed onto one identity and
                                # per-speaker pat counting was impossible; the
                                # speaker was only ever in the prose.  Parse the
                                # name and resolve it back through contacts.
                                if not sender_wxid and wx_msg.type == WxMsgType.PAT:
                                    pat_author_name = extract_pat_sender(content)
                                    if pat_author_name:
                                        sender_wxid = self._resolve_name_to_wxid(
                                            pat_author_name
                                        )
                                        log.debug(
                                            "pat_author_resolved",
                                            name=pat_author_name,
                                            wxid=sender_wxid or "(unresolved)",
                                        )
                            else:
                                if packed_wxids:
                                    sender_wxid = packed_wxids[0]
                                if not sender_wxid:
                                    sender_wxid = self._extract_sender_wxid(wx_msg)

                            if wx_msg.is_sender and sender_wxid:
                                if not self._self_wxid:
                                    self._self_wxid = sender_wxid
                                    log.info("self_wxid_detected", wxid=sender_wxid)

                            mention_self = bool(
                                self._self_wxid and self._self_wxid in packed_wxids
                            )

                            media_path = extract_media_path(wx_msg) or None

                            # The clue, captured whether or not a file was found.
                            # WeChat writes an image's thumbnail seconds to tens
                            # of seconds after the message, so a sync pass that
                            # runs in between finds nothing -- and without this
                            # the md5 was lost and the picture stayed invisible
                            # forever.  See core.conversation.media_registry.
                            media_md5 = ""
                            media_kind = ""
                            if wx_msg.type == WxMsgType.IMAGE:
                                media_md5 = extract_image_md5(wx_msg)
                                media_kind = "image"
                            elif wx_msg.type == WxMsgType.EMOJI:
                                media_md5 = extract_image_md5(wx_msg)
                                media_kind = "emoji"
                            extra_data = {}
                            if sender_wxid:
                                extra_data["sender_wxid"] = sender_wxid
                            if packed_wxids:
                                extra_data["mentioned_wxids"] = packed_wxids
                            if mention_self:
                                extra_data["mention_self"] = True
                            if media_path:
                                extra_data["media_path"] = media_path
                            if wx_msg.type == WxMsgType.IMAGE and media_path:
                                original_media_path = media_path
                                img_cache = self._cache_image_dat(media_path)
                                if img_cache:
                                    media_path = img_cache
                                else:
                                    media_path = original_media_path
                            if wx_msg.type == WxMsgType.EMOJI:
                                emoji_meta = extract_emoji_meta(wx_msg)
                                emoji_md5 = emoji_meta.get("md5", "")
                                if emoji_meta.get("cdn_url"):
                                    extra_data["emoji_url"] = emoji_meta["cdn_url"]
                                if emoji_md5:
                                    extra_data["emoji_md5"] = emoji_md5
                                if emoji_meta.get("cdn_url") and emoji_md5:
                                    self._prefetch_emoji(emoji_meta["cdn_url"], emoji_md5)
                                if media_path:
                                    img_cache = self._cache_image_dat(media_path)
                                    if img_cache:
                                        media_path = img_cache
                                elif emoji_md5:
                                    emoji_cache = Path("data/emoji_cache") / f"{emoji_md5}.dat"
                                    if emoji_cache.exists() and emoji_cache.stat().st_size > 0:
                                        media_path = str(emoji_cache)
                                elif not emoji_md5:
                                    from db.content_extract import _extract_all_strings
                                    all_strings = _extract_all_strings(wx_msg.bytes_extra or b"")
                                    import re
                                    md5_pattern = re.compile(r"[0-9a-f]{32}")
                                    for s in all_strings:
                                        s = s.strip().strip('"').strip()
                                        if md5_pattern.fullmatch(s):
                                            emoji_md5 = s
                                            extra_data["emoji_md5"] = emoji_md5
                                            from db.content_extract import _resolve_emoji_local
                                            local_path = _resolve_emoji_local(emoji_md5)
                                            if local_path:
                                                media_path = local_path
                                            break
                            if wx_msg.type == WxMsgType.VOICE and media_path:
                                asr_text = self._transcribe_voice(media_path)
                                if asr_text:
                                    content = f"[语音] {asr_text}"
                                    # 2026-10-27：这个标记原来被写在下面的表情包分支里（
                                    # 复制粘贴串行），于是语音没标上、表情包反倒标上了
                                    extra_data["voice_transcribed"] = True

                            if wx_msg.type == WxMsgType.EMOJI and emoji_md5:
                                if not emoji_meta.get("desc"):
                                    try:
                                        from core.preprocess.emoji_recognition import schedule_emoji_recognition
                                        schedule_emoji_recognition(emoji_md5)
                                    except Exception:
                                        warn("db/sync_service.py:706 Exception", err=repr(__import__("sys").exc_info()[1]))

                            from core.ingestion.platform_messages import WechatMessage
                            from core.ingestion.message_bridge import inbound_to_message

                            wx_platform_msg = WechatMessage(
                                msg_svr_id=wx_msg.msg_svr_id,
                                local_id=wx_msg.local_id,
                                type=wx_msg.type,
                                sub_type=wx_msg.sub_type,
                                is_sender=wx_msg.is_sender,
                                create_time=wx_msg.create_time,
                                str_talker=wx_msg.str_talker,
                                str_content=wx_msg.str_content,
                                compress_content=wx_msg.compress_content,
                                bytes_extra=wx_msg.bytes_extra,
                                real_sender_id=wx_msg.real_sender_id,
                                sender_wxid=sender_wxid or self._extract_sender_wxid(wx_msg),
                                sender_name=(
                                    self._resolve_sender_name(
                                        sender_wxid or self._extract_sender_wxid(wx_msg)
                                    )
                                    # The pat author's name is known even when it
                                    # cannot be resolved to a contact.  Keeping it
                                    # is what lets the transcript read
                                    # "[..] P5: "P5" 拍了拍 "鲸鲸"" instead of
                                    # an un-attributable "unknown:" line.
                                    or pat_author_name
                                    or None
                                ),
                                extracted_content=content,
                                media_path=media_path or "",
                                emoji_md5=extra_data.get("emoji_md5", ""),
                                emoji_url=extra_data.get("emoji_url", ""),
                                link_url=extract_link_url(wx_msg) if wx_msg.type == WxMsgType.SHARE_LINK else "",
                                link_title=extract_link_title(wx_msg) if wx_msg.type == WxMsgType.SHARE_LINK else "",
                                mentioned_wxids=packed_wxids or [],
                                voice_transcribed=extra_data.get("voice_transcribed", False),
                                voice_text=asr_text if extra_data.get("voice_transcribed") else "",
                            )

                            inbound = wx_platform_msg.to_inbound()
                            inbound.extra["account_id"] = self._account_id

                            if mention_self:
                                inbound.extra["mention_self"] = True
                            if sender_wxid:
                                inbound.extra["sender_wxid"] = sender_wxid
                            if packed_wxids:
                                inbound.extra["mentioned_wxids"] = packed_wxids

                            msg = inbound_to_message(inbound)
                            msg.source = source
                            msg.account_id = self._account_id
                            # Visibility is decided by the row's own timestamp
                            # against the sync boundary, never by which scan
                            # found it.  A row older than the boundary is
                            # history: it stays in the DB for search/memory but
                            # sync_start filters it out of NEW_MESSAGES_SYNCED,
                            # so old chats are never re-analyzed or replied to.
                            #
                            # Bootstrap and the rolled-back path use one fixed
                            # boundary for the whole run.  A plain re-scan uses
                            # a per-chat line instead, so one chat's traffic
                            # cannot silence another.
                            boundary = (
                                legacy_sync_ts if bootstrap_mode
                                else self._seen_boundary_for(talker)
                            )
                            if boundary and wx_msg.create_time <= boundary:
                                msg.synced_at = datetime.fromtimestamp(0)
                            else:
                                msg.synced_at = datetime.now()

                            if wx_msg.is_sender:
                                send_log = self.repo.send_log.claim_pending(source, talker, session=own_session)
                                if send_log:
                                    msg.msg_source = send_log.source
                                    if msg.msg_id:
                                        self.repo.send_log.confirm_with_msg_id(send_log.id, msg.msg_id, session=own_session)
                                else:
                                    msg.msg_source = MsgSource.USER_DIRECT

                            own_session.add(msg)
                            if not boundary or wx_msg.create_time > boundary:
                                enqueue_message_outbox(own_session, msg)
                            total += 1
                            # A reply quotes someone else's message; if that
                            # message had a picture, this is the only way the
                            # picture is reachable -- WeChat gives the reply
                            # nothing but a "[媒体消息]" placeholder.
                            #
                            # Wrapped as a whole: this is bookkeeping that only
                            # matters to the media layer, and an exception here
                            # used to abort the entire ingest batch -- one wrong
                            # attribute name meant no message was stored at all
                            # and she silently stopped answering.
                            try:
                                quote_ref = str(
                                    getattr(msg, "quote_reply_id", "") or ""
                                ).strip()
                                if media_kind or quote_ref:
                                    media_pending.append({
                                        "message": msg,
                                        # The raw object carries the platform id
                                        # as msg_svr_id; WxMessage has no msg_id.
                                        "msg_id": int(wx_msg.msg_svr_id or 0),
                                        "kind": media_kind,
                                        "md5": media_md5,
                                        "source_path": str(media_path or ""),
                                        "quote_ref": quote_ref,
                                        "chat_id": str(talker or ""),
                                        "sender_id": str(sender_wxid or ""),
                                        "created_at": datetime.fromtimestamp(
                                            wx_msg.create_time
                                        ) if wx_msg.create_time else None,
                                    })
                            except Exception as exc:  # noqa: BLE001
                                log.warning("media_collect_failed",
                                            msg_svr_id=getattr(wx_msg, "msg_svr_id", 0),
                                            error=str(exc)[:140])

                        own_session.commit()

                        # After the commit, so message ids are assigned.  Every
                        # media message gets a row whether or not a file was
                        # found: the row is what makes a retry possible.
                        if media_pending:
                            try:
                                from core.conversation.media_registry import (
                                    register_from_message,
                                )

                                for item in media_pending:
                                    register_from_message(
                                        self.engine,
                                        message_id=getattr(item["message"], "id", None),
                                        msg_id=item["msg_id"] or None,
                                        chat_id=item["chat_id"],
                                        sender_id=item["sender_id"],
                                        kind=item["kind"],
                                        md5=item["md5"],
                                        media_path=item["source_path"],
                                        quote_ref=item["quote_ref"],
                                        account_id=self._account_id,
                                        platform=self._platform,
                                        created_at=item["created_at"],
                                    )
                            except Exception as exc:  # noqa: BLE001
                                log.warning("media_register_batch_failed",
                                            count=len(media_pending),
                                            error=str(exc)[:140])
                            media_pending.clear()

                    offset += batch_size

                if table_failed:
                    failed_tables.add(table_name)
                    continue
                final_cursors[table_name] = plan["max_local_id"] if plan["max_local_id"] is not None else plan["cursor"]

        self.sync_state.update_cursors(
            source,
            final_cursors,
            status="partial" if failed_tables else "ok",
            error_msg=(
                "table_query_failed: " + ",".join(sorted(failed_tables))
                if failed_tables else None
            ),
        )

        try:
            cleaned = self.repo.send_log.cleanup_stale(timeout_minutes=5)
            if cleaned:
                log.info("send_log_cleanup", stale_count=cleaned)
        except Exception as e:
            log.warning("send_log_cleanup_failed", error=str(e))

        log.info("sync_done", total_synced=total)
        return total

    def sync_contacts(self, wx_contact_db_path: str, source: str = MessageSource.WECHAT) -> int:
        # See sync_messages: NullPool releases contact.db before the next
        # decrypt pass tries to overwrite it.
        wx_engine = create_engine(
            f"sqlite:///{wx_contact_db_path}",
            poolclass=NullPool,
        )
        total = 0

        _SYSTEM_ACCOUNTS = {
            "filehelper", "weixin", "fmessage", "floatbottle",
            "medianote", "newsapp", "notifymessage",
            "wxfilehelper", "weixinguanjia",
        }

        with Session(wx_engine) as wx_session:
            try:
                rows = wx_session.execute(
                    text(
                        "SELECT username, nick_name, remark, alias, "
                        "big_head_url, small_head_url "
                        "FROM contact WHERE username NOT LIKE 'gh_%'"
                    )
                ).fetchall()
            except Exception as e:
                log.warning("msg_query_fallback", error=str(e)[:80])
                rows = wx_session.execute(
                    text(
                        "SELECT username, nick_name, remark, alias, "
                        "big_head_url, small_head_url "
                        "FROM contact"
                    )
                ).fetchall()

            with Session(self.engine) as own_session:
                for row in rows:
                    wx_contact = WxContact(
                        username=row[0],
                        nickname=row[1] or "",
                        remark=row[2] or "",
                        alias=row[3] or "",
                        avatar_url=row[4] or row[5] or "",
                    )

                    if wx_contact.username in _SYSTEM_ACCOUNTS:
                        continue
                    if not wx_contact.is_group and not wx_contact.username.startswith("wxid_") and not wx_contact.username.startswith("v1_"):
                        if not wx_contact.nickname and not wx_contact.remark:
                            continue

                    existing = own_session.get(Contact, wx_contact.username)
                    if existing:
                        existing.nickname = wx_contact.nickname
                        existing.remark = wx_contact.remark
                        existing.avatar_path = wx_contact.avatar_url
                        existing.is_group = 1 if wx_contact.is_group else 0
                        existing.account_id = self._account_id
                        existing.updated_at = datetime.now()
                    else:
                        c = Contact(
                            wxid=wx_contact.username,
                            nickname=wx_contact.nickname,
                            remark=wx_contact.remark,
                            avatar_path=wx_contact.avatar_url,
                            is_group=1 if wx_contact.is_group else 0,
                            account_id=self._account_id,
                            updated_at=datetime.now(),
                        )
                        own_session.add(c)
                    total += 1

                own_session.commit()

        log.info("sync_contacts_done", total=total)
        return total

    @staticmethod
    def _prefetch_emoji(url: str, md5: str):
        from pathlib import Path as _Path
        cache_dir = _Path("data/emoji_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{md5}.dat"
        if cache_file.exists() and cache_file.stat().st_size > 0:
            return
        try:
            with egress.client(timeout_ms=15_000) as client:
                resp = client.get(url)
                resp.raise_for_status()
                cache_file.write_bytes(resp.content)
            log.info("emoji_prefetched", md5=md5, size=len(resp.content))
        except Exception as e:
            log.warning("emoji_prefetch_failed", md5=md5, url=url[:80], error=str(e))

    @staticmethod
    def _cache_image_dat(dat_path: str) -> str:
        from pathlib import Path as _Path

        src = _Path(dat_path)
        if not src.exists():
            return ""

        is_emoji = src.suffix.lower() not in (".dat", ".jpg", ".jpeg", ".png", ".gif", ".webp")
        if not is_emoji and src.suffix.lower() not in (".dat", ".jpg", ".jpeg", ".png", ".gif", ".webp"):
            return ""

        cache_dir = _Path("data/image_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)

        stem = src.stem
        for cached in cache_dir.iterdir():
            if cached.stem != stem:
                continue
            # 缓存命中用**媒体完整性的唯一判据**（core/conversation/media.py）。
            # 这里以前只问 ``st_size > 0``：一次截断的写入（缺 EOI/IEND 的那批图）
            # 就被永久当成"已缓存"，于是"以后再试一次"永远不会发生，坏图一直被
            # 交给视觉供应商换回 400 unsupported image。判据本身不在这里复制
            # 第二份——坏图不算命中，允许下一次重试。
            if is_complete_image(str(cached)):
                return str(cached)
            log.info("image_cache_hit_incomplete", src=str(src)[:80],
                     cached=cached.name)

        if src.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            cache_file = cache_dir / src.name
            cache_file.write_bytes(src.read_bytes())
            return str(cache_file)

        try:
            raw = src.read_bytes()

            if is_emoji:
                dec_ext = decrypt_emoji_file(raw, stem)
                if dec_ext:
                    decrypted, ext = dec_ext
                    cache_file = cache_dir / f"{stem}.{ext}"
                    cache_file.write_bytes(decrypted)
                    log.info("emoji_cached", src=str(src)[:80], ext=ext, size=len(decrypted))
                    return str(cache_file)
                log.warning("emoji_decrypt_failed", src=str(src)[:80])
                return ""

            decrypted, ext = decrypt_dat(raw)
            if decrypted and ext:
                if ext == "wxgf":
                    png_data = decode_wxgf(decrypted)
                    if png_data:
                        cache_file = cache_dir / f"{stem}.png"
                        cache_file.write_bytes(png_data)
                        log.info("image_cached_wxgf", src=str(src)[:80], size=len(png_data))
                        return str(cache_file)
                    log.warning("wxgf_decode_failed", src=str(src)[:80])
                    return ""

                cache_file = cache_dir / f"{stem}.{ext}"
                cache_file.write_bytes(decrypted)
                log.info("image_cached", src=str(src)[:80], ext=ext, size=len(decrypted))
                return str(cache_file)
            log.warning("image_decrypt_failed", src=str(src)[:80], reason="decryption_returned_empty")
        except Exception as e:
            log.warning("image_cache_error", src=str(src)[:80], error=str(e))

        return ""

    @staticmethod
    def _extract_sender_wxid(msg: WxMessage) -> str:
        if msg.is_sender:
            return "self"
        if not msg.is_group:
            return msg.str_talker
        return "unknown"

    def _transcribe_voice(self, media_path: str) -> str:
        try:
            import whisper
        except ImportError:
            log.debug("whisper_not_installed", path=media_path)
            return ""

        voice_path = Path(media_path)
        if not voice_path.exists():
            log.debug("voice_file_not_found", path=media_path)
            return ""

        try:
            if voice_path.suffix.lower() not in (".wav", ".mp3", ".m4a", ".ogg", ".flac", ".amr", ".silk"):
                converted = self._convert_voice_to_wav(media_path)
                if converted:
                    voice_path = Path(converted)
                else:
                    log.debug("voice_convert_failed", path=media_path)
                    return ""

            if not hasattr(self, "_whisper_model"):
                log.info("whisper_loading", model="base")
                self._whisper_model = whisper.load_model("base")

            result = self._whisper_model.transcribe(
                str(voice_path),
                language="zh",
                fp16=False,
            )
            text = result.get("text", "").strip()
            if text and len(text) > 1:
                log.info("voice_transcribed", path=media_path, text=text[:50])
                return text
            return ""
        except Exception as e:
            log.warning("voice_transcribe_failed", path=media_path, error=str(e)[:100])
            return ""

    @staticmethod
    def _convert_voice_to_wav(media_path: str) -> str | None:
        try:
            from pydub import AudioSegment
        except ImportError:
            warn("db/sync_service.py:1104 ImportError", err=repr(__import__("sys").exc_info()[1]))
            return None

        try:
            src = Path(media_path)
            audio = AudioSegment.from_file(str(src))
            wav_path = src.with_suffix(".wav")
            audio.export(str(wav_path), format="wav")
            return str(wav_path)
        except Exception as e:
            log.warning("silk_to_wav_failed", path=media_path, error=str(e)[:80])
            return None