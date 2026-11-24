from __future__ import annotations

import re
import structlog

from agents.tools.base import BaseTool, ToolResult

log = structlog.get_logger()


def _strip_display_tags(text: str) -> str:
    text = re.sub(r"\[emo:\S+?\]\s*", "", text)
    text = re.sub(r"\[sticker:[^\]]+\]", "", text)
    return text.strip()


class SendMessageTool(BaseTool):
    name = "send_message"
    description = "发送消息或文件给指定联系人或群聊。需要对应平台客户端已连接。"
    parameters = {
        "who": {
            "type": "string",
            "description": "接收者名称，可以是联系人昵称、备注名或群聊名称",
            "required": True,
        },
        "content": {
            "type": "string",
            "description": "要发送的消息内容",
        },
        "filepath": {
            "type": "string",
            "description": "要发送的文件路径，与content二选一",
        },
        "platform": {
            "type": "string",
            "description": "发送平台: wechat(默认)/qq/telegram，不填则默认wechat",
        },
        "account": {
            "type": "string",
            "description": "使用哪个账号发送: self(鲸鲸自己的号，默认)/owner(主人的号)，仅在主人明确要求用主人号时使用owner",
        },
    }

    def __init__(self, repo, provider_manager=None, unified_query=None):
        self.repo = repo
        self._provider_manager = provider_manager
        self._uq = unified_query

    async def execute(self, **kwargs) -> ToolResult:
        who = kwargs.get("who", "").strip()
        content = kwargs.get("content", "").strip()
        filepath = kwargs.get("filepath", "").strip()
        platform = kwargs.get("platform", "wechat").strip()
        account_choice = kwargs.get("account", "self").strip()

        if not who:
            return ToolResult("", error="缺少接收者名称")
        if not content and not filepath:
            return ToolResult("", error="缺少消息内容或文件路径")

        if content:
            content = _strip_display_tags(content)

        snapshot = getattr(self.ctx, "speech_snapshot", None) if self.ctx else None
        if snapshot is None and self.ctx is not None:
            try:
                from core.speech.turn import snapshot_from_context
                snapshot = snapshot_from_context(
                    self.ctx,
                    platform=platform,
                    chat_id=who,
                    chat_type="group" if (getattr(self.ctx, "extra", {}) or {}).get("is_group") else "private",
                    inbound_text=content or filepath,
                    msg_type="text",
                )
            except Exception as exc:
                log.warning("tool_snapshot_resolve_failed", error=str(exc))

        if snapshot is None:
            return ToolResult("", error="当前会话没有可用的说话策略快照，发送已拒绝")

        try:
            from core.bootstrap import get_outbound_gateway
            from core.speech.domain import OutboundDraft
            gateway = get_outbound_gateway()
            if hasattr(self.ctx, "take_policy_decision"):
                prechecked = self.ctx.take_policy_decision()
            else:
                prechecked = (getattr(self.ctx, "extra", {}) or {}).pop(
                    "_policy_decision", None,
                )
            draft = OutboundDraft(
                request_id=getattr(getattr(self.ctx, "turn_context", None), "request_id", ""),
                kind="file" if filepath else "text",
                target=snapshot.turn.target,
                account_choice=account_choice,
                content=content,
                filepath=filepath,
                who=who,
                extra={"prechecked_decision": prechecked} if prechecked is not None else {},
            )
            if filepath:
                result = await gateway.send_file(draft, snapshot, self.ctx)
            else:
                from core.speech import rhythm

                result = await rhythm.deliver(
                    gateway, draft, snapshot, self.ctx, who=who,
                )
                if result is None:
                    result = await gateway.send_text(draft, snapshot, self.ctx)
            return ToolResult(result.output, error=result.error)
        except Exception as exc:
            log.error("gateway_send_exception", error=str(exc))
            return ToolResult("", error=f"发送失败: {exc}")

