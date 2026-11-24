"""QQ（NapCat / OneBot v11）适配器：把 qq_setup.py 的能力装进统一五步。"""

from __future__ import annotations

import json
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
    sys.path.insert(0, str(SCRIPTS_DIR))  # 让 qq_setup 可直接 import


def _verify(ctx: SetupContext) -> dict:
    return dict(ctx.options.get("verify") or {})


class QQSetup:
    key = "qq"
    display = "QQ（NapCat）"

    # ---- 契约 ----

    def steps(self) -> list[Step]:
        return [
            Step("prepare", "① 准备接入端（NapCat）",
                 hint="没装会自动从官方 GitHub Release 下载解压（--no-download 可关）"),
            Step("authorize", "② 登录/授权", manual=True,
                 hint="NapCat 会拉起 QQ 客户端 → 在 QQ 窗口登录鲸鲸号（或 NapCat WebUI http://127.0.0.1:6099 扫码）；"
                      "OneBot 的 HTTP+正向 WS 没开的话下一步会自动补"),
            Step("verify", "③ 验证能用", hint="get_login_info 探活 + 正向 WS 握手"),
            Step("configure", "④ 写配置(.env)",
                 hint="PLATFORMS_QQ_ACCOUNT / PLATFORMS_QQ_OWNER_ACCOUNTS + QQ_WS_URL / QQ_HTTP_URL / QQ_ACCESS_TOKEN / QQ_SEND_ENABLED"),
            Step("accept", "⑤ 验收", hint="重启后看日志 + 做一次最小真实动作"),
        ]

    def status(self) -> dict:
        env = read_env(ROOT / ".env")
        account = env.get("PLATFORMS_QQ_ACCOUNT", "")
        ws_url = env.get("QQ_WS_URL", "")
        http_url = env.get("QQ_HTTP_URL", "")
        owner = env.get("PLATFORMS_QQ_OWNER_ACCOUNTS", "")
        if not account:
            nxt = "跑 `平台接入.bat`（或 python scripts\\platform_setup.py --platform qq）"
        elif not (ws_url and http_url):
            nxt = "缺连接配置（QQ_WS_URL / QQ_HTTP_URL）：重跑接入流程的验证步骤"
        elif not owner:
            nxt = "已配连接，还缺主人 QQ 号（PLATFORMS_QQ_OWNER_ACCOUNTS）"
        else:
            nxt = "已配置；重启鲸鲸后看日志 qq_provider_connected / qq_ws_connected"
        return {
            "configured": bool(account),
            "credentials_ok": bool(account and ws_url and http_url),
            "next_step": nxt,
            "last_error": "",
        }

    def prepare(self, ctx: SetupContext) -> StepResult:
        import qq_setup

        ensure = ctx.options.get("ensure_napcat_install") or qq_setup.ensure_napcat_install
        install_dir = Path(str(ctx.options.get("install_dir") or (ctx.root / "napcat")))
        resolved, state = ensure(
            install_dir,
            auto_download=not bool(ctx.options.get("no_download")),
            api_url=str(ctx.options.get("github_api") or ""),
            asset_hint=str(ctx.options.get("asset") or ""),
            proxy=str(ctx.options.get("github_proxy") or ""),
        )
        if resolved is None:
            return StepResult(
                False, f"NapCat 未就绪（{state}）",
                next_action="联网重跑；或手动下载官方 Release，用 --napcat-dir <目录> 指定",
            )
        started_note = ""
        if not ctx.options.get("no_start"):
            target = Path(str(resolved))
            running_check = ctx.options.get("napcat_running") or qq_setup.napcat_running
            start_fn = ctx.options.get("start_napcat") or qq_setup.start_napcat
            running = bool(running_check(target))
            if state.startswith("downloaded") or ctx.options.get("start") or not running:
                ok_start, note = start_fn(target)
                if not ok_start and "QQ.exe" in note:
                    return StepResult(
                        False, f"NapCat 就绪，但没法启动：{note}",
                        next_action="先安装 QQ PC 版（NT 内核，https://im.qq.com/pcqq ）；"
                                    "装完重跑本流程（会自动拉起 NapCat 并打开 WebUI 扫码）",
                    )
                started_note = f"；{note}" if ok_start else f"；自动启动失败：{note}"
            else:
                started_note = "；NapCat 已在运行（端口探测），跳过启动"
        ctx.options["napcat_dir"] = str(resolved)
        return StepResult(True, f"NapCat 就绪：{resolved}（{state}）{started_note}")

    def authorize(self, ctx: SetupContext) -> StepResult:
        return StepResult(
            True,
            "需要人工：启动 NapCat → 手机 QQ 扫码登录 → WebUI 开 OneBot v11 的 HTTP + 正向 WS",
            next_action="做完后重跑本流程（会从「验证」步骤自动检测）",
        )

    def verify(self, ctx: SetupContext) -> StepResult:
        import qq_setup

        napcat_dir = Path(ctx.options.get("napcat_dir") or (ctx.root / "napcat"))
        probe_login = ctx.options.get("probe_login") or qq_setup.probe_login
        probe_http = ctx.options.get("probe_http_ports") or qq_setup.probe_http_ports
        probe_ws = ctx.options.get("probe_ws") or qq_setup.probe_ws

        http_url = str(ctx.options.get("http_url") or "")
        token = str(ctx.options.get("token") or "")
        parsed = {"http": None, "ws": None, "ws_reverse_only": False}
        for path in qq_setup.find_config_files(napcat_dir) if napcat_dir.exists() else []:
            try:
                candidate = qq_setup.parse_onebot_config(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except Exception:  # noqa: BLE001
                continue
            if candidate.get("http") or candidate.get("ws"):
                parsed = candidate
                break
        if not http_url and parsed.get("http"):
            http_url = f"http://{parsed['http']['host']}:{parsed['http']['port']}"
            token = token or parsed["http"].get("token", "")

        login = probe_login(http_url, token=token) if http_url else None
        if not login or "error" in login:
            found = probe_http(getattr(qq_setup, "DEFAULT_HTTP_PORTS", (3000, 3001, 3002, 5700)),
                               token=token)
            login = found if found and "error" not in found else None
        if not login:
            fixed, fix_note = qq_setup.ensure_onebot_network(napcat_dir, token=token)
            if fixed and "已自动写入" in fix_note:
                return StepResult(
                    False, f"没连上 OneBot HTTP；已自动补好配置：{fix_note}",
                    next_action="重启 NapCat（关掉它的控制台窗口，再双击 napcat.bat）后重跑本流程",
                )
            extra = f"（{fix_note}）" if fix_note else ""
            return StepResult(
                False, f"没连上 OneBot HTTP（NapCat 没启动 / QQ 没登录 / HTTP 服务没开）{extra}",
                next_action="启动 NapCat → 手机 QQ 扫码 → 再重跑验证（OneBot 没开的话会自动补）",
            )

        ws_url = str(ctx.options.get("ws_url") or "")
        if not ws_url and parsed.get("ws"):
            ws_url = f"ws://{parsed['ws']['host']}:{parsed['ws']['port']}"
            token = token or parsed["ws"].get("token", "")
        ws_state = str(probe_ws(ws_url)) if ws_url else "未拿到 WS 地址"
        ok = bool(ws_url) and "失败" not in ws_state and "error" not in ws_state.lower()
        ctx.options["verify"] = {
            "login": login, "ws_url": ws_url, "ws": ws_state, "token": token,
        }
        detail = (f"HTTP 已连：{login.get('nickname') or ''} / {login.get('user_id') or ''}；"
                  f"WS：{ws_state}")
        return StepResult(ok, detail,
                          next_action="" if ok else "去 NapCat WebUI 加一个「正向 WS 服务端」再验证")

    def env_values(self, ctx: SetupContext) -> dict[str, str]:
        import qq_setup

        data = _verify(ctx)
        login = data.get("login") or {}
        values = qq_setup.build_env_values(
            qq_account=str(login.get("user_id") or ctx.options.get("qq_account") or ""),
            http_url=str(login.get("http_url") or ctx.options.get("http_url") or ""),
            ws_url=str(data.get("ws_url") or ctx.options.get("ws_url") or ""),
            token=str(data.get("token") or ctx.options.get("token") or ""),
            send_enabled=not bool(ctx.options.get("no_send")),
            owner=str(ctx.options.get("owner") or ""),
        )
        return {key: str(value) for key, value in values.items() if value is not None}

    def acceptance(self, ctx: SetupContext) -> list[str]:
        return [
            "重启鲸鲸（先停旧再起），日志看 qq_provider_connected / qq_ws_connected",
            "私聊发一句、群里 @ 她，能收能回（send_log 里 platform=qq）",
            "进阶验收：群成员名单 / 收图识别 / 引用图片定位（批次 2-4）",
        ]


register_platform_setup(QQSetup())
