from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config.settings import config
from safelog import warn

log = structlog.get_logger()


@dataclass
class ValidationIssue:
    level: str
    category: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


_RESULTS: list[ValidationIssue] = []


def _issue(level: str, category: str, message: str, **detail):
    _RESULTS.append(ValidationIssue(level=level, category=category, message=message, detail=detail))


def _check_llm():
    provider = config.llm.provider
    key_map = {
        "deepseek": config.llm.deepseek_api_key,
        "openai": config.llm.openai_api_key,
        "dashscope": config.llm.dashscope_api_key,
        "siliconflow": config.llm.siliconflow_api_key,
    }

    key = key_map.get(provider)
    if key is None:
        _issue("warn", "llm", f"LLM provider '{provider}' 未注册 api_key 映射", provider=provider)
    elif not key:
        _issue("error", "llm", f"当前 LLM provider '{provider}' 的 api_key 为空", provider=provider)

    # 2026-11-22：本机代理（CC Switch 15721）的 api_key 内联在 configs/llm.toml，
    # 不走 settings 的 env 映射；按旧映射查会每启动一次报一条无意义的 warn。
    local_endpoints = {"qwen_proxy"}
    for name in config.llm.fallback_chain:
        fb_key = key_map.get(name)
        if name == "ollama" or name in local_endpoints:
            continue
        if fb_key is None:
            _issue("warn", "llm", f"fallback chain 中 '{name}' 未注册 api_key 映射", provider=name)
        elif not fb_key:
            _issue("warn", "llm", f"fallback chain 中 '{name}' 的 api_key 为空", provider=name)

    if config.llm.timeout_seconds < 5:
        _issue("warn", "llm", "timeout_seconds 过小，可能导致频繁超时", timeout=config.llm.timeout_seconds)

    if config.llm.daily_token_budget < 0:
        _issue('error', 'llm', 'daily_token_budget 不能为负数', budget=config.llm.daily_token_budget)
    elif config.llm.daily_token_budget == 0:
        _issue('warn', 'llm', 'daily_token_budget=0，按 unlimited 处理', budget=0)
    if config.llm.per_agent_budget < 0:
        _issue('error', 'llm', 'per_agent_budget 不能为负数', budget=config.llm.per_agent_budget)
    elif config.llm.per_agent_budget == 0:
        _issue('warn', 'llm', 'per_agent_budget=0，按 unlimited 处理', budget=0)


def _check_embedding():
    provider = config.embedding.provider
    key_map = {
        "siliconflow": config.llm.siliconflow_api_key,
        "openai": config.llm.openai_api_key,
        "deepseek": config.llm.deepseek_api_key,
    }
    key = key_map.get(provider)
    if key is not None and not key:
        _issue("warn", "embedding", f"embedding provider '{provider}' 的 api_key 为空", provider=provider)

    if config.embedding.dim <= 0:
        _issue("error", "embedding", "embedding dim 必须为正数", dim=config.embedding.dim)


def _check_wechat():
    has_any_db_path = False
    for ac in config.all_account_configs():
        if ac.platform == "wechat" and ac.wx_db_path:
            has_any_db_path = True
            break
    if not has_any_db_path and not config.wechat.db_path:
        _issue("warn", "wechat", "没有任何微信账号配置 db_path，同步功能不可用")
    if not config.wechat.key:
        _issue("warn", "wechat", "wechat.key 为空，消息解密不可用")


def _check_paths():
    for name, path in [
        ("data_dir", config.paths.data_dir),
        ("decrypted_dir", config.paths.decrypted_dir),
        ("output_dir", config.paths.output_dir),
        ("log_dir", config.paths.log_dir),
    ]:
        if path and not Path(path).exists():
            _issue("info", "paths", f"{name} 不存在，将在启动时自动创建", path=str(path))


def _check_persona():
    if config.persona.enabled and config.persona.default_persona_id <= 0:
        _issue("error", "persona", "persona 已启用但 default_persona_id 无效", id=config.persona.default_persona_id)




def _check_temporal():
    timezone_name = (getattr(config.temporal, "timezone", "") or "").strip()
    if not timezone_name:
        return
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone_name)
    except Exception as exc:
        warn("core/config/validate.py:121 Exception", err=repr(exc))
        _issue(
            "warn", "temporal",
            f"TEMPORAL_TIMEZONE '{timezone_name}' 无法解析，将退回系统本地时区",
            error=str(exc),
        )


def validate_config() -> list[ValidationIssue]:
    _RESULTS.clear()
    _check_llm()
    _check_embedding()
    _check_wechat()
    _check_paths()
    _check_persona()
    _check_temporal()
    return list(_RESULTS)


def validate_and_report() -> bool:
    issues = validate_config()
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warn"]

    for i in issues:
        if i.level == "error":
            log.error("config_validation", category=i.category, message=i.message, **i.detail)
        elif i.level == "warn":
            log.warning("config_validation", category=i.category, message=i.message, **i.detail)
        else:
            log.info("config_validation", category=i.category, message=i.message, **i.detail)

    if errors:
        log.error("config_validation_failed", errors=len(errors), warnings=len(warnings))
        return False
    if warnings:
        log.warning("config_validation_passed_with_warnings", warnings=len(warnings))
    return True
