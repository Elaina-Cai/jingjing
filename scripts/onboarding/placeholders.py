"""未实现平台的占位适配器：如实标注「未实现 + 下一步去哪申请」。

统一流程先把契约立住；这些平台有 Provider 之后，按 scripts/onboarding/qq.py
的样子补 prepare/authorize/verify/env_values 即可。
"""

from __future__ import annotations

from scripts.onboarding.base import (
    SetupContext,
    Step,
    StepResult,
    register_platform_setup,
)

_PLATFORMS = {
    "telegram": ("Telegram", "找 @BotFather 建 bot 拿 token；Provider 未实现（registry 只有 DTO/身份适配器）"),
    "discord": ("Discord", "Discord Developer Portal 建 Application+Bot 拿 token；Provider 未实现"),
    "bilibili": ("B站", "开放平台申请（直播/私信能力）；Provider 未实现"),
    "douyin": ("抖音", "开放平台申请；Provider 未实现"),
    "dingtalk": ("钉钉", "开放平台建机器人；Provider 未实现"),
    "feishu": ("飞书", "开放平台建应用；Provider 未实现"),
    "web": ("Web", "内建 Web 控制台（/setup、/chat）；无需外部接入"),
}


class PlaceholderSetup:
    def __init__(self, key: str, display: str, hint: str) -> None:
        self.key = key
        self.display = display
        self.hint = hint

    def steps(self) -> list[Step]:
        return [
            Step("prepare", "① 准备接入端", hint=self.hint),
            Step("authorize", "② 登录/授权", manual=True, hint="未实现"),
            Step("verify", "③ 验证能用", hint="未实现"),
            Step("configure", "④ 写配置(.env)", hint="未实现"),
            Step("accept", "⑤ 验收", hint="未实现"),
        ]

    def status(self) -> dict:
        return {
            "configured": False,
            "credentials_ok": False,
            "next_step": f"未实现：{self.hint}",
            "last_error": "",
        }

    def prepare(self, ctx: SetupContext) -> StepResult:
        return StepResult(False, "未实现", next_action=self.hint)

    def authorize(self, ctx: SetupContext) -> StepResult:
        return StepResult(False, "未实现", next_action=self.hint)

    def verify(self, ctx: SetupContext) -> StepResult:
        return StepResult(False, "未实现（Provider 未注册）", next_action=self.hint)

    def env_values(self, ctx: SetupContext) -> dict[str, str]:
        return {}

    def acceptance(self, ctx: SetupContext) -> list[str]:
        return [f"未实现：{self.hint}"]


for _key, (_display, _hint) in _PLATFORMS.items():
    register_platform_setup(PlaceholderSetup(_key, _display, _hint))
