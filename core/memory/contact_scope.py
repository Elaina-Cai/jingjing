"""可信账号内的联系人解析；显示名不提供身份或读取权限。"""
from __future__ import annotations


def chat_allowed(key, principal) -> bool:
    if not key or principal is None or len(key) != 3 or not all(key):
        return False
    tenant = (str(getattr(principal, "platform", "") or ""),
              str(getattr(principal, "bot_account_id", "") or ""))
    chat = str(getattr(principal, "chat_id", "") or "")
    kind = getattr(principal, "chat_type", "")
    group = bool(getattr(principal, "is_group", False)) or str(getattr(kind, "value", kind)) in {"group", "channel"}
    return bool(all(tenant) and chat and tuple(key[:2]) == tenant
                and (key[2] == chat or (bool(getattr(principal, "is_owner", False)) and not group)))


class ScopedContacts:
    def __init__(self, unified_query, principal):
        self.query = unified_query
        self.platform = str(getattr(principal, "platform", "") or "")
        self.account = str(getattr(principal, "bot_account_id", "") or "")

    def _valid(self, result):
        return (str(getattr(result, "platform", "") or ""),
                str(getattr(result, "account_id", "") or "")) == (self.platform, self.account)

    def get(self, raw_id):
        if self.query is None or not all((self.platform, self.account, raw_id)):
            return None
        result = self.query.get_contact(raw_id, platform=self.platform, account_id=self.account)
        data = getattr(result, "data", None)
        return data if self._valid(result) and str(getattr(data, "wxid", "")) == raw_id else None

    def resolve(self, name, *, group_only=False):
        if not name or self.query is None or not all((self.platform, self.account)):
            return None
        exact = self.get(name)
        if exact is not None and (not group_only or exact.is_group):
            return exact
        rows = self.query.search_contacts(query=name, platform=self.platform, account_id=self.account)
        matches = {str(r.data.wxid): r.data for r in rows if self._valid(r)
                   and getattr(r, "data", None) is not None and getattr(r.data, "wxid", "")
                   and (not group_only or r.data.is_group)}
        if len(matches) > 1:
            raise ValueError("contact_name_ambiguous: 名称对应多个联系人，请提供明确 ID")
        return next(iter(matches.values()), None)

    def display(self, raw_id):
        contact = self.get(raw_id)
        return (contact.remark or contact.nickname or raw_id) if contact is not None else raw_id
