"""NapCat 常驻控制：start / stop / status / ensure（无窗口，给启动器和手工用）。

用法::

    python scripts\\napcat_ctl.py status
    python scripts\\napcat_ctl.py start
    python scripts\\napcat_ctl.py stop
    python scripts\\napcat_ctl.py ensure    # 没跑才拉起（启动器「启动鲸鲸」前会自动调）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import qq_setup  # noqa: E402

# 管道输出统一 UTF-8（启动器按 UTF-8 读取，避免中文乱码）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _ensure_onebot(qq_setup, napcat_dir, *, elevate: bool) -> str:
    """检查当前登录号的 OneBot 通道；缺则自动补，需要时提权重启 NapCat。"""
    import subprocess
    import time as _time

    status, msg = qq_setup.ensure_current_onebot(napcat_dir)
    print("OneBot：" + msg)
    if status != "restart":
        return status
    ok, note = qq_setup.stop_napcat()
    if not ok:
        print("[!] 应用通道需要重启 NapCat：" + note)
        if elevate and sys.platform == "win32":
            self_script = Path(__file__).resolve()
            cmd = (f"Start-Process -FilePath '{sys.executable}' "
                   f"-ArgumentList '{self_script}','start','--no-open' -Verb RunAs")
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, timeout=60)
            print("已请求管理员重启 NapCat（请在 UAC 弹窗点『是』）")
            return "elevate"
        print("提示：用 python scripts/napcat_ctl.py start --elevate（或启动器「启动 NapCat」）可自动完成。")
        return "failed"
    _time.sleep(3)
    ok2, note2 = qq_setup.start_napcat(napcat_dir, open_webui=False)
    print("✓ OneBot 通道已应用（NapCat 已重启）" if ok2 else "[!] NapCat 重启失败：" + note2)
    return "ok" if ok2 else "failed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NapCat 常驻控制（无窗口）")
    parser.add_argument("action", choices=("start", "stop", "status", "ensure"))
    parser.add_argument("--napcat-dir", default="", help="NapCat 目录（默认项目下 napcat/）")
    parser.add_argument("--no-open", action="store_true",
                        help="启动后不自动打开 WebUI（默认自动打开，方便扫码登录）")
    parser.add_argument("--elevate", action="store_true",
                        help="权限不足时弹 UAC：start/ensure=以管理员启动；stop=以管理员强杀")
    args = parser.parse_args(argv)
    napcat_dir = Path(args.napcat_dir) if args.napcat_dir else (ROOT / "napcat")

    if args.action == "status":
        running = qq_setup.napcat_running(napcat_dir)
        url, _port = qq_setup.webui_target(napcat_dir)
        print("NapCat 正在运行（端口探测）" if running else "NapCat 没在运行")
        print(f"WebUI（登录扫码）：{url}")
        return 0 if running else 1

    if args.action in ("start", "ensure"):
        already = qq_setup.napcat_running(napcat_dir)
        # start=用户明确要启动（已在跑也帮打开登录页）；ensure=只在本次真正拉起时才打开
        want_open = (not args.no_open) and (args.action == "start" or not already)
        if already:
            print("NapCat 已经在跑，跳过启动")
            _ensure_onebot(qq_setup, napcat_dir, elevate=args.elevate)
            if want_open:
                opened = qq_setup.open_webui_when_ready_dir(napcat_dir, timeout=15)
                print("登录页已打开" if opened else "（WebUI 未就绪，稍后再点一次）")
            return 0
        if args.elevate and sys.platform == "win32":
            import subprocess
            self_script = Path(__file__).resolve()
            cmd = (f"Start-Process -FilePath '{sys.executable}' "
                   f"-ArgumentList '{self_script}','start','--no-open' -Verb RunAs")
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, timeout=60)
            print("已请求以管理员启动 NapCat（请在 UAC 弹窗点『是』）")
            if want_open:
                opened = qq_setup.open_webui_when_ready_dir(napcat_dir, timeout=45)
                print("登录页已打开" if opened else "（等 WebUI 超时：稍后用 status 检查）")
            return 0
        ok, note = qq_setup.start_napcat(napcat_dir, open_webui=False)
        print(("✓ " if ok else "[!] ") + note)
        if ok:
            _ensure_onebot(qq_setup, napcat_dir, elevate=args.elevate)
        if ok and want_open:
            opened = qq_setup.open_webui_when_ready_dir(napcat_dir, timeout=45)
            print("登录页已打开" if opened else "（WebUI 还没就绪：稍后再点一次即可）")
        return 0 if ok else 1
    ok, note = qq_setup.stop_napcat(elevate=args.elevate)
    print(("✓ " if ok else "[!] ") + note)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
