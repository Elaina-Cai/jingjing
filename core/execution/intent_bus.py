from __future__ import annotations

import asyncio
import structlog
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

log = structlog.get_logger()


class IntentType(Enum):
    SEND_TEXT = "send_text"
    SEND_STICKER = "send_sticker"
    SEND_FILE = "send_file"
    SEND_IMAGE = "send_image"
    SEND_VOICE = "send_voice"


@dataclass
class Intent:
    intent_type: IntentType
    platform: str
    account_id: str = ""
    chat_id: str = ""
    content: str = ""
    mood_id: str = ""
    emotion: str = ""
    filepath: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.intent_type.value}:{self.platform}:{self.account_id}:{self.chat_id}"


@dataclass
class IntentResult:
    ok: bool
    intent: Intent
    msg_id: str = ""
    error: str = ""


class IntentExecutor:
    async def execute(self, intent: Intent) -> IntentResult:
        raise NotImplementedError


class IntentBus:
    def __init__(self):
        self._executors: dict[str, IntentExecutor] = {}
        self._hooks: list[Callable[[Intent], Awaitable[None]]] = []

    def register_executor(self, platform: str, executor: IntentExecutor):
        self._executors[platform] = executor
        log.info("intent_executor_registered", platform=platform)

    def add_hook(self, hook: Callable[[Intent], Awaitable[None]]):
        self._hooks.append(hook)

    async def dispatch(self, intent: Intent) -> IntentResult:
        log.info(
            "intent_dispatch",
            type=intent.intent_type.value,
            platform=intent.platform,
            account=intent.account_id,
            chat=intent.chat_id,
        )

        for hook in self._hooks:
            try:
                await hook(intent)
            except Exception as e:
                log.warning("intent_hook_failed", error=str(e))

        executor = self._executors.get(intent.platform)
        if not executor:
            log.error("intent_no_executor", platform=intent.platform)
            return IntentResult(ok=False, intent=intent, error=f"无 {intent.platform} 的执行器")

        try:
            result = await executor.execute(intent)
            log.info(
                "intent_executed",
                ok=result.ok,
                type=intent.intent_type.value,
                platform=intent.platform,
            )
            return result
        except Exception as e:
            log.error("intent_execute_failed", error=str(e))
            return IntentResult(ok=False, intent=intent, error=str(e))

    async def dispatch_batch(self, intents: list[Intent]) -> list[IntentResult]:
        results = []
        for intent in intents:
            result = await self.dispatch(intent)
            results.append(result)
            if intent.intent_type == IntentType.SEND_STICKER:
                await asyncio.sleep(0.5)
        return results


_intent_bus: IntentBus | None = None


def get_intent_bus() -> IntentBus:
    global _intent_bus
    if _intent_bus is None:
        _intent_bus = IntentBus()
    return _intent_bus
