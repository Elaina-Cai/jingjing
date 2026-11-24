from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import select

from db.meta_models import AccountRegistry
from db.repo_base import BaseRepository


class AccountRegistryRepository(BaseRepository):
    def get(self, account_id: str) -> AccountRegistry | None:
        with self._session() as s:
            return s.get(AccountRegistry, account_id)

    def list_all(self, platform: str | None = None, enabled_only: bool = False) -> Sequence[AccountRegistry]:
        with self._session() as s:
            q = select(AccountRegistry).order_by(AccountRegistry.created_at)
            if platform:
                q = q.where(AccountRegistry.platform == platform)
            if enabled_only:
                q = q.where(AccountRegistry.enabled == 1)
            return s.execute(q).scalars().all()

    def list_by_platform(self, platform: str) -> Sequence[AccountRegistry]:
        return self.list_all(platform=platform)

    def upsert(self, account_id: str, platform: str, role: str, data_dir: str,
               enabled: int = 1, config_json: str | None = None) -> AccountRegistry:
        with self._session() as s:
            existing = s.get(AccountRegistry, account_id)
            if existing:
                existing.platform = platform
                existing.role = role
                existing.data_dir = data_dir
                existing.enabled = enabled
                if config_json is not None:
                    existing.config_json = config_json
                s.commit()
                s.refresh(existing)
                return existing
            row = AccountRegistry(
                account_id=account_id,
                platform=platform,
                role=role,
                data_dir=data_dir,
                enabled=enabled,
                config_json=config_json,
                created_at=datetime.now(),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def disable(self, account_id: str) -> None:
        with self._session() as s:
            row = s.get(AccountRegistry, account_id)
            if row:
                row.enabled = 0
                s.commit()

    def enable(self, account_id: str) -> None:
        with self._session() as s:
            row = s.get(AccountRegistry, account_id)
            if row:
                row.enabled = 1
                s.commit()

    def set_enabled(self, account_id: str, enabled: int) -> None:
        with self._session() as s:
            row = s.get(AccountRegistry, account_id)
            if row:
                row.enabled = enabled
                s.commit()

    def remove(self, account_id: str) -> None:
        with self._session() as s:
            row = s.get(AccountRegistry, account_id)
            if row:
                s.delete(row)
                s.commit()
