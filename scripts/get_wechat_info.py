"""
获取当前登录微信账号信息（wxid、昵称、数据库路径等）

用法:
  python scripts/get_wechat_info.py          # 自动检测
  python scripts/get_wechat_info.py --json    # JSON 输出
  python scripts/get_wechat_info.py --set-env # 自动写入 .env
"""
from __future__ import annotations

import functools
print = functools.partial(print, flush=True)

import argparse
import json
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def find_wechat_db_dirs() -> list[dict]:
    """扫描 xwechat_files 目录，找到所有微信账号的数据库路径"""
    docs = os.path.expandvars(r"%USERPROFILE%\Documents")
    xwechat = os.path.join(docs, "xwechat_files")
    if not os.path.isdir(xwechat):
        return []

    results = []
    for name in os.listdir(xwechat):
        m = re.match(r"(wxid_[a-zA-Z0-9]+)_[a-f0-9]+", name)
        if not m:
            continue
        wxid = m.group(1)
        db_storage = os.path.join(xwechat, name, "db_storage")
        if os.path.isdir(db_storage):
            results.append({
                "wxid": wxid,
                "db_dir": os.path.join(xwechat, name),
                "db_storage": db_storage,
            })
    return results


def get_nickname_from_contacts(db_path: str, wxid: str) -> str | None:
    """从 yinyue.db 的 contacts 表查昵称"""
    try:
        from db.repository import RepositoryManager
        repo = RepositoryManager(db_path)
        c = repo.contacts.get_by_wxid(wxid)
        if c:
            return c.nickname or c.remark
    except Exception:
        pass
    return None


def get_nickname_from_wechat_db(db_storage: str, wxid: str) -> str | None:
    """尝试从微信原始数据库查昵称（需要解密，可能失败）"""
    return None


def detect_current_login() -> dict | None:
    """
    检测当前登录的微信账号。
    方法：找 xwechat_files 下最近修改的 db_storage 目录。
    """
    dirs = find_wechat_db_dirs()
    if not dirs:
        return None

    latest = None
    latest_mtime = 0
    for d in dirs:
        try:
            mtime = os.path.getmtime(d["db_storage"])
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest = d
        except OSError:
            continue

    return latest


def read_env() -> dict:
    """读取 .env 中现有的微信配置"""
    env_path = os.path.join(PROJECT_ROOT, ".env")
    result = {
        "WECHAT_DB_PATH": "",
        "WECHAT_YINYUE_WXID": "",
        "WECHAT_OWNER_WXIDS": "",
    }
    if not os.path.isfile(env_path):
        return result
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                k = k.strip()
                if k in result:
                    result[k] = v.strip()
    return result


def update_env(wxid: str, db_storage: str, role: str, owner_wxid: str = ""):
    """更新 .env 文件"""
    env_path = os.path.join(PROJECT_ROOT, ".env")
    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()

    def set_val(key: str, val: str):
        nonlocal content
        pattern = rf"^{re.escape(key)}=.*$"
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, f"{key}={val}", content, flags=re.MULTILINE)
        else:
            content += f"\n{key}={val}\n"

    set_val("WECHAT_DB_PATH", db_storage)

    if role == "yinyue":
        set_val("WECHAT_YINYUE_WXID", wxid)
        if owner_wxid:
            set_val("WECHAT_OWNER_WXIDS", owner_wxid)
    elif role == "owner":
        set_val("WECHAT_OWNER_WXIDS", wxid)
        existing_yinyue = re.search(r"WECHAT_YINYUE_WXID=(wxid_\w+)", content)
        if not existing_yinyue:
            set_val("WECHAT_YINYUE_WXID", "")

    with open(env_path, "w", encoding="utf-8") as f:
        f.write(content)


def main():
    parser = argparse.ArgumentParser(description="获取当前登录微信账号信息")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--set-env", action="store_true", help="交互式写入 .env")
    args = parser.parse_args()

    dirs = find_wechat_db_dirs()
    current = detect_current_login()
    env = read_env()

    if not dirs:
        print("未找到任何微信账号数据目录。")
        print("请确保微信已登录，且数据在 %USERPROFILE%\\Documents\\xwechat_files 下")
        return

    if args.json:
        output = {
            "accounts": dirs,
            "current_login": current,
            "env_config": env,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    print("=" * 60)
    print("  微信账号检测")
    print("=" * 60)
    print()
    print(f"  找到 {len(dirs)} 个微信账号数据目录：")
    print()

    for i, d in enumerate(dirs, 1):
        is_current = current and d["wxid"] == current["wxid"]
        marker = " ← 当前登录" if is_current else ""

        in_env_yinyue = d["wxid"] == env.get("WECHAT_YINYUE_WXID")
        in_env_owner = d["wxid"] in [x.strip() for x in env.get("WECHAT_OWNER_WXIDS", "").split(",") if x.strip()]
        env_role = ""
        if in_env_yinyue:
            env_role += " [鲸鲸号]"
        if in_env_owner:
            env_role += " [主人号]"

        print(f"  [{i}] wxid: {d['wxid']}{marker}{env_role}")
        print(f"      路径: {d['db_storage']}")

        try:
            from db.repository import RepositoryManager
            from core.bootstrap import DB_PATH
            repo = RepositoryManager(DB_PATH)
            c = repo.contacts.get_by_wxid(d["wxid"])
            if c:
                print(f"      昵称: {c.nickname}")
                if c.remark:
                    print(f"      备注: {c.remark}")
        except Exception:
            pass

        print()

    if current:
        print(f"  当前登录: {current['wxid']}")
        print(f"  数据库路径: {current['db_storage']}")

    print()
    print("  .env 现有配置:")
    print(f"    WECHAT_YINYUE_WXID = {env['WECHAT_YINYUE_WXID'] or '(未设置)'}")
    print(f"    WECHAT_OWNER_WXIDS = {env['WECHAT_OWNER_WXIDS'] or '(未设置)'}")
    print(f"    WECHAT_DB_PATH     = {env['WECHAT_DB_PATH'] or '(未设置)'}")

    if args.set_env:
        print()
        print("=" * 60)
        print("  交互式配置 .env")
        print("=" * 60)
        print()

        print("  选择鲸鲸的号（yinyue_wxid）：")
        for i, d in enumerate(dirs, 1):
            print(f"    [{i}] {d['wxid']}")
        print("    [0] 手动输入")

        choice = input("\n  请选择: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(dirs):
            yinyue_wxid = dirs[int(choice) - 1]["wxid"]
            yinyue_db = dirs[int(choice) - 1]["db_storage"]
        else:
            yinyue_wxid = input("  输入鲸鲸 wxid: ").strip()
            yinyue_db = current["db_storage"] if current else ""

        print()
        print("  选择主人的号（owner_wxid）：")
        for i, d in enumerate(dirs, 1):
            marker = " (同鲸鲸号!)" if d["wxid"] == yinyue_wxid else ""
            print(f"    [{i}] {d['wxid']}{marker}")
        print("    [0] 手动输入")

        choice = input("\n  请选择: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(dirs):
            owner_wxid = dirs[int(choice) - 1]["wxid"]
        else:
            owner_wxid = input("  输入主人 wxid: ").strip()

        if yinyue_wxid == owner_wxid:
            print("\n  ⚠️  鲸鲸号和主人号不能相同！")
            return

        print()
        print("  将写入 .env:")
        print(f"    WECHAT_YINYUE_WXID = {yinyue_wxid}")
        print(f"    WECHAT_OWNER_WXIDS = {owner_wxid}")
        print(f"    WECHAT_DB_PATH     = {yinyue_db}")

        confirm = input("\n  确认？[y/N] ").strip().lower()
        if confirm == "y":
            update_env(yinyue_wxid, yinyue_db, role="yinyue", owner_wxid=owner_wxid)
            print("  .env 已更新 ✓")
        else:
            print("  已取消")


if __name__ == "__main__":
    main()
