"""平台接入统一流程（五步契约）：注册表 + 内置适配器。

导入本包即完成内置适配器注册（qq / wechat / 7 个占位）。
"""

from __future__ import annotations

from scripts.onboarding import placeholders, qq, wechat  # noqa: F401  注册副作用
from scripts.onboarding.base import (  # noqa: F401
    STEP_KEYS,
    STEP_TITLES,
    PlatformSetup,
    SetupContext,
    Step,
    StepResult,
    all_setups,
    get_platform_setup,
    register_platform_setup,
)

__all__ = [
    "STEP_KEYS",
    "STEP_TITLES",
    "PlatformSetup",
    "SetupContext",
    "Step",
    "StepResult",
    "all_setups",
    "get_platform_setup",
    "register_platform_setup",
]
