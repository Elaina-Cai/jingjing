from __future__ import annotations

import hashlib


def global_message_id(platform: str, account_id: str, chat_id: str,
                      platform_msg_id: object) -> int:
    """Deterministic 63-bit identity for one platform message.

    ``(platform, account_id, chat_id, platform_msg_id)`` is the natural
    identity of an inbound message.  Hashing it gives agents/meta tables a
    stable key that cannot collide across platforms or accounts, unlike the
    platform's own ``msg_id`` or a per-account auto-increment sequence.
    """
    raw = "\x1f".join((
        str(getattr(platform, "value", platform) or ""),
        str(getattr(account_id, "value", account_id) or ""),
        str(getattr(chat_id, "value", chat_id) or ""),
        str(platform_msg_id if platform_msg_id is not None else ""),
    )).encode("utf-8", errors="replace")
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    value = int.from_bytes(digest, "big") & ((1 << 63) - 1)
    return value or 1


__all__ = ["global_message_id"]
