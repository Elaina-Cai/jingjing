"""Reconcile outbound send logs back into conversation windows (P3).

The platform message id is only known after the WeChat sync claims the sent
message and the send log records ``confirmed_msg_id``.  This small reconciler
closes the loop:

    reply sent -> send_log_id stored on window
      -> later sync confirms platform msg id
        -> window.last_bot_message_id is filled

It only touches windows that are explicitly waiting for a bot message id.
"""

from __future__ import annotations

import logging
from typing import Any

from core.conversation.store import WindowStore

log = logging.getLogger(__name__)


def reconcile_bot_message_ids(
    store: WindowStore,
    send_log_repo: Any,
    *,
    platform: str = "",
    account_id: str = "",
    limit: int = 200,
) -> int:
    if send_log_repo is None:
        return 0
    updated = 0
    pending = store.list_pending_bot_replies(
        platform=platform, account_id=account_id, limit=limit,
    )
    for key, window in pending:
        raw_id = str(window.last_send_log_id or "").strip()
        if not raw_id.isdigit():
            continue
        try:
            send_log = send_log_repo.get(int(raw_id))
        except Exception as exc:
            log.debug("reconcile_send_log_lookup_failed: %s", exc)
            continue
        if send_log is None:
            continue
        confirmed = getattr(send_log, "confirmed_msg_id", None)
        if not confirmed:
            continue
        window.last_bot_message_id = str(confirmed)
        window.last_send_log_id = ""
        if not window.last_bot_asked_question:
            content = str(getattr(send_log, "content", "") or "")
            window.last_bot_asked_question = ("?" in content) or ("？" in content)
        try:
            store.save(key, window)
            updated += 1
        except Exception as exc:
            log.debug("reconcile_window_save_failed: %s", exc)
    if updated:
        log.info(
            "conversation_bot_ids_reconciled platform=%s account_id=%s count=%d",
            platform, account_id, updated,
        )
    return updated
