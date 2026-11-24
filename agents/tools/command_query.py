"""Natural-language command ledger query tool (Phase 2).

This is the model-facing formal entry for "群里某人下过什么命令 / 谁查过什么".
Visibility is enforced by ``CommandService``:

* owner sees all tenants;
* an ordinary contact sees only their own commands;
* a group member sees commands in their membership period only;
* the per-group query switch is evaluated by ``IdentityService``.

The tool returns natural language; raw CommandRecord JSON never reaches the
model or the user.
"""

from __future__ import annotations

from agents.tools.base import BaseTool, ToolResult
from safelog import warn


class QueryCommandsTool(BaseTool):
    name = "query_commands"
    description = (
        "查询命令/提问/请求台账。群成员查询本群命令时用 group_id；"
        "普通联系人只返回自己下达过的命令；主人可查全局。"
        "结果已经是自然语言，直接用来回答用户，不要暴露 JSON 或内部 id。"
    )
    parameters = {
        "group_id": {
            "type": "string",
            "description": "要查询的群 id；群聊内通常留空，工具会用当前会话",
        },
        "include_revoked": {
            "type": "boolean",
            "description": "是否包含已撤销命令，默认 true",
        },
    }

    async def execute(self, **kwargs) -> ToolResult:
        from core.terminal.authority import (
            AuthorityService,
            active_context_principal,
            context_principal,
        )
        from core.terminal.command_runtime import get_terminal_command_service

        service = get_terminal_command_service()
        if service is None:
            return ToolResult("", error="命令台账不可用（缺少迁移 34 或 meta DB）")

        principal = context_principal(self.ctx)
        if principal is None:
            try:
                from core.terminal.event_runtime import get_event_identity_service

                identity_service = get_event_identity_service()
                if identity_service is not None:
                    principal = active_context_principal(
                        self.ctx,
                        service=AuthorityService(identity_service),
                    )
            except Exception:
                warn("agents/tools/command_query.py:61 Exception", err=repr(__import__("sys").exc_info()[1]))
                principal = None
        if principal is None:
            return ToolResult("", error="无法解析当前身份，命令查询被拒绝")

        group_id = str(kwargs.get("group_id") or principal.group_id or "")
        include_revoked = bool(kwargs.get("include_revoked", True))
        try:
            answer = service.render_query(
                principal,
                group_id=group_id,
                include_revoked=include_revoked,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool error
            warn("agents/tools/command_query.py:74 Exception", err=repr(exc))
            return ToolResult("", error=f"命令查询被拒绝: {str(exc)[:160]}")
        return ToolResult(answer or "没有可查询的命令")
