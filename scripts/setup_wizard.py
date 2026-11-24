"""鲸鲸一键安装向导（stdlib only，可在装依赖之前运行）。

目标：把「不会代码的人第一次装鲸鲸」收敛到一次运行：

    [1/7] Python 版本检查
    [2/7] 依赖安装（pip install -r requirements.txt）
    [3/7] .env 准备（缺失就从 .env.example 复制；已有则先备份）
    [4/7] 基本配置（API key / 发送开关 / 时区）
    [5/7] autostart.bat 路径自适应（%~dp0）
    [6/7] Ollama 检查（可选拉模型）
    [7/7] 体检（scripts/doctor.py）
    最后：只提示「下一步去哪做平台接入」（安装=环境；接入在启动器「平台接入」里做）

用法::

    python scripts/setup_wizard.py                # 交互式
    python scripts/setup_wizard.py --yes          # 无人值守（默认值）
    python scripts/setup_wizard.py --mirror       # pip 走清华镜像
    python scripts/setup_wizard.py --api-key sk-xxx
    python scripts/setup_wizard.py --skip-install --skip-ollama --no-start

双击项目根目录的「安装鲸鲸.bat」等价于 ``python scripts/setup_wizard.py``。
本模块只用标准库：它要能在第三方依赖装好之前就跑起来。
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

MIN_PYTHON = (3, 12)
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 依赖检查用：import 名 -> 说明（pip 包名不同的一并写清）
REQUIRED_MODULES: dict[str, str] = {
    "sqlalchemy": "sqlalchemy",
    "alembic": "alembic",
    "openai": "openai",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "jinja2": "jinja2",
    "multipart": "python-multipart",
    "structlog": "structlog",
    "schedule": "schedule",
    "lz4": "lz4",
    "zstandard": "zstandard",
    "httpx": "httpx",
    "pymem": "pymem",
    "Cryptodome": "pycryptodomex",
    "cv2": "opencv-python",
    "uiautomation": "uiautomation",
}

PROVIDER_KEY_FIELDS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
}
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def backup_file(path: Path, tag: str = "setup") -> Path | None:
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.bak_{tag}_{_stamp()}")
    shutil.copy2(path, target)
    return target


def read_env_map(path: Path) -> dict[str, str]:
    """读 .env 为 dict；同一键重复时沿用「谁先出现」（仅用于展示/填空）。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in values:
            values[key] = value.strip()
    return values


def duplicate_keys(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not path.exists():
        return counts
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return counts
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if key:
            counts[key] = counts.get(key, 0) + 1
    return {key: n for key, n in counts.items() if n > 1}


def set_env_values(path: Path, values: dict[str, str]) -> dict[str, str]:
    """原地更新 .env 里的键。

    - 已存在的键（含重复出现的所有行）统一替换为新值，避免「先/后生效」两套
      加载口径带来的歧义；
    - 缺失的键追加到文件末尾；
    - 只碰行首的 ``KEY=``，注释行不动。
    """
    actions: dict[str, str] = {}
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    for key, value in values.items():
        pattern = re.compile(rf"^([ \t]*){re.escape(key)}=[^\r\n]*", re.MULTILINE)
        found = len(pattern.findall(text))
        if found:
            text = pattern.sub(rf"\g<1>{key}={value}", text)
            actions[key] = "updated" if found == 1 else f"updated x{found}"
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"{key}={value}\n"
            actions[key] = "added"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return actions


def ensure_env_file(root: Path) -> tuple[Path, str]:
    env_path = root / ".env"
    if env_path.exists():
        return env_path, "exists"
    example = root / ".env.example"
    if example.exists():
        shutil.copy2(example, env_path)
        return env_path, "created_from_example"
    env_path.write_text("# 鲸鲸配置（由安装向导生成）\n", encoding="utf-8")
    return env_path, "created_empty"


def fix_autostart_bat(path: Path) -> str:
    """把 autostart.bat 的 ``cd /d "..."`` 改成自适应的 ``cd /d "%~dp0"``。"""
    if not path.exists():
        return "missing"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unreadable"
    pattern = re.compile(r'^cd /d ".*?"[ \t]*$', re.MULTILINE)
    matches = pattern.findall(text)
    if not matches:
        return "ok_manual" if "%~dp0" in text else "no_cd_line"
    if all('"%~dp0"' in m for m in matches):
        return "ok"
    backup = backup_file(path, "setup")
    text = pattern.sub('cd /d "%~dp0"', text)
    path.write_text(text, encoding="utf-8")
    return f"fixed(backup={backup.name if backup else 'none'})"


# ---------------------------------------------------------------------------
# 检查 / 安装
# ---------------------------------------------------------------------------


def check_python(minimum: tuple[int, int] = MIN_PYTHON) -> tuple[bool, str]:
    info = sys.version_info
    label = f"{info.major}.{info.minor}.{info.micro}"
    return info[:2] >= minimum, label


def missing_modules(modules: dict[str, str] | None = None) -> list[str]:
    table = modules if modules is not None else REQUIRED_MODULES
    return [name for name in table if importlib.util.find_spec(name) is None]


def install_dependencies(root: Path, *, mirror: bool = False, runner=None) -> bool:
    runner = runner or subprocess.run
    requirements = root / "requirements.txt"
    if not requirements.exists():
        print(f"  [!] 找不到 {requirements}")
        return False
    if importlib.util.find_spec("pip") is None:
        print("  当前 Python 没有 pip，先跑 ensurepip ...")
        try:
            runner([sys.executable, "-m", "ensurepip", "--upgrade"], cwd=str(root))
        except Exception as exc:  # noqa: BLE001 - 继续尝试安装
            print(f"  [!] ensurepip 失败: {exc}")
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(requirements)]
    if mirror:
        cmd += ["-i", PYPI_MIRROR]
        print("  （使用清华镜像加速）")
    print("  执行:", " ".join(cmd))
    try:
        result = runner(cmd, cwd=str(root))
    except FileNotFoundError as exc:
        print(f"  [!] pip 安装失败: {exc}")
        return False
    return getattr(result, "returncode", 1) == 0


def ollama_models(url: str = "http://localhost:11434/api/tags", timeout: float = 3.0) -> list[str] | None:
    """返回已安装的模型名；服务不可达时返回 None。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            import json

            data = json.load(resp)
    except Exception:  # noqa: BLE001 - 网络/服务不可达都算「没起来」
        return None
    return [str(item.get("name", "")) for item in data.get("models", [])]


def has_model(models: list[str], model: str) -> bool:
    if not model:
        return True
    return any(name == model or name.startswith(model) for name in models)


def pull_ollama_model(model: str, *, runner=None) -> bool:
    runner = runner or subprocess.run
    exe = shutil.which("ollama")
    if not exe:
        print("  [!] 没找到 ollama 命令，请先安装 Ollama: https://ollama.com/download")
        return False
    print(f"  下载模型 {model}（可能需要几 GB，请耐心等）...")
    result = runner([exe, "pull", model])
    return getattr(result, "returncode", 1) == 0


def launch_first_run(root: Path, extra_args: str = "") -> bool:
    """以管理员权限弹出 start.py（提密钥；extra_args 可传 --no-scheduler 只接入不起服务）。"""
    run_args = ("start.py " + str(extra_args or "")).strip()
    command = (
        "Start-Process -FilePath '{}' -ArgumentList '{}' "
        "-WorkingDirectory '{}' -Verb RunAs"
    ).format(sys.executable.replace("'", "''"), run_args.replace("'", "''"),
             str(root).replace("'", "''"))
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] 启动失败: {exc}")
        return False


# ---------------------------------------------------------------------------
# 交互
# ---------------------------------------------------------------------------


def ask_yes_no(prompt: str, *, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = _input(f"{prompt} [{hint}] ")
    if not raw:
        return default
    return raw.strip().lower() in ("y", "yes", "是", "1", "true")


def _input(prompt: str, default: str = "") -> str:
    try:
        raw = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    raw = raw.strip()
    return raw if raw else default


def _ask_secret(prompt: str) -> str:
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    except Exception:  # noqa: BLE001 - 某些控制台不支持隐藏输入
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""


def collect_env_updates(
    env: dict[str, str],
    *,
    interactive: bool = True,
    api_key: str = "",
) -> dict[str, str]:
    updates: dict[str, str] = {}
    provider = (env.get("PROVIDER") or "deepseek").strip().lower() or "deepseek"
    key_field = PROVIDER_KEY_FIELDS.get(provider, f"{provider.upper()}_API_KEY")

    if api_key:
        updates[key_field] = api_key
        print(f"  {key_field} 使用命令行传入的值")
    elif (env.get(key_field) or "").strip():
        current = env[key_field].strip()
        print(f"  检测到 {key_field} 已填写（{current[:6]}...）")
        if interactive and ask_yes_no("  要更换吗？", default=False):
            new_key = _ask_secret(f"  新的 {key_field}: ")
            if new_key:
                updates[key_field] = new_key
    else:
        print(f"  需要 {key_field}（去 {provider} 官网申请一个）")
        if interactive:
            new_key = _ask_secret(f"  请粘贴 {key_field}（直接回车=先跳过）: ")
            if new_key:
                updates[key_field] = new_key
            else:
                print("  [!] 先跳过；没有 key 鲸鲸回不了话，补上后重跑本向导即可")
        else:
            print("  [!] 未提供 key（无人值守模式），先留空")

    send = (env.get("WECHAT_SEND_ENABLED") or "").strip().lower()
    if send not in ("1", "true", "yes", "on"):
        if not interactive:
            updates["WECHAT_SEND_ENABLED"] = "true"
        elif ask_yes_no("  允许鲸鲸自动发送微信消息（WECHAT_SEND_ENABLED=true）？", default=True):
            updates["WECHAT_SEND_ENABLED"] = "true"

    if not (env.get("TEMPORAL_TIMEZONE") or "").strip():
        tz = ""
        if interactive:
            tz = _input("  展示时区（IANA 名，直接回车=Asia/Shanghai）: ", default="Asia/Shanghai")
        else:
            tz = "Asia/Shanghai"
        if tz:
            updates["TEMPORAL_TIMEZONE"] = tz
    return updates


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="鲸鲸一键安装向导")
    parser.add_argument("--yes", action="store_true", help="无人值守，全部用默认值")
    parser.add_argument("--mirror", action="store_true", help="pip 使用清华镜像")
    parser.add_argument("--api-key", default="", help="直接提供 LLM API key（默认提供方看 PROVIDER）")
    parser.add_argument("--skip-install", action="store_true", help="跳过依赖安装")
    parser.add_argument("--skip-ollama", action="store_true", help="跳过 Ollama 检查")
    parser.add_argument("--start", action="store_true",
                        help="兼容：装完直接管理员启动 start.py（默认不启动；接入请在启动器「平台接入」做）")
    parser.add_argument("--no-start", action="store_true", help="最后不要启动")
    parser.add_argument("--project-root", default="", help="项目目录（默认取本脚本上级目录）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.project_root).resolve() if args.project_root else project_root()
    interactive = not args.yes
    os.chdir(root)  # 让相对路径（.env / requirements.txt）都指向项目

    print("=" * 60)
    print("  鲸鲸 · 一键安装向导")
    print(f"  项目目录: {root}")
    print("=" * 60)

    # [1/7] Python
    ok, version = check_python()
    print(f"\n[1/7] Python 版本: {version}")
    if not ok:
        print(f"  [!] 需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更高。")
        print("      去 https://www.python.org/downloads/ 下载安装，")
        print('      安装时务必勾选 "Add python.exe to PATH"，然后重新运行本向导。')
        return 1
    print("  通过")

    # [2/7] 依赖
    print("\n[2/7] 检查依赖")
    missing = missing_modules()
    if missing:
        print(f"  缺少 {len(missing)} 个: {', '.join(missing)}")
        if args.skip_install:
            print("  [!] 已按 --skip-install 跳过安装")
        else:
            use_mirror = args.mirror or (
                interactive and ask_yes_no("  下载依赖可能较慢，使用清华镜像加速？", default=False)
            )
            if install_dependencies(root, mirror=use_mirror):
                missing = missing_modules()
                if missing:
                    print(f"  [!] 仍有缺失: {', '.join(missing)}（可重跑本向导）")
                else:
                    print("  依赖安装完成")
            else:
                print("  [!] 依赖安装失败：检查网络后重跑本向导（或手动 pip install -r requirements.txt）")
    else:
        print("  依赖齐全")

    # [3/7] .env
    print("\n[3/7] 准备 .env")
    env_path, how = ensure_env_file(root)
    if how == "exists":
        backup = backup_file(env_path, "setup")
        print(f"  使用已有 .env（已备份: {backup.name if backup else '无'}）")
    else:
        print(f"  .env {how}")
    duplicates = duplicate_keys(env_path)
    if duplicates:
        print(f"  [!] 检测到重复键（会被向导统一成一个值）: {', '.join(sorted(duplicates))}")

    # [4/7] 基本配置
    print("\n[4/7] 基本配置")
    env = read_env_map(env_path)
    updates = collect_env_updates(env, interactive=interactive, api_key=args.api_key)
    if updates:
        actions = set_env_values(env_path, updates)
        for key, action in actions.items():
            shown = updates[key]
            if "KEY" in key and len(shown) > 12:
                shown = shown[:6] + "..." + shown[-4:]
            print(f"  {key} = {shown}（{action}）")
    else:
        print("  无需改动")

    # [5/7] autostart.bat
    print("\n[5/7] autostart.bat 路径自适应")
    status = fix_autostart_bat(root / "autostart.bat")
    if status == "ok":
        print("  已是 %~dp0 自适应，无需改路径")
    elif status == "missing":
        print("  没有找到 autostart.bat（如果不需要双击启动可忽略）")
    elif status.startswith("fixed"):
        print(f"  已改成 %~dp0 自适应（{status}）")
    else:
        print(f"  {status}")

    # [6/7] Ollama
    print("\n[6/7] Ollama 检查")
    if args.skip_ollama:
        print("  已按 --skip-ollama 跳过")
    else:
        model = (read_env_map(env_path).get("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL).strip()
        models = ollama_models()
        if models is None:
            print("  Ollama 没起来。它是本地模型服务（快路径/降级要用），不是必须但强烈建议：")
            print("    1) 安装: https://ollama.com/download  2) 启动 Ollama 后重跑本向导")
        elif has_model(models, model):
            print(f"  Ollama 在跑，模型 {model} 已安装")
        else:
            print(f"  Ollama 在跑，但缺模型 {model}")
            if interactive and ask_yes_no(f"  现在下载 {model} 吗？", default=True):
                pull_ollama_model(model)
            else:
                print(f"  可稍后手动执行: ollama pull {model}")

    # [7/7] 体检
    print("\n[7/7] 体检")
    doctor = root / "scripts" / "doctor.py"
    if doctor.exists():
        subprocess.run([sys.executable, str(doctor)], cwd=str(root))
    else:
        print("  未找到 scripts/doctor.py，跳过")

    # 安装到此为止；接入平台请在启动器里做（安装=环境，接入=平台，启动=启动器）
    print()
    if args.start and not args.no_start:
        print("（兼容模式）用管理员权限打开 start.py：检测账号 → 提密钥 → 写 .env → 起服务")
        if launch_first_run(root):
            print("  已弹出管理员窗口，请按那边的提示继续。")
        else:
            print("  没能自动拉起，请手动：右键 PowerShell → 以管理员身份运行 → python start.py")
    else:
        print("安装完成 ✓（安装只装环境；平台接入请在启动器里做）")
        print("下一步：双击 鲸鲸启动器.pyw → 点「平台接入」→ 选 wechat / qq → 按五步走")
        print("  · wechat：自动弹 UAC 提密钥 → 微信窗口弹出登录 → 写 .env（只接入、不起服务）")
        print("  · qq    ：自动装 NapCat → 自动打开 WebUI 扫码 → 写 .env")
        print("接入完成后用启动器「启动鲸鲸」起服务；体检鲸鲸.bat 随时可跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
