"""微信适配器：把现有 安装向导 / start.py / 体检 的能力装进统一五步。

微信是"本地 DB"路线：客户端不代装、密钥要管理员提，因此 prepare/authorize 大多是
检测 + 指引（诚实标注人工步骤），verify 复用 doctor 的检查项。
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.onboarding.base import (
    SetupContext,
    Step,
    StepResult,
    register_platform_setup,
)
from scripts.onboarding.common import read_env

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))  # 让 doctor / start.py 相关模块可直接 import


class WechatSetup:
    key = "wechat"
    display = "微信"

    def steps(self) -> list[Step]:
        return [
            Step("prepare", "① 准备接入端",
                 hint="装微信 PC 4.x 并登录鲸鲸号（客户端不代装；文档：docs/使用手册.md §1）"),
            Step("authorize", "② 登录/授权", manual=True,
                 hint="管理员跑 python start.py：关闭微信 → DbkeyHook → 重新登录 → 提 WECHAT_KEY"),
            Step("verify", "③ 验证能用", hint="doctor：.env / 微信配置 / 密钥 / 微信进程"),
            Step("configure", "④ 写配置(.env)",
                 hint="WECHAT_* + WECHAT_OWNER_WXIDS（由 start.py / 安装鲸鲸.bat 写入）"),
            Step("accept", "⑤ 验收", hint="体检 12 项 + 发一条私聊看回复"),
        ]

    def status(self) -> dict:
        env = read_env(ROOT / ".env")
        wxid = env.get("WECHAT_YINYUE_WXID", "")
        key = env.get("WECHAT_KEY", "")
        owner = env.get("WECHAT_OWNER_WXIDS", "")
        if not wxid:
            nxt = "跑 `安装鲸鲸.bat`（首次配置）完成账号检测"
        elif not key:
            nxt = "缺 WECHAT_KEY：管理员跑 python start.py 提密钥"
        elif not owner:
            nxt = "缺主人号（WECHAT_OWNER_WXIDS）：python scripts\\get_wechat_info.py --set-env"
        else:
            nxt = "已配置；重启鲸鲸后看日志 scheduler_running"
        return {
            "configured": bool(wxid),
            "credentials_ok": bool(wxid and key),
            "next_step": nxt,
            "last_error": "",
        }

    def prepare(self, ctx: SetupContext) -> StepResult:
        import doctor

        check = doctor.check_wechat_running()
        ok = getattr(check, "level", "") == doctor.OK
        detail = str(getattr(check, "title", "") or "")
        fix = str(getattr(check, "fix", "") or "")
        return StepResult(ok, detail, next_action="" if ok else (fix or "先打开微信并登录鲸鲸号"))

    def authorize(self, ctx: SetupContext) -> StepResult:
        """默认自动提权拉起 start.py（关微信→hook→微信窗口弹出登录→提 WECHAT_KEY）。

        微信的"登录入口"就是微信客户端窗口：start.py 会先关掉微信、再让它自己弹出来登录。
        """
        if ctx.dry_run or ctx.options.get("no_elevate"):
            return StepResult(
                True,
                "需要人工（管理员）：python start.py（关微信→DbkeyHook→弹出的微信窗口里登录→提 WECHAT_KEY）",
                next_action="跑完 start.py 后重跑本流程（会从「验证」步骤自动检测）",
            )
        try:
            import setup_wizard

            def _default_launch(root):
                return setup_wizard.launch_first_run(root, "--no-scheduler")

            launch = ctx.options.get("launch_start_py") or _default_launch
            launched = bool(launch(ctx.root))
        except Exception as exc:  # noqa: BLE001
            return StepResult(
                False, f"自动提权拉起 start.py 失败：{exc}"[:160],
                next_action="手动：右键 PowerShell → 以管理员身份运行 → python start.py",
            )
        if not launched:
            return StepResult(
                False, "自动提权拉起 start.py 失败（可能取消了 UAC）",
                next_action="手动：右键 PowerShell → 以管理员身份运行 → python start.py",
            )
        return StepResult(
            True,
            "已弹出管理员窗口运行 start.py（只接入、不起服务）：关微信 → 提密钥 → "
            "**在弹出的微信窗口里登录鲸鲸号**",
            next_action="登录并看到「密钥已捕获 ✓」后重跑本流程；起服务请用启动器「启动鲸鲸」",
        )

    def verify(self, ctx: SetupContext) -> StepResult:
        import doctor

        env_path = ctx.root / ".env"
        checks = [
            doctor.check_env_file(env_path),
            doctor.check_wechat_env(env_path),
            doctor.check_wechat_running(),
        ]
        bad = [c for c in checks if getattr(c, "level", "") != doctor.OK]
        if not bad:
            return StepResult(True, "微信接入 OK（.env / 微信配置 / 密钥 / 进程）")
        detail = "；".join(
            f"{getattr(c, 'title', '')}" + (f"（{getattr(c, 'fix', '')}）" if getattr(c, "fix", "") else "")
            for c in bad
        )
        return StepResult(False, detail, next_action="按上面的建议修，或双击 体检鲸鲸.bat 看全量 12 项")

    def env_values(self, ctx: SetupContext) -> dict[str, str]:
        # 微信配置由 start.py / 安装向导负责写；统一流程不重复写，避免两套写入逻辑打架。
        return {}

    def acceptance(self, ctx: SetupContext) -> list[str]:
        return [
            "双击 体检鲸鲸.bat：12 项全绿（或只剩已知的沙箱项）",
            "给鲸鲸号发一条私聊，能收到回复",
            "看 logs/yinyue.log 出现 scheduler_running / feature_flags",
        ]


register_platform_setup(WechatSetup())
