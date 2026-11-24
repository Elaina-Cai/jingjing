"""平台接入统一流程：五步契约与适配器协议。

五步固定为：准备接入端 → 登录/授权 → 验证能用 → 写配置(.env) → 验收。
平台差异全部收进适配器；流程与话术由 ``scripts/platform_setup.py`` 统一驱动。
详见 ``docs/平台接入统一流程.md``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

STEP_KEYS = ("prepare", "authorize", "verify", "configure", "accept")
STEP_TITLES = {
    "prepare": "① 准备接入端",
    "authorize": "② 登录/授权",
    "verify": "③ 验证能用",
    "configure": "④ 写配置(.env)",
    "accept": "⑤ 验收",
}


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    manual: bool = False
    hint: str = ""


@dataclass
class StepResult:
    ok: bool
    detail: str = ""
    next_action: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class SetupContext:
    root: Path
    dry_run: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    def log(self, text: str) -> None:
        print(text, flush=True)


@runtime_checkable
class PlatformSetup(Protocol):
    key: str
    display: str

    def status(self) -> dict: ...
    def steps(self) -> list[Step]: ...
    def prepare(self, ctx: SetupContext) -> StepResult: ...
    def authorize(self, ctx: SetupContext) -> StepResult: ...
    def verify(self, ctx: SetupContext) -> StepResult: ...
    def env_values(self, ctx: SetupContext) -> dict[str, str]: ...
    def acceptance(self, ctx: SetupContext) -> list[str]: ...


_REGISTRY: dict[str, PlatformSetup] = {}


def register_platform_setup(adapter: PlatformSetup) -> None:
    key = str(getattr(adapter, "key", "") or "").strip().lower()
    if not key:
        raise ValueError("adapter.key 不能为空")
    _REGISTRY[key] = adapter


def get_platform_setup(key: str) -> PlatformSetup | None:
    return _REGISTRY.get(str(key or "").strip().lower())


def all_setups() -> dict[str, PlatformSetup]:
    return dict(_REGISTRY)
