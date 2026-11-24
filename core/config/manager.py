from __future__ import annotations

import structlog
from pathlib import Path
from typing import Any, Callable
from safelog import warn

log = structlog.get_logger()


class ConfigManager:
    def __init__(self):
        self._watcher_callbacks: list[Callable] = []
        self._last_mtime: float = 0.0

    def get_schema(self) -> dict:
        try:
            from core.config.settings import AppConfig
            from pydantic import BaseModel
            schema = {}
            for field_name, field_info in AppConfig.model_fields.items():
                field_type = field_info.annotation
                if hasattr(field_type, "model_fields"):
                    section = {}
                    for sub_name, sub_info in field_type.model_fields.items():
                        sub_type = sub_info.annotation
                        type_str = "string"
                        if sub_type in (int, "int"):
                            type_str = "int"
                        elif sub_type in (float, "float"):
                            type_str = "float"
                        elif sub_type in (bool, "bool"):
                            type_str = "bool"
                        elif sub_type in (list, "list"):
                            type_str = "list"

                        default = sub_info.default if sub_info.default is not None else ""
                        is_secret = "key" in sub_name.lower() or "secret" in sub_name.lower()

                        section[sub_name] = {
                            "type": type_str,
                            "default": str(default) if not is_secret else "********",
                            "desc": sub_info.description or "",
                            "secret": is_secret,
                        }
                    schema[field_name] = section
            return schema
        except Exception as e:
            log.warning("config_schema_failed", error=str(e))
            return {}

    def get_values(self) -> dict:
        try:
            from core.config.settings import config
            from pydantic import BaseModel
            result = {}
            for field_name in config.model_fields:
                section_obj = getattr(config, field_name, None)
                if section_obj is None:
                    continue
                if hasattr(section_obj, "model_fields"):
                    result[field_name] = {}
                    for sub_name in section_obj.model_fields:
                        val = getattr(section_obj, sub_name, None)
                        is_secret = "key" in sub_name.lower() or "secret" in sub_name.lower()
                        if is_secret and val:
                            result[field_name][sub_name] = "********"
                        else:
                            result[field_name][sub_name] = val
            return result
        except Exception as e:
            log.warning("config_values_failed", error=str(e))
            return {}

    def update(self, section: str, field_name: str, value: Any) -> bool:
        try:
            env_key = f"{section.upper()}_{field_name.upper()}"
            env_path = Path(".env")
            lines = []
            if env_path.exists():
                lines = env_path.read_text(encoding="utf-8").splitlines()

            found = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith(f"{env_key}=") or stripped.startswith(f"{env_key} ="):
                    lines[i] = f"{env_key}={value}"
                    found = True
                    break
            if not found:
                lines.append(f"{env_key}={value}")

            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log.info("config_updated", key=env_key)
            return True
        except Exception as e:
            log.error("config_update_failed", error=str(e))
            return False

    def get_status(self) -> dict:
        try:
            from core.config.settings import config
            return {
                "loaded": True,
                "sections": list(config.model_fields.keys()),
                "env_file": str(Path(".env").absolute()),
                "env_exists": Path(".env").exists(),
            }
        except Exception as e:
            warn("core/config/manager.py:108 Exception", err=repr(e))
            return {"loaded": False, "error": str(e)}

    async def check_reload(self) -> bool:
        try:
            env_path = Path(".env")
            if not env_path.exists():
                return False
            mtime = env_path.stat().st_mtime
            if mtime != self._last_mtime:
                self._last_mtime = mtime
                for cb in self._watcher_callbacks:
                    try:
                        cb()
                    except Exception as _:
                        warn("core/config/manager.py:122 Exception", err=repr(_))
                log.info("config_file_changed")
                return True
        except Exception as _:
            warn("core/config/manager.py:126 Exception", err=repr(_))
        return False

    def on_reload(self, callback: Callable):
        self._watcher_callbacks.append(callback)


def get_config_manager() -> ConfigManager:
    from core.bootstrap import _singletons
    if "config_manager" not in _singletons:
        _singletons["config_manager"] = ConfigManager()
    return _singletons["config_manager"]
