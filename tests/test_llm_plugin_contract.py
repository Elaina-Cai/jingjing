"""WP6 plugin entry points + adapter contract suite.

Run directly:
    python tests/test_llm_plugin_contract.py
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "fake_provider_pkg"
BAD_FIXTURE = ROOT / "tests" / "fixtures" / "fake_provider_pkg_bad"
_TMP = Path(os.environ.get("YINYUE_TEST_TMP", "data/_test_llm_plugin"))
_TMP.mkdir(parents=True, exist_ok=True)


@contextmanager
def _fixture_path(path: Path):
    sys.path.insert(0, str(path))
    try:
        yield
    finally:
        try:
            sys.path.remove(str(path))
        except ValueError:
            pass


def _write_toml(name: str, text: str) -> str:
    path = _TMP / name
    path.write_text(text, encoding="utf-8")
    return str(path)
from llm.adapters import (  # noqa: E402
    ADAPTER_CONTRACT_VERSION,
    AdapterContractError,
    FakeTransport,
    assert_adapter_contract,
    get_adapter_class,
    known_kinds,
    load_plugins,
    register_adapter,
    rejected_plugins,
    reset_plugins,
    run_adapter_contract,
)
from llm.adapters.base import Capabilities  # noqa: E402
from llm.adapters.openai_compat import OpenAICompatAdapter  # noqa: E402
from llm.config import EndpointSpec, LLMConfigError, load_llm_config  # noqa: E402


def _spec() -> EndpointSpec:
    return EndpointSpec(
        name="contract",
        kind="openai_compat",
        base_url="https://contract.test/v1",
        model="contract-model",
        api_key="k",
        capabilities=Capabilities(
            tools=True, vision=True, json_mode=True, streaming=True,
            stream_usage=True, context_window=100000,
        ),
    )


def test_fake_provider_package_loads_by_entry_point() -> None:
    with _fixture_path(FIXTURE):
        reset_plugins()
        loaded = load_plugins(force=True)
    kinds = [item["kind"] for item in loaded]
    assert "fake_provider" in kinds, loaded
    adapter_cls = get_adapter_class("fake_provider")
    assert adapter_cls is not None
    assert adapter_cls.__name__ == "FakeProviderAdapter"
    assert "fake_provider" in known_kinds()
    assert {"openai_compat", "anthropic"} <= known_kinds()


def test_contract_version_mismatch_is_rejected() -> None:
    with _fixture_path(BAD_FIXTURE):
        reset_plugins()
        load_plugins(force=True)
        assert get_adapter_class("bad_provider") is None
        rejected = [
            item for item in rejected_plugins() if item["entry_point"] == "bad_provider"
        ]
        assert rejected, rejected_plugins()
        assert "不匹配" in rejected[0]["error"], rejected


def test_register_adapter_rejects_wrong_version() -> None:
    class _WrongVersion:
        KIND = "wrong_version"
        ADAPTER_CONTRACT_VERSION = 99

    try:
        register_adapter("wrong_version", _WrongVersion)
    except AdapterContractError as exc:
        assert "99" in str(exc) and "拒绝加载" in str(exc)
    else:
        raise AssertionError("wrong contract version must be rejected")


def test_plugin_kind_passes_config_validation() -> None:
    toml = """version = 1

[routing]
default = "plug"

[endpoints.plug]
kind = "fake_provider"
base_url = "https://plugin.test/v1"
model = "plugin-model"
"""
    path = _write_toml("plugin_config.toml", toml)
    from core.config.settings import LLMSettings

    with _fixture_path(FIXTURE):
        reset_plugins()
        cfg = load_llm_config(path=path, settings=LLMSettings(_env_file=None), env={})
    assert cfg.endpoints["plug"].kind == "fake_provider"
    assert cfg.default_endpoint == "plug"


def test_unknown_kind_is_rejected() -> None:
    toml = """version = 1

[routing]
default = "ghost"

[endpoints.ghost]
kind = "ghost_kind"
base_url = "https://ghost.test/v1"
model = "ghost-model"
"""
    path = _write_toml("ghost_config.toml", toml)
    from core.config.settings import LLMSettings

    with _fixture_path(FIXTURE):
        reset_plugins()
        try:
            load_llm_config(path=path, settings=LLMSettings(_env_file=None), env={})
        except LLMConfigError as exc:
            assert "ghost_kind" in str(exc), exc
        else:
            raise AssertionError("unknown kind must be rejected")


def test_run_adapter_contract_openai_compat() -> None:
    transport = FakeTransport()
    adapter = OpenAICompatAdapter(_spec(), client_factory=lambda: transport)
    report = run_adapter_contract(adapter, transport, model="contract-model")
    failed = [item for item in report["checks"] if not item["ok"]]
    assert report["ok"], failed
    assert report["version"] == ADAPTER_CONTRACT_VERSION
    names = {item["name"] for item in report["checks"]}
    for expected in (
        "capabilities", "chat_text", "usage_reported", "timeout_passed",
        "tools_preserve_raw_arguments", "stream_text", "stream_usage_reported",
        "error_mapping", "cancellation_propagates",
    ):
        assert expected in names, sorted(names)


def test_contract_detects_broken_adapter() -> None:
    class _Broken:
        name = "broken"

        def capabilities(self, model):
            raise RuntimeError("no capabilities")

    transport = FakeTransport()
    report = run_adapter_contract(_Broken(), transport, model="m")
    assert report["ok"] is False
    try:
        assert_adapter_contract(_Broken(), transport, model="m")
    except AdapterContractError as exc:
        assert "broken" in str(exc) and "capabilities" in str(exc), exc
    else:
        raise AssertionError("broken adapter must fail the contract")


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
