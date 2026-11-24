"""WP-STATE 状态装配 v1 的存储：``self_moves`` 台账 + ``chat_state`` 每会话一行。

**降级契约（硬约束）**：读失败（表缺失 / 结构坏 / 磁盘错）一律返回 ``None`` /
``[]``，**绝不抛**；调用方拿到空值就退回"没有状态"的现状行为，不得因此拒绝服务。
写失败照旧抛——写不进去是配置/磁盘问题，需要被看见。

**只记她自己的行为**：本仓库不存用户消息正文、不存昵称、不存记忆内容；只按时间戳
记"她发过什么类型的回复、用户后来有没有回"。

时间语义：一律 **本地 naive 墙钟**（与 ``session_focus`` / ``work_items`` 同口径），
经唯一 owner ``core.temporal.to_naive_local`` 折算。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from core import temporal
from db.meta_models import ChatStateRow, SelfMoveRow
from db.repo_base import BaseRepository
from safelog import warn

log = structlog.get_logger()

__all__ = ["SelfMove", "ChatState", "StateRepository"]


def _now() -> datetime:
    return datetime.now()


def _parse_keys(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if str(x)]
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        warn("db/repo_state.py:45 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return []
    return [str(x) for x in parsed if str(x)] if isinstance(parsed, list) else []


def _dump_keys(keys: Iterable[str]) -> str:
    return json.dumps([str(k) for k in keys if str(k)], ensure_ascii=False)


@dataclass
class SelfMove:
    """一行 ``self_moves`` 的快照（不是 ORM 行）。"""

    id: int = 0
    platform: str = ""
    account_id: str = ""
    chat_id: str = ""
    sent_at: datetime | None = None
    move_kind: str = "other"
    bit_keys: list[str] = field(default_factory=list)
    content_hash: str = ""
    chars: int = 0
    expects_reply: int = 0
    engagement: str = "pending"      # pending / replied
    engaged_at: datetime | None = None


@dataclass
class ChatState:
    """一行 ``chat_state`` 的快照。"""

    platform: str = ""
    account_id: str = ""
    chat_id: str = ""
    last_user_at: datetime | None = None
    last_bot_at: datetime | None = None
    last_bot_move_kind: str = ""
    last_bot_bit_keys: list[str] = field(default_factory=list)
    last_bot_content_hash: str = ""
    last_bot_expects_reply: int = 0
    last_bot_engagement: str = "none"  # none / pending / replied
    last_bot_engaged_at: datetime | None = None
    updated_at: datetime | None = None
    revision: int = 1
    #: 「翻篇边界」：晚于它的动作才进"最近用过/冷却"（None = 没有边界）。
    reset_at: datetime | None = None


class StateRepository(BaseRepository):
    """``self_moves`` / ``chat_state`` 的读写入口（构造见 ``MetaRepositoryManager.state``）。"""

    # ------------------------------------------------------------------ 读
    def get_chat_state(self, *, platform: str, account_id: str, chat_id: str) -> ChatState | None:
        """读一行会话状态；**读不到返回 None，绝不抛**（降级契约）。"""
        try:
            with self._session() as session:
                row = session.get(
                    ChatStateRow,
                    (platform or "", account_id or "", chat_id or ""),
                )
                return None if row is None else self._state_snapshot(row)
        except SQLAlchemyError as exc:
            log.debug(
                "chat_state_unavailable",
                chat_id=chat_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None

    def recent_moves(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        since: datetime,
    ) -> list[SelfMove]:
        """读 ``since`` 之后她发出的回复；**失败返回 []，绝不抛**。"""
        try:
            with self._session() as session:
                rows = session.execute(
                    select(SelfMoveRow)
                    .where(
                        SelfMoveRow.platform == (platform or ""),
                        SelfMoveRow.account_id == (account_id or ""),
                        SelfMoveRow.chat_id == (chat_id or ""),
                        SelfMoveRow.sent_at >= since,
                    )
                    .order_by(SelfMoveRow.sent_at.desc(), SelfMoveRow.id.desc())
                ).scalars().all()
                return [self._move_snapshot(row) for row in rows]
        except SQLAlchemyError as exc:
            log.debug(
                "self_moves_unavailable",
                chat_id=chat_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return []

    # ------------------------------------------------------------------ 写
    def record_self_move(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        reply: str,
        sent_at: datetime,
        move_kind: str = "other",
        bit_keys: Sequence[str] = (),
        expects_reply: int = 0,
    ) -> None:
        """记录一次成功发送（追加台账 + 更新会话状态的"最后一次回复"）。

        去重：``(platform, account_id, chat_id, content_hash, sent_at_sec)`` 唯一，
        重复记录静默忽略（``INSERT OR IGNORE`` 语义）。
        """
        sent_at = temporal.to_naive_local(sent_at) or _now()
        sent_sec = sent_at.replace(microsecond=0)
        content_hash = hashlib.sha1((reply or "").encode("utf-8")).hexdigest()[:12]
        keys = [str(k) for k in bit_keys if str(k)]
        values = dict(
            platform=platform or "",
            account_id=account_id or "",
            chat_id=chat_id or "",
            sent_at=sent_at,
            sent_at_sec=sent_sec,
            move_kind=str(move_kind or "other"),
            bit_keys_json=_dump_keys(keys),
            content_hash=content_hash,
            chars=len(reply or ""),
            expects_reply=int(expects_reply or 0),
            engagement="pending",
            engaged_at=None,
            created_at=_now(),
        )
        key = (platform or "", account_id or "", chat_id or "")
        with self._session() as session:
            session.execute(
                sqlite_insert(SelfMoveRow)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        "platform", "account_id", "chat_id", "content_hash", "sent_at_sec",
                    ]
                )
            )
            row = session.get(ChatStateRow, key)
            if row is None:
                session.add(ChatStateRow(
                    platform=key[0],
                    account_id=key[1],
                    chat_id=key[2],
                    last_bot_at=sent_at,
                    last_bot_move_kind=values["move_kind"],
                    last_bot_bit_keys_json=values["bit_keys_json"],
                    last_bot_content_hash=content_hash,
                    last_bot_expects_reply=values["expects_reply"],
                    last_bot_engagement="pending",
                    last_bot_engaged_at=None,
                    updated_at=sent_at,
                    revision=1,
                ))
            else:
                row.last_bot_at = sent_at
                row.last_bot_move_kind = values["move_kind"]
                row.last_bot_bit_keys_json = values["bit_keys_json"]
                row.last_bot_content_hash = content_hash
                row.last_bot_expects_reply = values["expects_reply"]
                row.last_bot_engagement = "pending"
                row.last_bot_engaged_at = None
                row.updated_at = sent_at
                row.revision = int(row.revision or 0) + 1
            session.commit()

    def mark_reset(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        at: datetime | None = None,
    ) -> None:
        """落一条「翻篇边界」：晚于它的动作不再进"最近用过/冷却"。

        同时清掉 ``last_bot_*``（上一条记录也被翻篇），保留 ``last_user_at``
        （"本轮"时间锚点还要用）。幂等：重复调用只更新边界与 revision。
        """
        at = temporal.to_naive_local(at) or _now()
        key = (platform or "", account_id or "", chat_id or "")
        with self._session() as session:
            row = session.get(ChatStateRow, key)
            if row is None:
                session.add(ChatStateRow(
                    platform=key[0], account_id=key[1], chat_id=key[2],
                    reset_at=at, updated_at=at, revision=1,
                ))
            else:
                row.reset_at = at
                row.last_bot_at = None
                row.last_bot_move_kind = ""
                row.last_bot_bit_keys_json = "[]"
                row.last_bot_content_hash = ""
                row.last_bot_expects_reply = 0
                row.last_bot_engagement = "none"
                row.last_bot_engaged_at = None
                row.updated_at = at
                row.revision = int(row.revision or 0) + 1
            session.commit()

    def observe_user_turn(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        at: datetime,
    ) -> None:
        """记录一次用户消息；若它晚于她最后一次回复 → 把那条回复标成 ``replied``。

        幂等：``last_user_at >= at`` 时直接返回（同一轮被多次调用不会重复更新）。
        """
        at = temporal.to_naive_local(at) or _now()
        key = (platform or "", account_id or "", chat_id or "")
        with self._session() as session:
            row = session.get(ChatStateRow, key)
            if row is None:
                session.add(ChatStateRow(
                    platform=key[0],
                    account_id=key[1],
                    chat_id=key[2],
                    last_user_at=at,
                    updated_at=at,
                    revision=1,
                ))
                session.commit()
                return
            if row.last_user_at is not None and row.last_user_at >= at:
                return
            row.last_user_at = at
            row.updated_at = at
            if (
                row.last_bot_at is not None
                and (row.last_bot_engagement or "none") == "pending"
                and row.last_bot_at < at
            ):
                row.last_bot_engagement = "replied"
                row.last_bot_engaged_at = at
                target = session.execute(
                    select(SelfMoveRow)
                    .where(
                        SelfMoveRow.platform == key[0],
                        SelfMoveRow.account_id == key[1],
                        SelfMoveRow.chat_id == key[2],
                        SelfMoveRow.content_hash == (row.last_bot_content_hash or ""),
                        SelfMoveRow.engagement == "pending",
                    )
                    .order_by(SelfMoveRow.sent_at.desc(), SelfMoveRow.id.desc())
                    .limit(1)
                ).scalars().first()
                if target is not None:
                    target.engagement = "replied"
                    target.engaged_at = at
            session.commit()

    # ------------------------------------------------------------------ 快照
    @staticmethod
    def _move_snapshot(row: SelfMoveRow) -> SelfMove:
        return SelfMove(
            id=int(row.id or 0),
            platform=row.platform or "",
            account_id=row.account_id or "",
            chat_id=row.chat_id or "",
            sent_at=row.sent_at,
            move_kind=row.move_kind or "other",
            bit_keys=_parse_keys(row.bit_keys_json),
            content_hash=row.content_hash or "",
            chars=int(row.chars or 0),
            expects_reply=int(row.expects_reply or 0),
            engagement=row.engagement or "pending",
            engaged_at=row.engaged_at,
        )

    @staticmethod
    def _state_snapshot(row: ChatStateRow) -> ChatState:
        return ChatState(
            platform=row.platform or "",
            account_id=row.account_id or "",
            chat_id=row.chat_id or "",
            last_user_at=row.last_user_at,
            last_bot_at=row.last_bot_at,
            last_bot_move_kind=row.last_bot_move_kind or "",
            last_bot_bit_keys=_parse_keys(row.last_bot_bit_keys_json),
            last_bot_content_hash=row.last_bot_content_hash or "",
            last_bot_expects_reply=int(row.last_bot_expects_reply or 0),
            last_bot_engagement=row.last_bot_engagement or "none",
            last_bot_engaged_at=row.last_bot_engaged_at,
            updated_at=row.updated_at,
            revision=int(row.revision or 0),
            reset_at=row.reset_at,
        )
