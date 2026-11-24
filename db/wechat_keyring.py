"""微信 4.x 的多库解密验证：一把 key 不一定开所有库（contact.db 就是反例）。

2026-10-28 起的约定：
- keyring  = data/platforms/wechat/keyring.json      每个库各自的 key + 验证结果
- caps     = data/platforms/wechat/<acct>/capabilities.json  这一台机器上"哪些数据可用"
- 能力缺失不再静默：写进 caps.degraded，并在面板/白名单里明确降级。
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYRING_PATH = os.path.join(ROOT, "data", "platforms", "wechat", "keyring.json")

# 目录名 -> 能力名（决定下游哪些功能可用）
FAMILY = {
    "message": "messages",
    "contact": "contacts",
    "message_fts": "fts",
    "message_resource": "media",
    "emoticon": "emoji",
    "media": "media",
    "favorite": "favorites",
    "session": "sessions",
    "head_image": "avatars",
}

# 每个能力至少要有哪张表，才算"真的能用"
EXPECT_TABLE = {
    # 2026-10-28：message_*.db 是分片库，主表名不叫 messages（实测 "no such table:
    # messages"），所以只做 integrity_check，不锁表名；否则会把"能解密"误判成"不可用"。
    "messages": None,
    "contacts": "contact",
    "fts": None,
    "media": None,
    "emoji": None,
    "favorites": None,
    "sessions": None,
    "avatars": None,
}


def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _save(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def caps_path(account: str) -> str:
    return os.path.join(ROOT, "data", "platforms", "wechat", account, "capabilities.json")


def capabilities(account: str) -> dict:
    """"这一台机器、这个账号"现在能拿到哪些数据。文件不存在就返回空（＝未知，别当可用）。"""
    return _load(caps_path(account), {})


def available(account: str, name: str, default: bool = True) -> bool:
    caps = capabilities(account)
    got = (caps.get("capabilities") or {}).get(name)
    return default if got is None else bool(got)


def _verify(out_path: str, expect_table: str = None) -> tuple:
    """解出来的库能不能真读：完整性 + 主表 + 行数。"""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % out_path.replace("\\", "/"), uri=True)
        ok = str(con.execute("pragma integrity_check").fetchone()[0]).lower() == "ok"
        if not ok:
            con.close()
            return False, "integrity_check_failed"
        if expect_table:
            con.execute("select 1 from %s limit 1" % expect_table).fetchone()
        con.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:120]


def source_dbs(account: str) -> list:
    """这个账号在微信客户端里的库文件（db_storage 下）。"""
    hits: list = []
    pat = os.path.join(os.path.expanduser("~"), "Documents", "xwechat_files",
                       account + "*", "db_storage", "**", "*.db")
    hits += glob.glob(pat, recursive=True)
    return sorted(set(hits))


def family_of(path: str) -> str:
    d = os.path.basename(os.path.dirname(path)).lower()
    return FAMILY.get(d, d)


def probe(account: str, key: str = "", *, force: bool = False, limit_per_family: int = 1) -> dict:
    """逐个库试解密，写 keyring + capabilities。返回 {ok, capabilities, degraded, dbs}。"""
    from db import decrypt as _d  # 复用现成的解密实现

    # 2026-10-28：默认优先用 .env 里的 WECHAT_KEY —— get_wx_key() 在本机会返回
    # 形如 00010203... 的假 key（被 wx_key_rejected looks_like_pattern 拒绝过），
    # 用它逐库试解密会全部 v4_hmac_mismatch。
    key = (key or os.environ.get("WECHAT_KEY") or _d.get_wx_key() or "").strip()
    import re as _re
    if not _re.fullmatch(r"[0-9a-fA-F]{64}", key or ""):
        key = ""
    srcs = source_dbs(account)
    kr = _load(KEYRING_PATH, {"version": 4, "keys": {}})
    keys = kr.setdefault("keys", {})
    tmpdir = tempfile.mkdtemp(prefix="wxprobe_")
    caps, dbs, seen = {}, [], {}
    try:
        for src in srcs:
            rel = os.path.relpath(src, os.path.dirname(os.path.dirname(os.path.dirname(src))))
            fam = family_of(src)
            if "_fts" in os.path.basename(src).lower():
                continue  # 全文索引我们从不读，跳过（解它没意义，还会把能力判成不可用）
            seen[fam] = seen.get(fam, 0) + 1
            if seen[fam] > limit_per_family and not force:
                continue
            ent = keys.get(rel) or {}
            use_key = ent.get("key") or key
            out = os.path.join(tmpdir, str(abs(hash(rel))) + ".db")
            ok, why = False, ""
            t0 = time.time()
            try:
                _ret = _d._decrypt_single_db(use_key, src, out)
                if not _ret:
                    why = "decrypt_returned_false"
                elif not os.path.exists(out):
                    why = "out_missing"
                else:
                    ok, why = _verify(out, EXPECT_TABLE.get(fam))
            except Exception as exc:  # noqa: BLE001
                why = (exc.__class__.__name__ + ": " + str(exc))[:120]
            if not ok and not why:
                why = "hmac_mismatch"
            keys[rel] = {"family": fam, "key": use_key if ok else "",
                         "ok": ok, "why": "" if ok else why,
                         "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "ms": int((time.time() - t0) * 1000)}
            caps[fam] = caps.get(fam, True) and ok
            dbs.append({"db": rel, "family": fam, "ok": ok, "why": why,
                        "ms": keys[rel]["ms"]})
            try:
                os.remove(out)
            except OSError:
                pass
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    kr["version"] = 4
    kr["obtained_by"] = kr.get("obtained_by") or "env:WECHAT_KEY"
    kr["probed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(KEYRING_PATH, kr)

    degraded = sorted([k for k, v in caps.items() if not v])
    out = {"ok": True, "account": account,
           "probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "capabilities": caps, "degraded": degraded, "dbs": dbs}
    _save(caps_path(account), out)
    return out


if __name__ == "__main__":
    import sys
    acct = sys.argv[1] if len(sys.argv) > 1 else "wxid_seed02"
    print(json.dumps(probe(acct, force=True), ensure_ascii=False, indent=2)[:1500])
