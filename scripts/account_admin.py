# -*- coding: utf-8 -*-
"""账号登记的管理口：**唯一会写 `account_registry` 的地方**（2026-10-27）。

为什么单独一个脚本而不是塞进 `console/executor.py`：
  * executor 的纪律是"绝不碰数据库"（见它自己的 docstring），这条不能破；
  * 写库要过 `db.write_guard`：生产库只有在**入口代码显式声明意图**时才允许写
    （见 `db/write_guard.py::declare_writes_allowed` 的说明）。所以"声明"必须
    发生在一个明确的入口里，而不是一个被 HTTP 端点随手调用的函数里。

纪律：
  1. 只动 `account_registry` 一张表；不建目录、不删数据、不碰别的表。
  2. 真写之前，把整张表备份成 `data/console/accounts_backup/accounts-<ts>.json`。
  3. 真写必须带 `--allow-production`；不带就只做探测（dry-run）。
  4. 输出永远是一行 JSON，方便 console/executor.py 原样透传给面板。
  5. `role='owner'` 是"谁是我要服务的人"的唯一登记处 —— 允许多个，但会警告。

用法（面板会通过 executor 调它，人也可以直接跑）：
    python scripts/account_admin.py probe --platform wechat --account-id wxid_x
    python scripts/account_admin.py save  --platform wechat --account-id wxid_x \
        --role yinyue --data-dir "data/platforms/wechat/wxid_x" --allow-production
    python scripts/account_admin.py remove --account-id wxid_x --allow-production
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
BACKUP_DIR = os.path.join(DATA, "console", "accounts_backup")
PLATFORMS = ("wechat", "qq")
ROLES = ("owner", "yinyue", "guest")
_ID_RE = re.compile(r"^[A-Za-z0-9_@.\-]{2,64}$")


def _meta_path() -> str:
    return os.environ.get("META_DB_PATH") or os.path.join(DATA, "yinyue_meta.db")


def _ro(path: str):
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    return con


def _rows() -> list[dict]:
    path = _meta_path()
    if not os.path.exists(path):
        raise SystemExit(json.dumps({"ok": False, "error": "元库不在：%s" % path}, ensure_ascii=False))
    con = _ro(path)
    try:
        cur = con.execute("select account_id, platform, role, data_dir, enabled from account_registry")
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def _default_dir(platform: str, account_id: str) -> str:
    return os.path.join("data", "platforms", platform, account_id)


def probe(platform: str, account_id: str, data_dir: str = "", role: str = "") -> dict:
    warns, errs = [], []
    platform = (platform or "").strip().lower()
    account_id = (account_id or "").strip()
    role = (role or "").strip().lower()
    if platform not in PLATFORMS:
        errs.append("platform 只能是 %s，收到 %r" % ("/".join(PLATFORMS), platform))
    if not _ID_RE.match(account_id or ""):
        errs.append("account_id 只接受字母/数字/_/@/./-（2~64 字），微信是 wxid_xxx、QQ 是数字")
    if role and role not in ROLES:
        errs.append("role 只能是 %s" % "/".join(ROLES))
    rel = (data_dir or "").strip() or _default_dir(platform, account_id)
    absdir = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
    out = {"platform": platform, "account_id": account_id, "role": role,
           "data_dir": os.path.relpath(absdir, ROOT), "dir_exists": os.path.isdir(absdir),
           "db_exists": False, "messages": None, "last_at": None}
    acct_db = os.path.join(absdir, "account.db")
    if not out["dir_exists"]:
        warns.append("目录还不存在（可以先登记，等它自己写出来）")
    elif os.path.exists(acct_db):
        out["db_exists"] = True
        try:
            c2 = _ro(acct_db)
            out["messages"] = c2.execute("select count(*) from messages").fetchone()[0]
            out["last_at"] = c2.execute("select max(created_at) from messages").fetchone()[0]
            c2.close()
        except Exception as exc:  # noqa: BLE001
            warns.append("account.db 读不动：%s: %s" % (type(exc).__name__, exc))
    else:
        warns.append("目录在但还没有 account.db（正常：第一次收到消息才会建）")
    try:
        rows = _rows()
    except SystemExit as exc:
        return {"ok": False, "error": str(exc), "probe": out}
    me = [r for r in rows if r["account_id"] == account_id]
    if me:
        out["existing"] = {k: me[0][k] for k in ("platform", "role", "data_dir", "enabled")}
        if me[0]["platform"] != platform:
            errs.append("这个 account_id 已经登记成 %s，不能改成 %s（会串库）" % (
                me[0]["platform"], platform))
    if role == "owner":
        same = [r["account_id"] for r in rows
                if r["role"] == "owner" and r["account_id"] != account_id and r["platform"] == platform]
        other = [r["account_id"] for r in rows
                 if r["role"] == "owner" and r["account_id"] != account_id and r["platform"] != platform]
        if same:
            warns.append("%s 平台已经有 owner：%s —— 同一平台两个 owner 时该平台取哪条不确定，建议每平台一个"
                         % (platform, "、".join(same)))
        if other:
            warns.append("别的平台也各有 owner（%s）—— 这是正常形态，不冲突" % "、".join(other))
    out["owners_now"] = [r["account_id"] for r in rows if r["role"] == "owner"]
    out["owners_by_platform"] = {}
    for r in rows:
        if r["role"] == "owner":
            out["owners_by_platform"].setdefault(r["platform"], []).append(r["account_id"])
    out["warnings"], out["errors"] = warns, errs
    return {"ok": not errs, "probe": out}


def _backup(reason: str) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, "accounts-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    payload = {"at": datetime.now().isoformat(timespec="seconds"), "reason": reason,
               "meta": os.path.relpath(_meta_path(), ROOT), "rows": _rows()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def _write_open(path: str):
    """拿到生产库的可写句柄：必须先声明意图，再过 write_guard。"""
    sys.path.insert(0, ROOT)
    from db import write_guard

    write_guard.declare_writes_allowed(declared_by="scripts/account_admin.py")
    write_guard.assert_writes_allowed(path, what="scripts/account_admin.py 改 account_registry")
    from sqlalchemy import create_engine

    return create_engine("sqlite:///%s" % path.replace("\\", "/"))


def save(platform: str, account_id: str, data_dir: str, role: str, enabled: int) -> dict:
    got = probe(platform, account_id, data_dir, role)
    if not got.get("ok"):
        return got
    pr = got["probe"]
    backup = _backup("save %s/%s" % (platform, account_id))
    eng = _write_open(_meta_path())
    from sqlalchemy.orm import Session

    from db.meta_models import AccountRegistry

    with Session(eng) as s:
        row = s.get(AccountRegistry, account_id)
        if row:
            row.platform, row.role, row.data_dir, row.enabled = platform, role, pr["data_dir"], int(enabled)
            action = "updated"
        else:
            row = AccountRegistry(account_id=account_id, platform=platform, role=role,
                                  data_dir=pr["data_dir"], enabled=int(enabled),
                                  config_json=None, created_at=datetime.now())
            s.add(row)
            action = "inserted"
        s.commit()
    return {"ok": True, "action": action, "probe": pr, "backup": os.path.relpath(backup, ROOT),
            "note": "%s %s/%s；改完要重启鲸鲸.bat 才生效" % (
                "已更新" if action == "updated" else "已新增", platform, account_id)}


def remove(account_id: str) -> dict:
    rows = [r for r in _rows() if r["account_id"] == account_id]
    if not rows:
        return {"ok": False, "error": "没有这个账号：%s" % account_id}
    backup = _backup("remove %s" % account_id)
    eng = _write_open(_meta_path())
    from sqlalchemy.orm import Session

    from db.meta_models import AccountRegistry

    with Session(eng) as s:
        row = s.get(AccountRegistry, account_id)
        if row:
            s.delete(row)
            s.commit()
    return {"ok": True, "action": "removed", "row": rows[0],
            "backup": os.path.relpath(backup, ROOT),
            "note": "已从登记表移除 %s（数据目录没动）；改完要重启鲸鲸.bat" % account_id}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("probe", "save", "remove", "list"))
    ap.add_argument("--platform", default="")
    ap.add_argument("--account-id", default="")
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--role", default="")
    ap.add_argument("--enabled", type=int, default=1)
    ap.add_argument("--allow-production", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            out = {"ok": True, "rows": _rows()}
        elif a.cmd == "probe":
            out = probe(a.platform, a.account_id, a.data_dir, a.role)
        elif a.cmd == "save":
            if not a.allow_production:
                got = probe(a.platform, a.account_id, a.data_dir, a.role)
                got["dry_run"] = True
                got["note"] = "只看不打（没带 --allow-production）；确认无误再加 --allow-production"
                out = got
            else:
                out = save(a.platform, a.account_id, a.data_dir, a.role or "yinyue", a.enabled)
        else:
            out = remove(a.account_id) if a.allow_production else {
                "ok": False, "dry_run": True, "error": "删除要带 --allow-production"}
    except Exception as exc:  # noqa: BLE001 —— 永远回一行 JSON
        out = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
