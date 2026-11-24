import os
import sys
import shutil
import winreg
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
BAT_PATH = PROJECT_ROOT / "autostart.bat"
STARTUP_DIR = Path(os.environ.get("APPDATA")) / r"Microsoft\Windows\Start Menu\Programs\Startup"
STARTUP_LNK = STARTUP_DIR / "yinyue.lnk"


def register_startup():
    if not BAT_PATH.exists():
        print(f"[!] 找不到 {BAT_PATH}")
        return False

    STARTUP_DIR.mkdir(parents=True, exist_ok=True)

    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(STARTUP_LNK))
        shortcut.Targetpath = str(BAT_PATH)
        shortcut.WorkingDirectory = str(PROJECT_ROOT)
        shortcut.Description = "yinyue 微信私人助理"
        shortcut.save()
        print(f"[OK] 开机自启已创建: {STARTUP_LNK}")
        return True
    except ImportError:
        reg_key = winreg.HKEY_CURRENT_USER
        reg_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.OpenKey(reg_key, reg_path, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "yinyue", 0, winreg.REG_SZ, f'cmd /c "{BAT_PATH}"')
            print(f"[OK] 注册表自启已创建 (HKEY_CURRENT_USER\\{reg_path}\\yinyue)")
            return True
        except Exception as e:
            print(f"[!] 注册表写入失败: {e}")
            return False


def unregister_startup():
    if STARTUP_LNK.exists():
        STARTUP_LNK.unlink()
        print(f"[OK] 已删除快捷方式: {STARTUP_LNK}")

    reg_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "yinyue")
    except FileNotFoundError:
        pass
    except Exception:
        pass
    print("[OK] 已清理注册表自启项")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print("用法:")
        print("  python install_startup.py enable   - 开启开机自启")
        print("  python install_startup.py disable  - 关闭开机自启")
        print("  python install_startup.py status   - 查看状态")
        return

    cmd = sys.argv[1].lower()
    if cmd == "enable":
        register_startup()
    elif cmd == "disable":
        unregister_startup()
    elif cmd == "status":
        exists = STARTUP_LNK.exists()
        print(f"快捷方式: {'存在' if exists else '不存在'} ({STARTUP_LNK})")
    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
