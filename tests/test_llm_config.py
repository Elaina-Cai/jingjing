"""WP1 config-layer guards: TOML + legacy env equivalence + priority.

Run directly:
    python tests/test_llm_config.py
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(os.environ.get("YINYUE_TEST_TMP", "data/_test_llm_config"))
_TMP.mkdir(parents=True, exist_ok=True)

_LEGACY_KEYS = (
    "PROVIDER", "FALLBACK_CHAIN", "TIMEOUT_SECONDS", "MAX_RETRIES",
    "DAILY_TOKEN_BUDGET", "PER_AGENT_BUDGET",
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "OLLAMA_BASE_URL", "OLLAMA_MODEL",
    "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "DASHSCOPE_VISION_MODEL",
    "SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL", "LLM_CONFIG_PATH",
)


@contextmanager
def _clean_env():
    saved = {key: os.environ.pop(key, None) for key in _LEGACY_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def _settings(**kwargs):
    from core.config.settings import LLMSettings

    return LLMSettings(_env_file=None, **kwargs)


def _absent(name: str = "absent.toml") -> str:
    path = _TMP / name
    if path.exists():
        path.unlink()
    return str(path)


def _write_toml(name: str, text: str) -> str:
    path = _TMP / name
    path.write_text(text, encoding="utf-8")
    return str(path)


class _Captured:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def _emit(self, level: str, event, **kw) -> None:
        self.events.append({"level": level, "event": event, **kw})

    def info(self, event=None, **kw):
        self._emit("info", event, **kw)

    def warning(self, event=None, **kw):
        self._emit("warning", event, **kw)

    def error(self, event=None, **kw):
        self._emit("error", event, **kw)

    def debug(self, event=None, **kw):
        self._emit("debug", event, **kw)


_TOML_BASE = """version = 1

[routing]
default = "strong"
fallback = ["strong"]

[endpoints.strong]
kind = "openai_compat"
base_url = "https://api.siliconflow.cn/v1"
api_key_env = "SILICONFLOW_API_KEY"
model = "Qwen3-235B-A22B-Instruct"
"""

def test_legacy_env_maps_to_endpoints() -> None:
    from llm.config import load_llm_config

    with _clean_env():
        st = _settings(
            provider="openai",
            fallback_chain=["deepseek", "openai"],
            deepseek_api_key="dk",
            deepseek_base_url="https://ds.example/v1",
            deepseek_model="ds-x",
            timeout_seconds=12,
            max_retries=4,
            daily_token_budget=1234,
            per_agent_budget=99,
        )
        cfg = load_llm_config(path=_absent(), settings=st, env={})

    assert set(cfg.endpoints) >= {"deepseek", "openai", "ollama"}
    assert cfg.default_endpoint == "deepseek"
    assert cfg.legacy_provider == "openai"
    assert cfg.fallback == ("deepseek", "openai")
    deepseek = cfg.endpoints["deepseek"]
    assert deepseek.api_key_env == "DEEPSEEK_API_KEY"
    assert deepseek.base_url == "https://ds.example/v1"
    assert deepseek.model == "ds-x"
    assert deepseek.timeout == 12.0
    assert cfg.max_retries == 4
    assert cfg.budgets["daily_tokens"] == 1234
    assert cfg.budgets["per_agent"]["*"] == 99
    assert cfg.on_missing_capability == "fail"


def test_legacy_stream_usage_and_ollama_mapping() -> None:
    from llm.config import load_llm_config

    with _clean_env():
        st_vl = _settings(ollama_model="qwen2.5-vl:7b", ollama_base_url="http://127.0.0.1:11434")
        cfg_vl = load_llm_config(path=_absent("vl.toml"), settings=st_vl, env={})
        st_text = _settings(ollama_model="qwen2.5:7b")
        cfg_text = load_llm_config(path=_absent("text.toml"), settings=st_text, env={})

    ollama = cfg_vl.endpoints["ollama"]
    assert ollama.api_key == "ollama"
    assert ollama.api_key_env == ""
    assert ollama.base_url == "http://127.0.0.1:11434/v1"
    assert ollama.capabilities.vision is True
    assert cfg_text.endpoints["ollama"].capabilities.vision is False
    for name in ("deepseek", "openai", "ollama"):
        caps = cfg_vl.endpoints[name].capabilities
        assert caps.stream_usage is True, name
        assert caps.tools and caps.json_mode and caps.streaming, name


def test_legacy_on_missing_capability_is_fail() -> None:
    from llm.config import load_llm_config

    with _clean_env():
        cfg = load_llm_config(path=_absent("fail.toml"), settings=_settings(), env={})
    assert cfg.on_missing_capability == "fail"


def test_fast_chat_agent_route() -> None:
    from llm.config import AgentRoute, load_llm_config

    with _clean_env():
        cfg = load_llm_config(path=_absent("fast.toml"), settings=_settings(), env={})
    expected = AgentRoute(endpoint="ollama", only_kind="chat", on_circuit_open="fallback")
    assert cfg.agent_endpoints["fast_chat"] == expected
    assert cfg.agent_endpoints["fast_tool_format"] == expected


def test_legacy_dashscope_mapping() -> None:
    from llm.config import load_llm_config

    with _clean_env():
        st = _settings(dashscope_api_key="ds-key", dashscope_vision_model="qwen-vl-max")
        cfg = load_llm_config(path=_absent("dash.toml"), settings=st, env={})
    assert "dashscope" in cfg.endpoints
    assert cfg.endpoints["dashscope"].model == "qwen-vl-max"
    assert cfg.kind_lead.get("vision") == ("dashscope",)


def test_env_overrides_toml() -> None:
    from llm.config import load_llm_config

    path = _write_toml("env_override.toml", _TOML_BASE)
    with _clean_env():
        cfg = load_llm_config(
            path=path,
            settings=_settings(),
            env={
                "YINYUE_LLM_DEFAULT_ENDPOINT": "ollama",
                "YINYUE_LLM_ROUTE_CONVERSATIONAL": "strong",
            },
        )
    assert cfg.default_endpoint == "ollama"
    assert cfg.agent_endpoints["conversational"].endpoint == "strong"


def test_priority_yinyue_env_beats_legacy() -> None:
    from llm.config import load_llm_config

    path = _write_toml("priority.toml", _TOML_BASE)
    with _clean_env():
        st = _settings(fallback_chain=["deepseek", "openai", "ollama"])
        cfg = load_llm_config(
            path=path,
            settings=st,
            env={"YINYUE_LLM_DEFAULT_ENDPOINT": "strong"},
        )
    assert cfg.default_endpoint == "strong"
    assert cfg.fallback == ("deepseek", "openai", "ollama")


def test_legacy_provider_does_not_reroute() -> None:
    from llm.config import load_llm_config

    with _clean_env():
        cfg = load_llm_config(
            path=_absent("provider.toml"),
            settings=_settings(provider="openai"),
            env={},
        )
    assert cfg.legacy_provider == "openai"
    assert cfg.default_endpoint == "deepseek"


def test_missing_endpoint_raises_human_message() -> None:
    from llm.config import LLMConfigError, load_llm_config

    path = _write_toml("missing_endpoint.toml", (
        "version = 1\n\n"
        "[routing]\n"
        'default = "ghost"\n\n'
        "[endpoints.deepseek]\n"
        'kind = "openai_compat"\n'
        'base_url = "https://x/v1"\n'
        'model = "m"\n'
    ))
    with _clean_env():
        try:
            load_llm_config(path=path, settings=_settings(), env={})
        except LLMConfigError as exc:
            message = str(exc)
        else:
            raise AssertionError("should raise LLMConfigError")
    assert "ghost" in message and "deepseek" in message, message


def test_conflict_prefers_legacy_env_with_deprecation() -> None:
    import llm.config as config_mod

    path = _write_toml("conflict.toml", (
        "version = 1\n\n"
        "[routing]\n"
        'default = "deepseek"\n'
        'fallback = ["deepseek"]\n\n'
        "[endpoints.deepseek]\n"
        'kind = "openai_compat"\n'
        'base_url = "https://toml.example/v1"\n'
        'model = "toml-model"\n'
    ))
    cap = _Captured()
    old_log = config_mod.log
    config_mod.log = cap
    try:
        with _clean_env():
            st = _settings(deepseek_model="legacy-model")
            cfg = config_mod.load_llm_config(path=path, settings=st, env={})
    finally:
        config_mod.log = old_log
    assert cfg.endpoints["deepseek"].model == "legacy-model"
    events = [e["event"] for e in cap.events if e.get("event")]
    assert "llm_legacy_env_deprecated" in events, events


def test_example_toml_parses() -> None:
    from llm.config import load_llm_config

    path = ROOT / "configs" / "llm.example.toml"
    assert path.is_file(), path
    with _clean_env():
        cfg = load_llm_config(path=str(path), settings=_settings(), env={})
    assert "strong" in cfg.endpoints
    assert cfg.endpoints["strong"].model == "Qwen3-235B-A22B-Instruct"
    assert cfg.agent_endpoints["conversational"].endpoint == "strong"
    assert cfg.default_endpoint == "deepseek"
    assert cfg.budgets["per_agent"]["conversational"] == 200000
    assert cfg.kind_lead.get("vision") == ("dashscope",)


def test_doctor_report_smoke() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import doctor_llm

    from llm.config import load_llm_config

    with _clean_env():
        st = _settings(deepseek_api_key="sk-abcdefgh")
        cfg = load_llm_config(path=_absent("doctor.toml"), settings=st, env={})
        report = doctor_llm.build_report(cfg, settings=st)
    assert "LLM 路由表" in report
    assert "deepseek" in report
    assert "****efgh" in report
    assert "fallback" in report



def test_doctor_shows_endpoint_prices() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import doctor_llm

    from llm.adapters.base import Capabilities
    from llm.config import EndpointSpec, LLMConfig

    endpoint = EndpointSpec(
        name="priced",
        kind="openai_compat",
        base_url="https://x/v1",
        model="m",
        api_key="k",
        capabilities=Capabilities(tools=True),
        pricing={
            "currency": "CNY",
            "input_per_mtok": 2.0,
            "output_per_mtok": 8.0,
            "cache_discount": 0.25,
        },
    )
    cfg = LLMConfig(
        endpoints={"priced": endpoint},
        default_endpoint="priced",
        agent_endpoints={},
        fallback=(),
        budgets={"daily_tokens": 1000},
    )
    report = doctor_llm.build_report(cfg, settings=_settings())
    assert "单价:" in report
    assert "input CNY2.0/Mtok" in report and "output CNY8.0/Mtok" in report
    assert "cache_hit CNY0.5/Mtok" in report
    assert "已用 0" in report


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
