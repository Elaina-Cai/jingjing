from __future__ import annotations

import asyncio
import structlog

from core.execution.intent_bus import IntentExecutor, Intent, IntentResult, IntentType

log = structlog.get_logger()


class WechatIntentExecutor(IntentExecutor):
    def __init__(self):
        self._lock = asyncio.Lock()

    async def execute(self, intent: Intent) -> IntentResult:
        async with self._lock:
            log.info("wechat_executor_locked", type=intent.intent_type.value, chat=intent.chat_id)
            if intent.intent_type == IntentType.SEND_TEXT:
                return await self._send_text(intent)
            elif intent.intent_type == IntentType.SEND_STICKER:
                return await self._send_sticker(intent)
            elif intent.intent_type == IntentType.SEND_FILE:
                return await self._send_file(intent)
            elif intent.intent_type == IntentType.SEND_IMAGE:
                return await self._send_file(intent)
            else:
                return IntentResult(ok=False, intent=intent, error=f"不支持的意图类型: {intent.intent_type.value}")

    async def _send_text(self, intent: Intent) -> IntentResult:
        if not (intent.extra or {}).get("policy_prechecked"):
            return IntentResult(ok=False, intent=intent,
                                 error="SEND_TEXT 必须经由 OutboundGateway 发送")
        try:
            from core.execution.provider import WechatProvider
            from core.bootstrap import get_provider_manager
            pm = get_provider_manager()
            provider = None
            if pm:
                if intent.account_id:
                    provider = pm.get_by_account("wechat", intent.account_id)
                if not provider:
                    providers = pm.by_platform(
                        __import__("core.execution.provider", fromlist=["Platform"]).Platform.WECHAT
                    )
                    for p in providers:
                        if p.is_connected:
                            provider = p
                            break

            if not provider:
                from core.config.settings import config
                from core.execution.provider import WechatProvider as WP
                db_path = ""
                if intent.account_id:
                    ac = config.get_account_config("wechat", intent.account_id)
                    if ac:
                        db_path = ac.wx_db_path
                if not db_path:
                    db_path = config.wechat.db_path
                provider = WP(
                    account_id=intent.account_id,
                    db_path=db_path,
                    send_enabled=True,
                )

            result = await provider.send(intent.chat_id, intent.content)
            return IntentResult(ok=result.ok, intent=intent, msg_id=result.msg_id, error=result.error)
        except Exception as e:
            log.error("wechat_intent_send_text_failed", error=str(e))
            return IntentResult(ok=False, intent=intent, error=str(e))

    async def _send_sticker(self, intent: Intent) -> IntentResult:
        try:
            from core.execution.wx_sticker_sender import send_sticker
            from functools import partial
            fn = partial(
                send_sticker,
                who=intent.chat_id,
                mood_id=intent.mood_id,
                emotion=intent.emotion,
                platform="wechat",
                account_id=intent.account_id,
            )
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, fn)
            if result.get("ok"):
                return IntentResult(ok=True, intent=intent)
            return IntentResult(ok=False, intent=intent, error=result.get("error", "表情包发送失败"))
        except Exception as e:
            log.error("wechat_intent_send_sticker_failed", error=str(e), exc_info=True)
            return IntentResult(ok=False, intent=intent, error=str(e))

    async def _send_file(self, intent: Intent) -> IntentResult:
        if not (intent.extra or {}).get("policy_prechecked"):
            return IntentResult(ok=False, intent=intent,
                                 error="SEND_FILE 必须经由 OutboundGateway 发送")
        try:
            from core.execution.provider import WechatProvider
            from core.bootstrap import get_provider_manager
            pm = get_provider_manager()
            provider = None
            if pm:
                if intent.account_id:
                    provider = pm.get_by_account("wechat", intent.account_id)
                if not provider:
                    providers = pm.by_platform(
                        __import__("core.execution.provider", fromlist=["Platform"]).Platform.WECHAT
                    )
                    for p in providers:
                        if p.is_connected:
                            provider = p
                            break

            if not provider:
                from core.config.settings import config
                from core.execution.provider import WechatProvider as WP
                db_path = ""
                if intent.account_id:
                    ac = config.get_account_config("wechat", intent.account_id)
                    if ac:
                        db_path = ac.wx_db_path
                if not db_path:
                    db_path = config.wechat.db_path
                provider = WP(
                    account_id=intent.account_id,
                    db_path=db_path,
                    send_enabled=True,
                )

            result = await provider.send(intent.chat_id, intent.content, filepath=intent.filepath)
            return IntentResult(ok=result.ok, intent=intent, msg_id=result.msg_id, error=result.error)
        except Exception as e:
            log.error("wechat_intent_send_file_failed", error=str(e))
            return IntentResult(ok=False, intent=intent, error=str(e))
