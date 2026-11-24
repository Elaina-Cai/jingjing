"""
yinyue 一键启动脚本

使用方法:
  1. 确保微信已打开并登录
  2. 以管理员身份运行: python start.py
  3. 脚本自动完成: 检测账号 → 提取密钥 → 更新配置 → 启动调度器

如果微信未登录，脚本会提示你先登录。
"""
import functools
print = functools.partial(print, flush=True)

import os
import sys
import re
import time
import subprocess
import signal

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DBKEYHOOK_DIR = os.path.join(PROJECT_ROOT, "tools", "DbkeyHook")
DBKEYHOOK_CMD = os.path.join(DBKEYHOOK_DIR, "DbkeyHookCMD.exe")
DBKEYHOOK_UI = os.path.join(DBKEYHOOK_DIR, "DbkeyHookUI.exe")
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")
DBKEY_TXT = "dbkey.txt"


def step(n, total, msg):
    print(f"\n{'='*60}")
    print(f"  [{n}/{total}] {msg}")
    print(f"{'='*60}")


def check_wechat_running():
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True
    )
    for line in result.stdout.strip().split("\n"):
        if "Weixin.exe" in line:
            parts = line.strip('"').split('","')
            if len(parts) >= 2:
                return int(parts[1])
    return None


def check_ollama_running():
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3)
        return resp.status == 200
    except Exception as _:
        return False


def start_ollama():
    print("  启动 Ollama 服务...")
    subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
    )
    for _ in range(15):
        time.sleep(1)
        if check_ollama_running():
            print("  Ollama 已启动 ✓")
            return True
    print("  [!] Ollama 启动超时，请手动运行: ollama serve")
    return False


def extract_key_with_hook():
    print("  关闭微信进程...")
    for proc_name in ["WeChatAppEx.exe", "Weixin.exe", "WeChat.exe"]:
        subprocess.run(
            ["taskkill", "/F", "/IM", proc_name],
            capture_output=True, text=True, timeout=5
        )
    time.sleep(2)

    print("  启动 DbkeyHookCMD...")
    print("  >>> 请在弹出的微信窗口中登录 <<<\n")

    proc = subprocess.Popen(
        [DBKEYHOOK_CMD],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=DBKEYHOOK_DIR,
    )

    wechat_dir = _find_wechat_dir()
    dbkey_path = os.path.join(wechat_dir, DBKEY_TXT) if wechat_dir else None

    key = _wait_for_dbkey(dbkey_path, timeout=120)

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception as _:
            proc.kill()

    if dbkey_path and os.path.exists(dbkey_path):
        try:
            os.remove(dbkey_path)
        except Exception as _:
            pass

    return key


def _find_wechat_dir():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat")
        path, _ = winreg.QueryValueEx(key, "InstallPath")
        winreg.CloseKey(key)
        return path
    except Exception as _:
        pass
    for p in [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tencent\WeChat"),
        os.path.expandvars(r"%PROGRAMFILES%\Tencent\WeChat"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Tencent\WeChat"),
    ]:
        if os.path.isdir(p):
            return p
    return None


def _wait_for_dbkey(dbkey_path, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if dbkey_path and os.path.exists(dbkey_path):
            try:
                content = open(dbkey_path, 'r').read().strip()
                if content:
                    keys = re.findall(r'[0-9a-fA-F]{64}', content)
                    if keys:
                        key = keys[0].lower()
                        print(f"  密钥已捕获: {key[:16]}...{key[-8:]} ✓")
                        return key
            except Exception as _:
                pass

        for root, dirs, files in os.walk(DBKEYHOOK_DIR):
            for f in files:
                if f == DBKEY_TXT:
                    path = os.path.join(root, f)
                    try:
                        content = open(path, 'r').read().strip()
                        keys = re.findall(r'[0-9a-fA-F]{64}', content)
                        if keys:
                            key = keys[0].lower()
                            print(f"  密钥已捕获: {key[:16]}...{key[-8:]} ✓")
                            return key
                    except Exception as _:
                        pass

        elapsed = int(time.time() - t0)
        if elapsed % 10 == 0 and elapsed > 0:
            sys.stdout.write(f"\r  等待登录... {elapsed}s ")
            sys.stdout.flush()
        time.sleep(1)

    print(f"\n  [!] 等待超时 ({timeout}s)")
    return None


def update_env_key(key):
    with open(ENV_FILE, 'r', encoding='utf-8') as f:
        content = f.read()

    if 'WECHAT_KEY=' in content:
        content = re.sub(
            r'WECHAT_KEY=.*',
            f'WECHAT_KEY={key}',
            content
        )
    else:
        content += f'\nWECHAT_KEY={key}\n'

    with open(ENV_FILE, 'w', encoding='utf-8') as f:
        f.write(content)

    print("  .env 已更新 ✓")


def _set_env_value(content: str, key: str, value: str) -> str:
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
    else:
        content += f"\n{key}={value}\n"
    return content


def detect_wechat_accounts():
    """自动检测微信账号并配置 .env"""
    sys.path.insert(0, PROJECT_ROOT)
    from scripts.get_wechat_info import find_wechat_db_dirs, detect_current_login, read_env

    dirs = find_wechat_db_dirs()
    current = detect_current_login()
    env = read_env()

    yinyue_wxid = env.get("WECHAT_YINYUE_WXID", "")
    owner_wxids = env.get("WECHAT_OWNER_WXIDS", "")
    db_path = env.get("WECHAT_DB_PATH", "")

    if not dirs:
        print("  [!] 未找到微信账号数据目录，跳过账号检测")
        print("      稍后可运行: python scripts/get_wechat_info.py --set-env")
        return

    print(f"  找到 {len(dirs)} 个微信账号：")
    for i, d in enumerate(dirs, 1):
        is_current = current and d["wxid"] == current["wxid"]
        marker = " ← 当前登录" if is_current else ""
        role = ""
        if d["wxid"] == yinyue_wxid:
            role = " [鲸鲸号]"
        if d["wxid"] in [x.strip() for x in owner_wxids.split(",") if x.strip()]:
            role += " [主人号]"
        print(f"    [{i}] {d['wxid']}{marker}{role}")

    if yinyue_wxid and owner_wxids and db_path:
        print("\n  .env 已有配置:")
        print(f"    鲸鲸号: {yinyue_wxid}")
        print(f"    主人号: {owner_wxids}")
        print(f"    数据库: {db_path}")
        return

    print()
    if current:
        print(f"  当前登录: {current['wxid']}")
        print("  假设当前登录的是鲸鲸的号（鲸鲸）")

    if len(dirs) == 1:
        yinyue_wxid = dirs[0]["wxid"]
        db_path = dirs[0]["db_storage"]
        print(f"  只有一个账号，自动设为鲸鲸号: {yinyue_wxid}")
        print("  主人号需要手动配置")
    elif len(dirs) >= 2 and current:
        yinyue_wxid = current["wxid"]
        db_path = current["db_storage"]
        others = [d for d in dirs if d["wxid"] != current["wxid"]]
        if others:
            owner_wxids = others[0]["wxid"]
            print(f"  鲸鲸号: {yinyue_wxid}")
            print(f"  主人号: {owner_wxids}（另一个账号）")
    else:
        print("  无法自动判断，请手动配置:")
        print("    python scripts/get_wechat_info.py --set-env")
        return

    with open(ENV_FILE, 'r', encoding='utf-8') as f:
        content = f.read()

    content = _set_env_value(content, "WECHAT_YINYUE_WXID", yinyue_wxid)
    if owner_wxids:
        content = _set_env_value(content, "WECHAT_OWNER_WXIDS", owner_wxids)
    if db_path:
        content = _set_env_value(content, "WECHAT_DB_PATH", db_path)

    with open(ENV_FILE, 'w', encoding='utf-8') as f:
        f.write(content)

    print("\n  .env 已更新:")
    print(f"    WECHAT_YINYUE_WXID = {yinyue_wxid}")
    if owner_wxids:
        print(f"    WECHAT_OWNER_WXIDS = {owner_wxids}")
    if db_path:
        print(f"    WECHAT_DB_PATH = {db_path}")


def start_scheduler():
    print("  启动调度器...")
    print("  (Ctrl+C 退出)\n")

    os.chdir(PROJECT_ROOT)
    os.execv(sys.executable, [sys.executable, "-c", "from scheduler.main import main; main()"])


def main():
    TOTAL_STEPS = 5

    print("\n" + "=" * 60)
    print("  yinyue 一键启动")
    print("  微信私人助理系统")
    print("=" * 60)

    step(1, TOTAL_STEPS, "检测微信账号")
    detect_wechat_accounts()

    step(2, TOTAL_STEPS, "检查 Ollama 服务")
    if check_ollama_running():
        print("  Ollama 已运行 ✓")
    else:
        if not start_ollama():
            print("  继续启动（Ollama 非必须，DeepSeek API 可用即可）")

    step(3, TOTAL_STEPS, "提取微信数据库密钥")
    if not os.path.exists(DBKEYHOOK_CMD):
        print("  [!] 找不到 DbkeyHookCMD.exe")
        print(f"      路径: {DBKEYHOOK_CMD}")
        print("      请从 https://github.com/gzygood/DbkeyHook/releases 下载")
        key = None
    else:
        key = extract_key_with_hook()

    if not key:
        print("\n  密钥提取失败，尝试使用 .env 中的现有密钥...")
        with open(ENV_FILE, 'r', encoding='utf-8') as f:
            m = re.search(r'WECHAT_KEY=([0-9a-fA-F]{64})', f.read())
            if m:
                key = m.group(1)
                print(f"  使用现有密钥: {key[:16]}...{key[-8:]}")
            else:
                print("  [!] .env 中也没有密钥，无法继续")
                print("      请手动运行 DbkeyHookUI.exe 获取密钥后写入 .env")
                sys.exit(1)

    step(4, TOTAL_STEPS, "更新配置")
    update_env_key(key)

    if "--no-scheduler" in sys.argv:
        step(5, TOTAL_STEPS, "跳过启动调度器（--no-scheduler）")
        print("  接入完成；启动请用 鲸鲸启动器（或 autostart.bat）")
        return
    step(5, TOTAL_STEPS, "启动调度器")
    start_scheduler()


if __name__ == "__main__":
    main()
