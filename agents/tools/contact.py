from __future__ import annotations

from core import temporal
from agents.tools.base import BaseTool, ToolResult
from safelog import warn


class GetContactInfoTool(BaseTool):
    name = "get_contact_info"
    description = "查询联系人信息：备注名、昵称、是否群聊、最近消息摘要。可传wxid或昵称/备注名。仅查询当前可信账号内有权读取的会话"
    parameters = {
        "wxid": {"type": "string", "description": "联系人的wxid或昵称或备注名", "required": True},
        "platform": {"type": "string", "description": "必须与当前回合平台一致"},
        "account_id": {"type": "string", "description": "必须与当前回合账号一致"},
    }

    def __init__(self, repo, unified_query=None):
        self.repo = repo
        self._uq = unified_query

    async def execute(self, **kwargs) -> ToolResult:
        from core.memory.contact_scope import ScopedContacts, chat_allowed
        principal = self.principal
        contacts = ScopedContacts(self._uq, principal)
        if principal is None or not all((contacts.platform, contacts.account, principal.chat_id)):
            return ToolResult("", error="contact_scope_missing: 当前读取身份不完整")
        if any(kwargs.get(k) and str(kwargs[k]) != v for k, v in
               (("platform", contacts.platform), ("account_id", contacts.account))):
            return ToolResult("", error="contact_scope_denied: 参数不能更换读取账号")
        try:
            contact = contacts.resolve(str(kwargs.get("wxid") or ""))
        except ValueError as exc:
            return ToolResult("", error=str(exc))
        if contact is None:
            return ToolResult("", error="找不到联系人")
        wxid = str(contact.wxid)
        key = (contacts.platform, contacts.account, wxid)
        if not chat_allowed(key, principal):
            return ToolResult("", error="contact_scope_denied: 当前回合不能读取该会话")
        display = contact.remark or contact.nickname or wxid
        recent_lines = []
        results = self._uq.get_chat_messages(chat_id=wxid, limit=3,
                    platform=contacts.platform, account_id=contacts.account)
        for result in results:
            message = result.data
            if not contacts._valid(result) or str(getattr(message, "chat_id", "")) != wxid:
                continue
            content = message.content or ""
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            recent_lines.append("  [%s] %s" % (temporal.format_stamp(message.created_at), str(content)[:50]))
        recent = "\n".join(recent_lines) or "  无最近消息"
        kind = "群聊" if contact.is_group else "私聊"
        return ToolResult(f"联系人: {display} ({wxid})\n类型: {kind}\n最近消息:\n{recent}", data={
            "wxid": wxid, "display_name": display, "is_group": contact.is_group,
            "relation_type": None, "intimacy": None,
        })
