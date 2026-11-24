"""统一平台接入入口（五步流程）。

用法::

    python scripts\\platform_setup.py                    # 状态表（所有平台）
    python scripts\\platform_setup.py --platform qq      # 跑 QQ 五步
    python scripts\\platform_setup.py --platform qq --dry-run
    python scripts\\platform_setup.py --json

设计见 docs/平台接入统一流程.md；旧入口（安装鲸鲸.bat / QQ接入.bat）保留。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.onboarding import all_setups, get_platform_setup  # noqa: E402
from scripts.onboarding.base import STEP_KEYS, SetupContext, StepResult  # noqa: E402
from scripts.onboarding.common import render_status_table, write_env  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="鲸鲸 · 平台接入（统一五步）")
    parser.add_argument("--platform", default="", help="平台 key（qq/wechat/...）；缺省出状态表")
    parser.add_argument("--dry-run", action="store_true", help="只检查/不写 .env")
    parser.add_argument("--json", action="store_true", help="状态表输出 JSON")
    parser.add_argument("--owner", default="", help="主人账号（如 QQ 号/wxid）")
    parser.add_argument("--no-download", action="store_true", help="不自动下载接入端")
    parser.add_argument("--no-send", action="store_true", help="接入后先关闭发送")
    parser.add_argument("--http-url", default="", help="平台连接地址（如 OneBot HTTP）")
    parser.add_argument("--ws-url", default="", help="平台连接地址（如 OneBot WS）")
    parser.add_argument("--token", default="", help="平台访问 token")
    parser.add_argument("--napcat-dir", default="", help="NapCat 目录（跳过下载）")
    parser.add_argument("--install-dir", default="", help="接入端安装目录（默认 ./napcat）")
    parser.add_argument("--github-api", default="", help="GitHub API 镜像（下载失败时用）")
    parser.add_argument("--asset", default="", help="指定 release 资源名（手动挑包）")
    parser.add_argument("--github-proxy", default="", help="GitHub 下载代理前缀（留空=内置镜像候选链）")
    parser.add_argument("--no-start", action="store_true", help="不自动启动接入端（NapCat）")
    parser.add_argument("--no-elevate", action="store_true",
                        help="微信侧不自动提权拉起 start.py（只提示；默认会自动弹 UAC）")
    parser.add_argument("--start", action="store_true", help="强制启动接入端（NapCat，已装也能拉起）")
    parser.add_argument("--keep-going", action="store_true", help="某步失败仍继续（默认失败即停）")
    return parser


def status_table(as_json: bool) -> int:
    setups = all_setups()
    if as_json:
        out = {}
        for key, adapter in sorted(setups.items()):
            try:
                out[key] = dict(adapter.status())
            except Exception as exc:  # noqa: BLE001
                out[key] = {"error": str(exc)}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render_status_table(setups))
        print('\n账号管理（切换鲸鲸号/主人增删/导入）：服务在跑时打开 '
              'http://127.0.0.1:8972/accounts')
    return 0


def run_platform(args: argparse.Namespace) -> int:
    adapter = get_platform_setup(args.platform)
    if adapter is None:
        print(f"[!] 未知平台: {args.platform}（可用: {', '.join(sorted(all_setups()))}）")
        return 1

    options = {
        "owner": args.owner,
        "no_download": args.no_download,
        "no_send": args.no_send,
        "http_url": args.http_url,
        "ws_url": args.ws_url,
        "token": args.token,
        "napcat_dir": args.napcat_dir,
        "install_dir": args.install_dir,
        "github_api": args.github_api,
        "asset": args.asset,
        "github_proxy": args.github_proxy,
        "no_start": args.no_start,
        "no_elevate": args.no_elevate,
        "start": args.start,
    }
    ctx = SetupContext(root=ROOT, dry_run=args.dry_run, options=options)
    print(f"== {adapter.display}（{adapter.key}）· 统一接入五步 ==")
    steps = {s.key: s for s in adapter.steps()}
    failed = 0
    for step_key in STEP_KEYS:
        step = steps.get(step_key)
        title = step.title if step else step_key
        manual = "（人工）" if step and step.manual else ""
        print(f"\n{title}{manual}" + (f" —— {step.hint}" if step and step.hint else ""))
        try:
            if step_key == "prepare":
                result = adapter.prepare(ctx)
            elif step_key == "authorize":
                result = adapter.authorize(ctx)
            elif step_key == "verify":
                result = adapter.verify(ctx)
            elif step_key == "configure":
                values = adapter.env_values(ctx)
                if not values:
                    result = StepResult(True, "跳过：该平台配置由它自己的工具负责（如微信 start.py）")
                else:
                    actions = write_env(ctx.root, values, dry_run=args.dry_run,
                                        reason=adapter.key)
                    detail = "，".join(f"{key}({action})" for key, action in actions.items())
                    result = StepResult(True, ("[dry-run] 将写入: " if args.dry_run else "已写入: ") + detail)
            else:
                result = StepResult(True, "；".join(adapter.acceptance(ctx)))
        except Exception as exc:  # noqa: BLE001 - 单步失败给出可执行建议
            result = StepResult(False, f"步骤异常: {exc}", next_action="把上面的信息发给懂技术的人")
        print(("  OK " if result.ok else "  !! ") + str(result.detail))
        if result.next_action:
            print(f"  下一步：{result.next_action}")
        if not result.ok:
            failed += 1
            if not args.keep_going:
                break
    if failed:
        print(f"\n结果：有 {failed} 步未完成（修好后重跑本命令；全部通过会写入 .env）")
        return 1
    print("\n结果：五步全绿 ✓")
    return 0


def interactive_menu(args: argparse.Namespace) -> int:
    """控制台里先看状态表、再选平台跑五步（非 TTY 不会进来，避免管道里卡住）。"""
    while True:
        try:
            key = input("\n输入平台 key 跑它的五步（回车退出）: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not key:
            return 0
        if get_platform_setup(key) is None:
            print(f"[!] 未知平台: {key}（可用: {', '.join(sorted(all_setups()))}）")
            continue
        args.platform = key
        run_platform(args)
        print("\n（可继续选下一个平台；回车退出）")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.platform:
        return run_platform(args)
    status_table(args.json)
    if args.json or not sys.stdin.isatty():
        return 0
    return interactive_menu(args)


if __name__ == "__main__":
    raise SystemExit(main())
