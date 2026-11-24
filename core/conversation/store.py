"""Conversation-window storage ports.

``core.conversation`` never depends on SQLAlchemy.  Callers inject either the
in-memory store (tests, single-process shadow) or the meta-backed store
(production).  Both expose the same tiny surface.
"""

from __future__ import annotations

from dataclasses import replace
from threading import RLock
from typing import Protocol

from core.conversation.domain import ConversationWindow, WindowState
from safelog import warn

WindowKey = tuple[str, str, str, str]


def make_key(platform: str, account_id: str, chat_id: str,
             person_id: str) -> WindowKey:
    return (platform, account_id, chat_id, person_id)


class WindowStore(Protocol):
    def get(self, key: WindowKey) -> ConversationWindow | None: ...

    def save(self, key: WindowKey, window: ConversationWindow) -> None: ...

    def delete(self, key: WindowKey) -> None: ...

    def clear(self) -> None: ...

    def list_open(self, *, platform: str = "", account_id: str = "",
                  limit: int = 200) -> list[tuple[WindowKey, ConversationWindow]]: ...

    def list_pending_bot_replies(
        self, *, platform: str = "", account_id: str = "", limit: int = 200,
    ) -> list[tuple[WindowKey, "ConversationWindow"]]: ...



class InMemoryWindowStore:
    def __init__(self):
        self._lock = RLock()
        self._windows: dict[WindowKey, ConversationWindow] = {}

    def get(self, key: WindowKey) -> ConversationWindow | None:
        with self._lock:
            window = self._windows.get(key)
            return replace(window) if window is not None else None

    def save(self, key: WindowKey, window: ConversationWindow) -> None:
        with self._lock:
            self._windows[key] = replace(window)

    def delete(self, key: WindowKey) -> None:
        with self._lock:
            self._windows.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._windows.clear()

    def list_open(self, *, platform: str = "", account_id: str = "",
                  limit: int = 200) -> list[tuple[WindowKey, ConversationWindow]]:
        with self._lock:
            out = []
            for key, window in self._windows.items():
                if platform and key[0] != platform:
                    continue
                if account_id and key[1] != account_id:
                    continue
                if window.state.value not in ("open", "waiting"):
                    continue
                out.append((key, replace(window)))
                if len(out) >= limit:
                    break
            return out

    def list_pending_bot_replies(
        self, *, platform: str = "", account_id: str = "", limit: int = 200,
    ) -> list[tuple[WindowKey, ConversationWindow]]:
        with self._lock:
            out = []
            for key, window in self._windows.items():
                if platform and key[0] != platform:
                    continue
                if account_id and key[1] != account_id:
                    continue
                if not window.last_send_log_id or window.last_bot_message_id:
                    continue
                out.append((key, replace(window)))
                if len(out) >= limit:
                    break
            return out



class MetaWindowStore:
    """WindowStore backed by ``db.repo_conversation_windows``."""

    def __init__(self, repo):
        self._repo = repo

    @staticmethod
    def _to_window(row) -> ConversationWindow:
        try:
            state = WindowState(row.state)
        except ValueError:
            warn("core/conversation/store.py:109 ValueError", err=repr(__import__("sys").exc_info()[1]))
            state = WindowState.IDLE
        return ConversationWindow(
            platform=row.platform or "",
            account_id=row.account_id or "",
            chat_id=row.chat_id or "",
            person_id=row.person_id or "",
            state=state,
            topic=row.topic or "",
            last_bot_message_id=row.last_bot_message_id or "",
            last_send_log_id=row.last_send_log_id or "",
            last_bot_asked_question=bool(row.last_bot_asked_question),
            last_person_message_id=row.last_person_message_id or "",
            opened_at=row.opened_at,
            last_activity_at=row.last_activity_at,
            turn_count=int(row.turn_count or 0),
            bot_turn_count=int(row.bot_turn_count or 0),
            confidence=float(row.confidence or 0.0),
        )

    @staticmethod
    def _from_window(window: ConversationWindow) -> dict:
        return {
            "platform": window.platform,
            "account_id": window.account_id,
            "chat_id": window.chat_id,
            "person_id": window.person_id,
            "state": window.state.value,
            "topic": window.topic,
            "last_bot_message_id": window.last_bot_message_id,
            "last_send_log_id": window.last_send_log_id,
            "last_bot_asked_question": window.last_bot_asked_question,
            "last_person_message_id": window.last_person_message_id,
            "opened_at": window.opened_at,
            "last_activity_at": window.last_activity_at,
            "turn_count": window.turn_count,
            "bot_turn_count": window.bot_turn_count,
            "confidence": window.confidence,
        }

    def get(self, key: WindowKey) -> ConversationWindow | None:
        row = self._repo.get(
            platform=key[0], account_id=key[1], chat_id=key[2], person_id=key[3],
        )
        return self._to_window(row) if row is not None else None

    def save(self, key: WindowKey, window: ConversationWindow) -> None:
        self._repo.upsert(**self._from_window(window))

    def delete(self, key: WindowKey) -> None:
        self._repo.delete(
            platform=key[0], account_id=key[1], chat_id=key[2], person_id=key[3],
        )

    def clear(self) -> None:
        self._repo.clear()

    def list_open(self, *, platform: str = "", account_id: str = "",
                  limit: int = 200) -> list[tuple[WindowKey, ConversationWindow]]:
        rows = self._repo.list_open(
            platform=platform, account_id=account_id, limit=limit,
        )
        out = []
        for row in rows:
            key = make_key(row.platform, row.account_id, row.chat_id, row.person_id)
            out.append((key, self._to_window(row)))
        return out

    def list_pending_bot_replies(
        self, *, platform: str = "", account_id: str = "", limit: int = 200,
    ) -> list[tuple[WindowKey, ConversationWindow]]:
        rows = self._repo.list_pending_bot_replies(
            platform=platform, account_id=account_id, limit=limit,
        )
        out = []
        for row in rows:
            key = make_key(row.platform, row.account_id, row.chat_id, row.person_id)
            out.append((key, self._to_window(row)))
        return out

