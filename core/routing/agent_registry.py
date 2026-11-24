from __future__ import annotations

import structlog
from typing import Type, Any

from core.infra.event_bus import EventType, EventBus
from agents.base import BaseAgent

log = structlog.get_logger()


class AgentRegistry:
    _agents: dict[str, Type[BaseAgent]] = {}
    _event_subscriptions: dict[str, list[EventType]] = {}
    _descriptions: dict[str, str] = {}
    _instances: dict[str, BaseAgent] = {}
    _sub_ids: dict[str, str] = {}

    @classmethod
    def register(
        cls,
        name: str,
        agent_cls: Type[BaseAgent],
        events: list[EventType] | None = None,
        description: str = "",
    ):
        cls._agents[name] = agent_cls
        cls._event_subscriptions[name] = events or []
        cls._descriptions[name] = description or getattr(agent_cls, "description", "")

    @classmethod
    def get(cls, name: str) -> Type[BaseAgent] | None:
        return cls._agents.get(name)

    @classmethod
    def all_agents(cls) -> dict[str, Type[BaseAgent]]:
        return dict(cls._agents)

    @classmethod
    def events_for(cls, name: str) -> list[EventType]:
        return cls._event_subscriptions.get(name, [])

    @classmethod
    def enabled_agents(cls, enabled_names: list[str]) -> dict[str, Type[BaseAgent]]:
        return {k: v for k, v in cls._agents.items() if k in enabled_names}

    @classmethod
    def get_instance(cls, name: str, db_path: str = "", **deps) -> BaseAgent | None:
        if name in cls._instances:
            return cls._instances[name]

        agent_cls = cls._agents.get(name)
        if not agent_cls:
            return None

        instance = agent_cls(db_path=db_path, **deps)
        cls._instances[name] = instance
        return instance

    @classmethod
    def wire_events(cls, name: str, db_path: str = "", **deps):
        events = cls._event_subscriptions.get(name, [])
        if not events:
            return

        instance = cls.get_instance(name, db_path, **deps)
        if not instance:
            return

        handler = getattr(instance, "_make_handler", None)
        if not handler:
            log.debug("agent_no_handler", agent=name)
            return

        for event_type in events:
            sub_id = EventBus.subscribe(
                event_type,
                handler(),
                priority=instance.priority,
            )
            cls._sub_ids[f"{name}:{event_type.value}"] = sub_id

        log.info("agent_wired", agent=name, events=[e.value for e in events])

    @classmethod
    def unwire_events(cls, name: str):
        events = cls._event_subscriptions.get(name, [])
        for event_type in events:
            key = f"{name}:{event_type.value}"
            sub_id = cls._sub_ids.pop(key, None)
            if sub_id:
                EventBus.unsubscribe(sub_id)

    @classmethod
    def wire_all(cls, db_path: str = "", enabled_names: list[str] | None = None, **deps):
        names = enabled_names or list(cls._agents.keys())
        for name in names:
            if name in cls._agents:
                cls.wire_events(name, db_path, **deps)

    @classmethod
    def clear_instances(cls):
        for name in list(cls._instances.keys()):
            cls.unwire_events(name)
        cls._instances.clear()
        cls._sub_ids.clear()

    @classmethod
    def info(cls) -> list[dict]:
        return [
            {
                "name": name,
                "class": agent_cls.__name__,
                "priority": getattr(agent_cls, "priority", 3),
                "description": cls._descriptions.get(name, ""),
                "events": [e.value for e in cls._event_subscriptions.get(name, [])],
                "instantiated": name in cls._instances,
            }
            for name, agent_cls in cls._agents.items()
        ]
