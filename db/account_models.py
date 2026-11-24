from __future__ import annotations

"""Account-database compatibility layer.

Historically this module defined a second copy of every model.  The schema has
been merged into :mod:`db.models`; account databases now share the same
metadata and only restrict which tables ``create_all`` materialises.
"""

from db.models import (
    Analysis,
    Base,
    Contact,
    Message,
    SchemaVersion,
    SendLog,
    ActionLog,
    SyncState,
    UserProfile,
    init_db as _init_db,
)

# Legacy name kept for callers that type-hinted AccountBase.
AccountBase = Base
AccountSchemaVersion = SchemaVersion

ACCOUNT_TABLE_NAMES = (
    "contacts",
    "messages",
    "analysis",
    "sync_state",
    "user_profile",
    "send_log",
    "action_log",
    "event_outbox",
)


def init_account_db(db_path: str):
    """Create/return an account-scoped engine with only account tables."""
    tables = [
        Base.metadata.tables[name]
        for name in ACCOUNT_TABLE_NAMES
        if name in Base.metadata.tables
    ]
    return _init_db(db_path, tables=tables)


__all__ = [
    "AccountBase",
    "AccountSchemaVersion",
    "ACCOUNT_TABLE_NAMES",
    "Contact",
    "Message",
    "SyncState",
    "Analysis",
    "UserProfile",
    "SendLog",
    "ActionLog",
    "init_account_db",
]
