"""Stable observation identifiers.

The same message + model + kind + normalized content must produce the same id
on every replay, so workers can be restarted and idempotent upserts are safe.
"""

from __future__ import annotations

import hashlib


def stable_observation_id(
    *,
    model_version: str,
    platform: str,
    account_id: str,
    scope_type: str,
    scope_id: str,
    message_id: str,
    kind: str,
    content: str,
) -> str:
    raw = "\x1f".join([
        str(model_version or ""),
        str(platform or ""),
        str(account_id or ""),
        str(scope_type or ""),
        str(scope_id or ""),
        str(message_id or ""),
        str(kind or ""),
        str(content or "").strip()[:500],
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
