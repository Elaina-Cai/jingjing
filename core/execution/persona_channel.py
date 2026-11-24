# -*- coding: utf-8 -*-
"""⑩ 数据通道：持久人设（persona）只读口（批B Step B3）。

设计图：⑥ 行动模块 ──读人设（persona）──▶ ⑩ 工具包。
⑩ 只做**纯通道**：只读、不写、不调 LLM、不做业务判断；返回纯数据视图。
版本编译 / scope overrides / prompt 组装仍归 `core/speech` 与 ⑨ 各自 owner——
那是有判断的业务，不搬进通道。

* ``read_persona``：persona 行 → :class:`PersonaView`；读不到/异常 → ``None``（不猜）。
* ``persona_note``：规划上下文用的一行摘要（名字/性格/语气；不含 system prompt 正文）。
* ``persona_digest``：审计与回放用的 JSON 载荷（同样不含 system prompt 正文）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from safelog import warn

__all__ = [
    "PersonaView",
    "persona_channel_mode",
    "persona_digest",
    "persona_note",
    "read_persona",
]


def persona_channel_mode() -> str:
    """三态 flag（off/shadow/on，默认 off）；读不到按 off，不阻断。"""
    try:
        from core import features

        return features.mode(features.PERSONA_CHANNEL, features.MODE_OFF)
    except Exception:  # noqa: BLE001 - flag 读不到按 off
        warn("core/execution/persona_channel.py:33 Exception", err=repr(__import__("sys").exc_info()[1]))
        return "off"


@dataclass(frozen=True)
class PersonaView:
    """persistent persona 的纯数据视图（⑩ 通道的返回类型）。"""

    persona_id: int = 0
    name: str = ""
    system_prompt: str = ""
    traits: str = ""
    voice_config: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "persona_id": int(self.persona_id),
            "name": str(self.name),
            "system_prompt": str(self.system_prompt),
            "traits": str(self.traits),
            "voice_config": str(self.voice_config),
        }


def read_persona(
    repo: Any = None, *, persona_id: int | None = None,
) -> PersonaView | None:
    """纯读一条 persona：优先按 id，其次 active，最后 list_all 第一条。

    任何异常/缺失都返回 ``None``——通道不猜、不造默认人设。
    """
    if repo is None:
        try:
            from core.terminal.store_port import get_repo

            repo = get_repo()
        except Exception:  # noqa: BLE001 - 起不来就是读不到
            warn("core/execution/persona_channel.py:69 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None
    try:
        personas = getattr(repo, "personas", None)
    except Exception:  # noqa: BLE001 - 取不到就是读不到
        warn("core/execution/persona_channel.py:73 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    if personas is None:
        return None
    row = None
    try:
        if persona_id is not None:
            row = personas.get_by_id(int(persona_id))
        if row is None:
            active = list(personas.list_active() or [])
            row = active[0] if active else None
        if row is None:
            rows = list(personas.list_all() or [])
            row = rows[0] if rows else None
    except Exception:  # noqa: BLE001 - 读失败 = 读不到
        warn("core/execution/persona_channel.py:87 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    if row is None:
        return None
    try:
        pid = int(getattr(row, "id", 0) or 0)
    except (TypeError, ValueError):
        warn("core/execution/persona_channel.py:93 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        pid = 0
    return PersonaView(
        persona_id=pid,
        name=str(getattr(row, "name", "") or ""),
        system_prompt=str(getattr(row, "system_prompt", "") or ""),
        traits=str(getattr(row, "traits", "") or ""),
        voice_config=str(getattr(row, "voice_config", "") or ""),
    )


def _excerpt(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def persona_note(view: PersonaView | None, *, limit: int = 80) -> str:
    """规划上下文用的一行摘要；没有人设/没名字 → 空串（一个字符不加）。"""
    if view is None or not str(view.name or "").strip():
        return ""
    parts = [f"名字：{view.name}"]
    traits = _excerpt(view.traits, limit)
    voice = _excerpt(view.voice_config, limit)
    if traits:
        parts.append(f"性格：{traits}")
    if voice:
        parts.append(f"语气：{voice}")
    return "[人设] " + "；".join(parts)


def persona_digest(view: PersonaView | None, *, limit: int = 80) -> dict[str, Any]:
    """审计/回放载荷：不含 system prompt 正文，只留可核对的身份摘要。"""
    if view is None:
        return {"found": False}
    return {
        "found": True,
        "persona_id": int(view.persona_id),
        "name": str(view.name),
        "traits_excerpt": _excerpt(view.traits, limit),
        "voice_excerpt": _excerpt(view.voice_config, limit),
        "has_system_prompt": bool(str(view.system_prompt or "").strip()),
    }
