"""Windows 目录权限收紧的唯一实现（CB-138，2026-10-28）。

`chmod 0600` 在 Windows 上是**空操作**——NTFS 看 ACL，不看 POSIX mode。
要真做到「只有我能读」，只能 `icacls` 断继承。

抄 surfcore 时发现的同一个坑：`icacls DIR /inheritance:r /grant:r "u:(OI)(CI)F" /T`
把**目录专用**的 `(OI)(CI)` 套到 `/T` 展开的文件上会失败（继承标志只对目录有意义）。
所以这里目录与文件分两套参数，且永远不把 `/T` 和 `(OI)(CI)` 放一起。

失败必须明说「没做成」：返回 `ok=False` + 逐条失败原因，调用方不许把失败当成功。
"""
from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from core.infra import skips
from safelog import warn  # noqa: E402 - 仓库根在 sys.path 上（脚本从根目录跑）

import structlog

log = structlog.get_logger(__name__)


def current_user() -> str:
    """当前 Windows 用户名（icacls 里可用裸用户名，也可用 DOMAIN\\user）。"""
    return (os.environ.get("USERNAME") or getpass.getuser() or "").strip()


def grants_for(target: Path, user: str) -> list[str]:
    """icacls 参数（不含 `icacls` 与目标本身）。

    目录：`/inheritance:r /grant:r "u:(OI)(CI)F"`（只作用于这个目录，新子项才继承）
    文件：`/inheritance:r /grant:r "u:F"`（不带 (OI)(CI)）
    两者都不带 `/T`——递归由 Python 自己走，才能对每一层用对的参数、并逐条报错。
    """
    if not user:
        raise ValueError("拿不到当前用户名，无法收紧 ACL")
    perm = f"{user}:(OI)(CI)F" if target.is_dir() else f"{user}:F"
    return ["/inheritance:r", "/grant:r", perm]


def targets_of(path: Path) -> list[Path]:
    if path.is_dir():
        return [path] + sorted(p for p in path.rglob("*"))
    return [path]


def tighten(path: str | os.PathLike, *, dry_run: bool = False, timeout: int = 60) -> dict:
    """断继承 + 只给当前用户完全控制。返回逐条结果，失败不吞。"""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "path": str(p), "reason": "路径不存在", "applied": 0, "failed": [], "commands": []}
    user = current_user()
    if not user:
        return {"ok": False, "path": str(p), "reason": "拿不到当前用户名", "applied": 0, "failed": [], "commands": []}
    out = {"ok": True, "path": str(p), "user": user, "applied": 0, "failed": [], "commands": []}
    for t in targets_of(p):
        cmd = ["icacls", str(t)] + grants_for(t, user)
        out["commands"].append(cmd)
        if dry_run:
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 要原样报给上
            out["failed"].append({"path": str(t), "err": repr(exc)})
            continue
        if r.returncode != 0:
            out["failed"].append({"path": str(t), "err": ((r.stdout or "") + (r.stderr or "")).strip()[:200]})
        else:
            out["applied"] += 1
    if out["failed"]:
        out["ok"] = False
        out["reason"] = "%d 个路径没设成" % len(out["failed"])
        try:
            warn("acl_tighten_failed", path=out["path"], reason=out["reason"], failed=len(out["failed"]))
        except Exception as exc:  # noqa: BLE001 - 记事件失败时至少留一句话，不然"没做成"也没人知道
            log.warning("acl_tighten_warn_failed", err=repr(exc))
    return out



# 「该收紧的目录」只有这一份清单：CLI 与 checkup 都问它，不许各写一遍。
TIGHTENED = ("data/persona/cards",)
# 这些主体不算「别人」：系统自己、管理员组、所有者。
TRUSTED = ("NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators", "CREATOR OWNER")


def _decode(raw: bytes) -> str:
    """icacls 的输出按本机代码页来（中文 Windows 是 gbk）：先试 utf-8，再退 gbk。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            skips.skip("acl._decode", "malformed", enc)
            continue
    return raw.decode("utf-8", "replace")


def acl_rows(target: str | os.PathLike, *, timeout: int = 30) -> list[dict] | None:
    """读一个路径现在的 ACE：`[{"who": 主体, "perms": "(I)(OI)(CI)(F)", "inherited": True}]`。

    读不出来返回 `None` —— 不许把「读不到」当成「没人能读」。
    """
    path = Path(target)
    try:
        r = subprocess.run(["icacls", str(path)], capture_output=True, timeout=timeout)
    except Exception:  # noqa: BLE001 - 读不到就是读不到，上面会看见 None
        return None
    if r.returncode != 0:
        return None
    prefix = str(path)
    rows = []
    for line in _decode(r.stdout or b"").splitlines():
        s = line.strip()
        if not s:
            continue
        if "Successfully processed" in s or "已成功处理" in s:
            continue
        if "Mandatory Label" in s or "强制标签" in s:
            continue  # 完整性标签不是 ACE
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
        who, sep, perms = s.rpartition(":")
        if not sep or not perms.startswith("("):
            continue
        rows.append({"who": who.strip(), "perms": perms, "inherited": "(I)" in perms})
    return rows


def _is_stranger(who: str, user: str) -> bool:
    if not who:
        return True
    tail = who.split("\\")[-1]
    if user and (who == user or tail == user):
        return False
    return not any(who == trust or tail == trust.split("\\")[-1] for trust in TRUSTED)


def report(paths: list[str] | tuple[str, ...] | None = None) -> dict:
    """「谁能读她的卡」——只读，一条 ACL 都不改；读不出来算 unknown，不算 ok。"""
    names = tuple(paths or TIGHTENED)
    user = current_user()
    rows_by_path: dict[str, list[dict]] = {}
    unknown: list[str] = []
    strangers: list[str] = []
    inherited: list[str] = []
    for name in names:
        path = Path(name)
        if not path.exists():
            unknown.append(name)
            continue
        rows = acl_rows(path)
        if rows is None:
            unknown.append(name)
            continue
        rows_by_path[name] = rows
        for row in rows:
            label = "%s:%s" % (name, row["who"])
            if _is_stranger(row["who"], user):
                strangers.append(label)
            elif row["inherited"]:
                inherited.append(label)
    if unknown:
        level = "unknown"
    elif strangers:
        level = "fail"
    elif inherited:
        level = "warn"
    else:
        level = "ok"
    if level == "ok":
        line = "；".join("%s：只有我能读" % n for n in rows_by_path) or "没有要查的目录"
    elif level == "unknown":
        line = "查不到 ACL：" + "、".join(unknown)
    else:
        line = "；".join(
            "%s：%d 个别人、%d 条继承项" % (n, len(strangers), len(inherited)) for n in rows_by_path
        ) or "有问题"
    return {"ok": level == "ok", "at": time.strftime("%Y-%m-%d %H:%M:%S"), "level": level,
            "line": line, "paths": list(rows_by_path), "rows": {k: len(v) for k, v in rows_by_path.items()},
            "strangers": strangers, "inherited": inherited, "unknown": unknown}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in args
    paths = [a for a in args if not a.startswith("--")] or (list(TIGHTENED) + ["logs"])
    worst = 0
    for raw in paths:
        res = tighten(raw, dry_run=dry)
        print(json.dumps(res, ensure_ascii=False)[:800])
        if not res["ok"]:
            worst = 1
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
