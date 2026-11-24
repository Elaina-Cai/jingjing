"""LLM-backed observation extraction.

The core module owns the prompt/parse contract but knows nothing about the LLM
SDK.  An infrastructure adapter implements ``TextCompletionPort``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from core.contracts.events import ChatType, MessageEvent, MessageKind
from core.contracts.person_memory import (
    ObservationKind,
    ObservationVisibility,
    PersonObservation,
)
from core.people.ids import stable_observation_id
from core.people.ports import TextCompletionPort
from safelog import warn

_TEXT_KINDS = frozenset({
    MessageKind.TEXT,
    MessageKind.QUOTE_REPLY,
    MessageKind.FORWARD,
    MessageKind.SYSTEM,
})

_SYSTEM_PROMPT = """你是联系人长期记忆抽取器。
只提取“关于当前联系人本人”的、未来可能有用的稳定事实。
不要提取聊天寒暄、群公告、系统提示、别人的隐私八卦。
只输出 JSON 数组，不要 markdown，不要解释。

每项格式:
{
  "kind": "identity|fact|preference|event|plan|relationship|behavior|opinion",
  "content": "一句话事实，最多80字",
  "normalized_key": "可选，同类事实的稳定键，例如 preference.food.spicy",
  "confidence": 0.0-1.0,
  "importance": 0.0-1.0,
  "sensitivity": "normal|sensitive|secret",
  "expires_in_days": 0
}

没有可提取的事实就输出 []。"""


def _scope_visibility(chat_type: ChatType) -> ObservationVisibility:
    if chat_type in (ChatType.GROUP, ChatType.CHANNEL):
        return ObservationVisibility.GROUP_SHARED
    return ObservationVisibility.PRIVATE_ONLY


def _clamp(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        warn("core/people/llm_extractor.py:58 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return default
    return max(0.0, min(1.0, number))


def _parse_items(raw: str) -> list[Mapping[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return []
    if "```" in text:
        blocks = text.split("```")
        for block in blocks:
            cleaned = block.strip()
            if cleaned.startswith("[") or cleaned.startswith("{"):
                text = cleaned
                break
    start_list = text.find("[")
    end_list = text.rfind("]")
    if start_list >= 0 and end_list > start_list:
        text = text[start_list:end_list + 1]
    else:
        start_obj = text.find("{")
        end_obj = text.rfind("}")
        if start_obj >= 0 and end_obj > start_obj:
            text = text[start_obj:end_obj + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Common LLM failure: trailing commas.  Retry after removing them.
        warn("core/people/llm_extractor.py:85 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", text))
        except json.JSONDecodeError:
            warn("core/people/llm_extractor.py:89 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
            return []
    if isinstance(data, dict):
        for key in ("observations", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, Mapping)]


class LlmObservationExtractor:
    """Extract observations through a platform-neutral JSON completion port."""

    def __init__(
        self,
        completion: TextCompletionPort,
        *,
        model_version: str = "llm_v1",
        agent_name: str = "person_memory",
        max_items: int = 5,
    ) -> None:
        self._completion = completion
        self.model_version = model_version
        self._agent_name = agent_name
        self._max_items = max(1, int(max_items or 5))

    async def extract(
        self,
        event: MessageEvent,
        *,
        subject_person_id: str,
        speaker_person_id: str = "",
        evidence_id: str = "",
    ) -> list[PersonObservation]:
        subject_person_id = str(subject_person_id or speaker_person_id or "").strip()
        if not subject_person_id or event.kind not in _TEXT_KINDS:
            return []
        content = str(event.content or "").strip()
        if not content:
            return []

        user_prompt = (
            f"平台: {event.platform}\n"
            f"场景: {'群聊' if event.is_group else '私聊'}\n"
            f"发言人: {event.sender_name or event.sender_id}\n"
            f"消息类型: {event.kind.value}\n"
            f"消息内容:\n{content[:2000]}\n\n"
            "请提取关于当前联系人的观察。"
        )
        raw = await self._completion.complete(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            agent_name=self._agent_name,
        )
        items = _parse_items(raw)

        results: list[PersonObservation] = []
        observed_at = event.created_at or datetime.now()
        for item in items[: self._max_items]:
            content_value = str(item.get("content") or "").strip()
            if not content_value:
                continue
            try:
                kind = ObservationKind(str(item.get("kind") or "").strip().lower())
            except ValueError:
                warn("core/people/llm_extractor.py:155 ValueError", err=repr(__import__("sys").exc_info()[1]))
                kind = ObservationKind.FACT
            sensitivity = str(item.get("sensitivity") or "normal").strip().lower()
            if sensitivity not in ("normal", "sensitive", "secret"):
                sensitivity = "normal"
            expires_at = None
            try:
                days = int(item.get("expires_in_days") or 0)
            except (TypeError, ValueError):
                warn("core/people/llm_extractor.py:163 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
                days = 0
            if days > 0:
                expires_at = observed_at + timedelta(days=days)
            normalized_key = str(item.get("normalized_key") or "").strip()
            if not normalized_key:
                normalized_key = f"{kind.value}.llm_v1"
            results.append(PersonObservation(
                observation_id=stable_observation_id(
                    model_version=self.model_version,
                    platform=event.platform,
                    account_id=event.account_id,
                    scope_type=event.chat.chat_type.value,
                    scope_id=event.chat.chat_id,
                    message_id=event.message_id,
                    kind=kind.value,
                    content=content_value,
                ),
                person_id=subject_person_id,
                platform=event.platform,
                account_id=event.account_id,
                occurred_scope_type=event.chat.chat_type,
                occurred_scope_id=event.chat.chat_id,
                speaker_person_id=speaker_person_id or subject_person_id,
                source_evidence_id=str(evidence_id or ""),
                source_message_id=str(event.message_id or ""),
                observed_at=observed_at,
                kind=kind,
                content=content_value[:500],
                raw_quote=content[:200],
                normalized_key=normalized_key,
                confidence=_clamp(item.get("confidence"), 0.6),
                importance=_clamp(item.get("importance"), 0.5),
                visibility=_scope_visibility(event.chat.chat_type),
                sensitivity=sensitivity,
                expires_at=expires_at,
                model_version=self.model_version,
                evidence_ids=(str(evidence_id),) if evidence_id else (),
            ))
        return results
