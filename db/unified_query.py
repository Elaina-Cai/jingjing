from __future__ import annotations

import structlog
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

log = structlog.get_logger()


@dataclass
class AccountedResult:
    data: Any
    platform: str
    account_id: str


class UnifiedQuery:
    """
    统一查询层：跨平台跨账号的数据查询入口。

    - 对外提供统一接口，内部路由到正确的 account.db
    - 所有返回数据附带 platform + account_id 来源标记
    - Agent/Tool 只需通过此层访问账号级数据，不直接接触 AccountDBManager
    """

    def __init__(self, meta_repo, account_db_manager):
        self._meta = meta_repo
        self._adm = account_db_manager

    def search_messages(
        self,
        chat_id: str | None = None,
        sender_id: str | None = None,
        chat_type: str | None = None,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        platform: str | None = None,
        account_id: str | None = None,
        keyword: str | None = None,
    ) -> list[AccountedResult]:
        if platform and account_id:
            repo = self._adm.get_repo(platform, account_id)
            if not repo:
                return []
            msgs = repo.messages.get_recent(
                chat_id=chat_id, chat_type=chat_type, source=source,
                sender_id=sender_id, since=since, until=until, limit=limit,
                keyword=keyword,
            )
            return [AccountedResult(data=m, platform=platform, account_id=account_id) for m in msgs]

        results: list[AccountedResult] = []
        for (plat, aid), repo in self._adm.all_repos().items():
            if platform and plat != platform:
                continue
            msgs = repo.messages.get_recent(
                chat_id=chat_id, chat_type=chat_type, source=source,
                sender_id=sender_id, since=since, until=until, limit=limit,
                keyword=keyword,
            )
            for m in msgs:
                results.append(AccountedResult(data=m, platform=plat, account_id=aid))
        results.sort(key=lambda r: getattr(r.data, "created_at", None) or datetime.min, reverse=True)
        return results[:limit]

    def get_chat_messages(
        self,
        chat_id: str,
        sender_id: str | None = None,
        limit: int = 200,
        platform: str | None = None,
        account_id: str | None = None,
    ) -> list[AccountedResult]:
        if platform and account_id:
            repo = self._adm.get_repo(platform, account_id)
            if not repo:
                return []
            msgs = repo.messages.get_by_chat(chat_id, limit=limit, sender_id=sender_id)
            return [AccountedResult(data=m, platform=platform, account_id=account_id) for m in msgs]

        results: list[AccountedResult] = []
        for (plat, aid), repo in self._adm.all_repos().items():
            if platform and plat != platform:
                continue
            msgs = repo.messages.get_by_chat(chat_id, limit=limit, sender_id=sender_id)
            for m in msgs:
                results.append(AccountedResult(data=m, platform=plat, account_id=aid))
        results.sort(key=lambda r: getattr(r.data, "created_at", None) or datetime.min, reverse=True)
        return results[:limit]

    def get_contacts(
        self,
        platform: str | None = None,
        account_id: str | None = None,
        is_group: int | None = None,
        limit: int = 500,
    ) -> list[AccountedResult]:
        if platform and account_id:
            repo = self._adm.get_repo(platform, account_id)
            if not repo:
                return []
            contacts = repo.contacts.list_all(is_group=is_group, limit=limit)
            return [AccountedResult(data=c, platform=platform, account_id=account_id) for c in contacts]

        results: list[AccountedResult] = []
        for (plat, aid), repo in self._adm.all_repos().items():
            if platform and plat != platform:
                continue
            contacts = repo.contacts.list_all(is_group=is_group, limit=limit)
            for c in contacts:
                results.append(AccountedResult(data=c, platform=plat, account_id=aid))
        return results

    def get_contact_display_name(self, wxid: str, platform: str = "wechat") -> str:
        for (plat, aid), repo in self._adm.all_repos().items():
            if plat != platform:
                continue
            name = repo.contacts.get_display_name(wxid)
            if name != wxid:
                return name
        return wxid

    def search_contacts(
        self,
        query: str = "",
        platform: str | None = None,
        account_id: str | None = None,
        limit: int = 50,
    ) -> list[AccountedResult]:
        if platform and account_id:
            repo = self._adm.get_repo(platform, account_id)
            if not repo:
                return []
            contact = repo.contacts.get_by_wxid(query)
            if contact:
                return [AccountedResult(data=contact, platform=platform, account_id=account_id)]
            results = repo.contacts.search_by_name(query, limit=limit)
            return [AccountedResult(data=c, platform=platform, account_id=account_id) for c in results]

        results: list[AccountedResult] = []
        for (plat, aid), repo in self._adm.all_repos().items():
            if platform and plat != platform:
                continue
            contact = repo.contacts.get_by_wxid(query)
            if contact:
                results.append(AccountedResult(data=contact, platform=plat, account_id=aid))
                continue
            for c in repo.contacts.search_by_name(query, limit=limit):
                results.append(AccountedResult(data=c, platform=plat, account_id=aid))
        return results[:limit]

    def get_chat_list(
        self,
        platform: str | None = None,
        limit: int = 200,
    ) -> list[AccountedResult]:
        results: list[AccountedResult] = []
        for (plat, aid), repo in self._adm.all_repos().items():
            if platform and plat != platform:
                continue
            contacts = repo.contacts.list_all(limit=limit)
            for c in contacts:
                results.append(AccountedResult(data=c, platform=plat, account_id=aid))
        return results

    # -- stickers ------------------------------------------------------------------
    #
    # docs/data_isolation_architecture.md specifies get_emoji(emotion, platform,
    # account_id) at this layer, which is what get_sticker implements; "emoji"
    # was avoided because it already means the decrypted WeChat emoji cache.
    # The routing rules are identical to every other method here, so there is
    # one vocabulary for "whose data is this": name an account for that
    # account, name a platform for all of its accounts, name neither for
    # everything the super terminal can see.

    def list_stickers(
        self,
        platform: str | None = None,
        account_id: str | None = None,
    ) -> list[AccountedResult]:
        """Every sticker on every matching account, tagged with its origin."""
        from db import sticker_store

        return [
            AccountedResult(data=s, platform=s.platform, account_id=s.account_id)
            for s in sticker_store.all_stickers(platform, account_id)
        ]

    def get_sticker(
        self,
        emotion: str,
        platform: str | None = None,
        account_id: str | None = None,
    ) -> AccountedResult | None:
        """Resolve one emotion to a sticker, with provenance."""
        from db import sticker_store

        found = sticker_store.find_sticker(emotion, platform, account_id)
        if found is None:
            return None
        return AccountedResult(data=found, platform=found.platform, account_id=found.account_id)

    # Name used by the design document.
    get_emoji = get_sticker

    def get_message_by_id(
        self,
        msg_id: int,
        platform: str | None = None,
        account_id: str | None = None,
    ) -> AccountedResult | None:
        if platform and account_id:
            repo = self._adm.get_repo(platform, account_id)
            if not repo:
                return None
            msg = repo.messages.get_by_id(msg_id)
            if msg:
                return AccountedResult(data=msg, platform=platform, account_id=account_id)
            return None

        for (plat, aid), repo in self._adm.all_repos().items():
            if platform and plat != platform:
                continue
            msg = repo.messages.get_by_id(msg_id)
            if msg:
                return AccountedResult(data=msg, platform=plat, account_id=aid)
        return None

    def get_contact(
        self,
        wxid: str,
        platform: str | None = None,
        account_id: str | None = None,
    ) -> AccountedResult | None:
        if platform and account_id:
            repo = self._adm.get_repo(platform, account_id)
            if not repo:
                return None
            contact = repo.contacts.get_by_wxid(wxid)
            if contact:
                return AccountedResult(data=contact, platform=platform, account_id=account_id)
            return None

        for (plat, aid), repo in self._adm.all_repos().items():
            if platform and plat != platform:
                continue
            contact = repo.contacts.get_by_wxid(wxid)
            if contact:
                return AccountedResult(data=contact, platform=plat, account_id=aid)
        return None

    def get_reply_chain(
        self,
        msg_id: int,
        max_depth: int = 5,
        platform: str | None = None,
        account_id: str | None = None,
    ) -> list[AccountedResult]:
        if platform and account_id:
            repo = self._adm.get_repo(platform, account_id)
            if not repo:
                return []
            chain = repo.messages.get_reply_chain(msg_id, max_depth=max_depth)
            return [AccountedResult(data=m, platform=platform, account_id=account_id) for m in chain]

        for (plat, aid), repo in self._adm.all_repos().items():
            if platform and plat != platform:
                continue
            chain = repo.messages.get_reply_chain(msg_id, max_depth=max_depth)
            if chain:
                return [AccountedResult(data=m, platform=plat, account_id=aid) for m in chain]
        return []

    def get_contact_display_name_any(self, wxid: str) -> str:
        for (plat, aid), repo in self._adm.all_repos().items():
            name = repo.contacts.get_display_name(wxid)
            if name != wxid:
                return name
        return wxid

    def list_platforms(self) -> list[str]:
        return sorted({plat for (plat, _) in self._adm.all_repos()})

    def get_account_repo(self, platform: str, account_id: str):
        return self._adm.get_repo(platform, account_id)

    @property
    def meta(self):
        return self._meta

    @property
    def account_db_manager(self):
        return self._adm
