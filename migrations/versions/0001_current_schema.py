"""Baseline for the schema produced by the legacy migrations 1-22.

Existing databases are stamped with this revision.  New migrations should use
``alembic revision -m "..."`` and set ``down_revision = "0001_current_schema"``.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_current_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The baseline is intentionally empty: the pre-Alembic schema already
    # contains every table/column introduced up to migration 22.
    pass


def downgrade() -> None:
    pass
